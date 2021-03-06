# -*- coding: utf-8 -*-
#
#   pycheops - Tools for the analysis of data from the ESA CHEOPS mission
#
#   Copyright (C) 2018  Dr Pierre Maxted, Keele University
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""
Dataset
=======
 Object class for data access, data caching and data inspection tools

"""

from __future__ import (absolute_import, division, print_function,
                                unicode_literals)
import numpy as np
import tarfile
from zipfile import ZipFile
import re
from pathlib import Path
from .core import load_config
from astropy.io import fits
from astropy.table import Table, MaskedColumn
import matplotlib.pyplot as plt
from .instrument import transit_noise
from ftplib import FTP
from .models import TransitModel, FactorModel, EclipseModel
from uncertainties import UFloat
from lmfit import Parameter, Parameters, minimize, Minimizer,fit_report
from lmfit import __version__ as _lmfit_version_
from lmfit import Model
from scipy.interpolate import interp1d, LSQUnivariateSpline
import matplotlib.pyplot as plt
from emcee import EnsembleSampler
import corner
import copy
from celerite import terms, GP
from sys import stdout 
from astropy.coordinates import SkyCoord, get_body, Angle
from lmfit.printfuncs import gformat
from scipy.signal import medfilt
from .utils import lcbin, mode
import astropy.units as u
from uncertainties import ufloat, UFloat
from uncertainties.umath import sqrt as usqrt
from astropy.timeseries import LombScargle
from astropy.time import Time
from astropy.convolution import convolve, Gaussian1DKernel
from .instrument import CHEOPS_ORBIT_MINUTES
from scipy.stats import skewnorm
from scipy.optimize import minimize as scipy_minimize
from . import __version__
from .funcs import rhostar, massradius

try:
    from dace.cheops import Cheops
except ModuleNotFoundError: 
    pass

_file_key_re = re.compile(r'CH_PR(\d{2})(\d{4})_TG(\d{4})(\d{2})_V(\d{4})')

# Utility function for model fitting
def _kw_to_Parameter(name, kwarg):
    if isinstance(kwarg, float):
        return Parameter(name=name, value=kwarg, vary=False)
    if isinstance(kwarg, int):
        return Parameter(name=name, value=float(kwarg), vary=False)
    if isinstance(kwarg, tuple):
        return Parameter(name=name, value=np.median(kwarg), 
                min=min(kwarg), max=max(kwarg))
    if isinstance(kwarg, UFloat):
        return Parameter(name=name, value=kwarg.n, user_data=kwarg)
    if isinstance(kwarg, Parameter):
        return kwarg
    raise ValueError('Unrecognised type for keyword argument {}'.
        format(name))

#----

def _make_interp(t,x,scale=None):
    if scale is None:
        z = x
    elif scale == 'max':
        z = (x-min(x))/np.ptp(x) 
    elif scale == 'range':
        z = (x-np.median(x))/np.ptp(x)
    else:
        raise ValueError('scale must be None, max or range')
    return interp1d(t,z,bounds_error=False, fill_value=(z[0],z[-1]))

# Prior on (D, W, b) for transit/eclipse fitting.
# This prior assumes uniform priors on cos(i), log(k) and log(aR). The
# factor 2kW is the absolute value of the determinant of the Jacobian, 
# J = d(D, W, b)/d(cosi, k, aR)
def _log_prior(D, W, b):
    if (D < 2e-6) or (D > 0.2): return -np.inf
    if (b < 0) or (b > 1): return -np.inf
    if (W < 1e-4): return -np.inf
    k = np.sqrt(D)
    aR = np.sqrt((1+k)**2 - b**2)/(np.pi*W)
    if (aR < 2): return -np.inf
    return -np.log(2*k*W) - np.log(k) - np.log(aR)

# Target functions for emcee
def _log_posterior_jitter(pos, model, time, flux, flux_err,  params, vn,
        return_fit):

    # Check for pos[i] within valid range has to be done here
    # because it gets set to the limiting value if out of range by the
    # assignment to a parameter with min/max defined.
    parcopy = params.copy()
    for i, p in enumerate(vn):
        v = pos[i]
        if (v < parcopy[p].min) or (v > parcopy[p].max):
            return -np.inf
        parcopy[p].value = v
    fit = model.eval(parcopy, t=time)
    if return_fit:
        return fit

    if False in np.isfinite(fit):
        return -np.inf

    # Also check parameter range here so we catch "derived" parameters
    # that are out of range.
    lnprior = _log_prior(parcopy['D'], parcopy['W'], parcopy['b'])
    if not np.isfinite(lnprior):
        return -np.inf

    for p in parcopy:
        v = parcopy[p].value
        if (v < parcopy[p].min) or (v > parcopy[p].max):
            return -np.inf
        if np.isnan(v):
            return -np.inf
        u = parcopy[p].user_data
        if isinstance(u, UFloat):
            lnprior += -0.5*((u.n - v)/u.s)**2
    if not np.isfinite(lnprior):
        return -np.inf

    jitter = np.exp(parcopy['log_sigma'].value)
    s2 =flux_err**2 + jitter**2
    lnlike = -0.5*(np.sum((flux-fit)**2/s2 + np.log(2*np.pi*s2)))
    return lnlike + lnprior

#----

def _log_posterior_SHOTerm(pos, model, time, flux, flux_err,  params, vn, gp, 
        return_fit):

    # Check for pos[i] within valid range has to be done here
    # because it gets set to the limiting value if out of range by the
    # assignment to a parameter with min/max defined.
    parcopy = params.copy()
    for i, p in enumerate(vn):
        v = pos[i]
        if (v < parcopy[p].min) or (v > parcopy[p].max):
            return -np.inf
        parcopy[p].value = v
    fit = model.eval(parcopy, t=time)
    if return_fit:
        return fit

    if False in np.isfinite(fit):
        return -np.inf
    
    # Also check parameter range here so we catch "derived" parameters
    # that are out of range.
    lnprior = _log_prior(parcopy['D'], parcopy['W'], parcopy['b'])
    if not np.isfinite(lnprior):
        return -np.inf
    for p in parcopy:
        v = parcopy[p].value
        if (v < parcopy[p].min) or (v > parcopy[p].max):
            return -np.inf
        if np.isnan(v):
            return -np.inf
        u = parcopy[p].user_data
        if isinstance(u, UFloat):
            lnprior += -0.5*((u.n - v)/u.s)**2
    if not np.isfinite(lnprior):
        return -np.inf

    resid = flux-fit
    gp.set_parameter('kernel:terms[0]:log_S0',
            parcopy['log_S0'].value)
    gp.set_parameter('kernel:terms[0]:log_Q',
            parcopy['log_Q'].value)
    gp.set_parameter('kernel:terms[0]:log_omega0',
            parcopy['log_omega0'].value)
    gp.set_parameter('kernel:terms[1]:log_sigma',
            parcopy['log_sigma'].value)
    return gp.log_likelihood(resid) + lnprior
    
#---------------

def _make_labels(plotkeys, bjd_ref):
    labels = []
    for key in plotkeys:
        if key == 'T_0':
            labels.append(r'T$_0-{}$'.format(bjd_ref))
        elif key == 'h_1':
            labels.append(r'$h_1$')
        elif key == 'h_2':
            labels.append(r'$h_2$')
        elif key == 'dfdbg':
            labels.append(r'$df\,/\,d{\rm (bg)}$')
        elif key == 'dfdcontam':
            labels.append(r'$df\,/\,d{\rm (contam)}$')
        elif key == 'dfdx':
            labels.append(r'$df\,/\,dx$')
        elif key == 'd2fdx2':
            labels.append(r'$d^2f\,/\,dx^2$')
        elif key == 'dfdy':
            labels.append(r'$df\,/\,dy$')
        elif key == 'd2fdy2':
            labels.append(r'$d^2f\,/\,dy^2$')
        elif key == 'dfdt':
            labels.append(r'$df\,/\,dt$')
        elif key == 'd2fdt2':
            labels.append(r'$d^2f\,/\,dt^2$')
        elif key == 'dfdsinphi':
            labels.append(r'$df\,/\,d\sin(\phi)$')
        elif key == 'dfdcosphi':
            labels.append(r'$df\,/\,d\cos(\phi)$')
        elif key == 'dfdsin2phi':
            labels.append(r'$df\,/\,d\sin(2\phi)$')
        elif key == 'dfdcos2phi':
            labels.append(r'$df\,/\,d\cos(2\phi)$')
        elif key == 'dfdsin3phi':
            labels.append(r'$df\,/\,d\sin(3\phi)$')
        elif key == 'dfdcos3phi':
            labels.append(r'$df\,/\,d\cos(3\phi)$')
        elif key == 'log_sigma':
            labels.append(r'$\log\sigma$')
        elif key == 'log_omega0':
            labels.append(r'$\log\omega_0$')
        elif key == 'log_S0':
            labels.append(r'$\log{\rm S}_0$')
        elif key == 'log_Q':
            labels.append(r'$\log{\rm Q}$')
        elif key == 'sigma_w':
            labels.append(r'$\sigma_w$ [ppm]')
        elif key == 'logrho':
            labels.append(r'$\log\rho_{\star}$')
        elif key == 'aR':
            labels.append(r'a\,/\,R$_{\star}$')
        elif key == 'sini':
            labels.append(r'\sin i')
        else:
            labels.append(key)
    return labels

    
#---------------

class Dataset(object):
    """
    CHEOPS Dataset object

    :param file_key:
    :param force_download:
    :param download_all: If False, download light curves only
    :param configFile:
    :param target:
    :param verbose:

    """

    def __init__(self, file_key, force_download=False, download_all=True,
            configFile=None, target=None, verbose=True):

        self.file_key = file_key
        m = _file_key_re.search(file_key)
        if m is None:
            raise ValueError('Invalid file_key {}'.format(file_key))
        l = [int(i) for i in m.groups()]
        self.progtype,self.prog_id,self.req_id,self.visitctr,self.ver = l

        config = load_config(configFile)
        _cache_path = config['DEFAULT']['data_cache_path']
        tgzPath = Path(_cache_path,file_key).with_suffix('.tgz')
        self.tgzfile = str(tgzPath)

        if tgzPath.is_file() and not force_download:
            if verbose: print('Found archive tgzfile',self.tgzfile)
        else:
            if download_all:
                file_type='all'
            else:
                file_type='lightcurves'
            Cheops.download(file_type, 
                filters={'file_key':{'contains':file_key}},
                output_full_file_path=str(tgzPath)
                )

        lisPath = Path(_cache_path,file_key).with_suffix('.lis')
        # The file list can be out-of-date is force_download is used
        if lisPath.is_file() and not force_download:
            self.list = [line.rstrip('\n') for line in open(lisPath)]
        else:
            if verbose: print('Creating dataset file list')
            tar = tarfile.open(self.tgzfile)
            self.list = tar.getnames()
            tar.close()
            with open(str(lisPath), 'w') as fh:  
                fh.writelines("%s\n" % l for l in self.list)

        # Extract OPTIMAL light curve data file from .tgz file so we can
        # access the FITS file header information
        aperture='OPTIMAL'
        lcFile = "{}-{}.fits".format(self.file_key,aperture)
        lcPath = Path(self.tgzfile).parent/lcFile
        if lcPath.is_file():
            with fits.open(lcPath) as hdul:
                hdr = hdul[1].header
        else:
            tar = tarfile.open(self.tgzfile)
            r=re.compile('(.*_SCI_COR_Lightcurve-{}_.*.fits)'.format(aperture))
            datafile = list(filter(r.match, self.list))
            if len(datafile) == 0:
                raise Exception('Dataset does not contain light curve data.')
            if len(datafile) > 1:
                raise Exception('Multiple light curve files in datset')
            with tar.extractfile(datafile[0]) as fd:
                hdul = fits.open(fd)
                table = Table.read(hdul[1])
                hdr = hdul[1].header
                hdul.writeto(lcPath)
            tar.close()
        self.pi_name = hdr['PI_NAME']
        self.obsid = hdr['OBSID']
        if target is None:
            self.target = hdr['TARGNAME']
        else:
            self.target = target
        coords = SkyCoord(hdr['RA_TARG'],hdr['DEC_TARG'],unit='degree,degree')
        self.ra = coords.ra.to_string(precision=2,unit='hour',sep=':',pad=True)
        self.dec = coords.dec.to_string(precision=1,sep=':',unit='degree',
                alwayssign=True,pad=True)
        self.vmag = hdr['MAG_V']
        self.e_vmag = hdr['MAG_VERR']
        self.spectype = hdr['SPECTYPE']
        self.exptime = hdr['EXPTIME']
        self.texptime = hdr['TEXPTIME']
        self.pipe_ver = hdr['PIPE_VER']
        if verbose:
            print(' PI name     : {}'.format(self.pi_name))
            print(' OBS ID      : {}'.format(self.obsid))
            print(' Target      : {}'.format(self.target))
            print(' Coordinates : {} {}'.format(self.ra, self.dec))
            print(' Spec. type  : {}'.format(self.spectype))
            print(' V magnitude : {:0.2f} +- {:0.2f}'.
                    format(self.vmag, self.e_vmag))
        
#----

    @classmethod
    def from_test_data(self, subdir,  target=None, configFile=None, 
            verbose=True):
        ftp=FTP('obsftp.unige.ch')
        _ = ftp.login()
        wd = "pub/cheops/test_data/{}".format(subdir)
        ftp.cwd(wd)
        filelist = [fl[0] for fl in ftp.mlsd()]
        _re = re.compile(r'CH_(PR\d{6}_TG\d{6}).zip')
        zipfiles = list(filter(_re.match, filelist))
        if len(zipfiles) > 1:
            raise ValueError('More than one dataset in ftp directory')
        if len(zipfiles) == 0:
            raise ValueError('No zip files for datasets in ftp directory')
        zipfile = zipfiles[0]
        config = load_config(configFile)
        _cache_path = config['DEFAULT']['data_cache_path']
        zipPath = Path(_cache_path,zipfile)
        if zipPath.is_file():
            if verbose: print('{} already downloaded'.format(str(zipPath)))
        else:
            cmd = 'RETR {}'.format(zipfile)
            if verbose: print('Downloading {} ...'.format(zipfile))
            ftp.retrbinary(cmd, open(str(zipPath), 'wb').write)
            ftp.quit()
        
        file_key = zipfile[:-4]+'_V0000'
        m = _file_key_re.search(file_key)
        l = [int(i) for i in m.groups()]
        self.progtype,self.prog_id,self.req_id,self.visitctr,self.ver = l

        tgzPath = Path(_cache_path,file_key).with_suffix('.tgz')
        tgzfile = str(tgzPath)

        zpf = ZipFile(str(zipPath), mode='r')
        ziplist = zpf.namelist()

        _re_im = re.compile('(CH_.*SCI_RAW_Imagette_.*.fits)')
        _re_lc = re.compile('(CH_.*_SCI_COR_Lightcurve-.*fits)')
        with tarfile.open(tgzfile, mode='w:gz') as tgz:
            imgfiles = list(filter(_re_im.match, ziplist))
            if len(imgfiles) > 1:
                raise ValueError('More than one imagette file in zip file')
            if len(imgfiles) == 1:
                if verbose: print("Writing Imagette data to .tgz file...")
                imgfile=imgfiles[0]
                tarPath = Path('visit')/Path(file_key)/Path(imgfile).name 
                tarinfo = tarfile.TarInfo(name=str(tarPath))
                zipinfo = zpf.getinfo(imgfile)
                tarinfo.size = zipinfo.file_size
                zf = zpf.open(imgfile)
                tgz.addfile(tarinfo=tarinfo, fileobj=zf)
                zf.close()
            if verbose: print("Writing Lightcurve data to .tgz file...")
            for lcfile in list(filter(_re_lc.match, ziplist)):
                tarPath = Path('visit')/Path(file_key)/Path(lcfile).name
                tarinfo = tarfile.TarInfo(name=str(tarPath))
                zipinfo = zpf.getinfo(lcfile)
                tarinfo.size = zipinfo.file_size
                zf = zpf.open(lcfile)
                tgz.addfile(tarinfo=tarinfo, fileobj=zf)
                zf.close()
                if verbose: print ('.. {} - done'.format(Path(lcfile).name))
        zpf.close()

        return self(file_key=file_key, target=target, verbose=verbose)
        
#----

    @classmethod
    def from_simulation(self, job,  target=None, configFile=None, 
            version=0, verbose=True):
        ftp=FTP('obsftp.unige.ch')
        _ = ftp.login()
        wd = "pub/cheops/simulated_data/CHEOPSim_job{}".format(job)
        ftp.cwd(wd)
        filelist = [fl[0] for fl in ftp.mlsd()]
        _re = re.compile(r'CH_(PR\d{6}_TG\d{6}).zip')
        zipfiles = list(filter(_re.match, filelist))
        if len(zipfiles) > 1:
            raise ValueError('More than one dataset in ftp directory')
        if len(zipfiles) == 0:
            raise ValueError('No zip files for datasets in ftp directory')
        zipfile = zipfiles[0]
        config = load_config(configFile)
        _cache_path = config['DEFAULT']['data_cache_path']
        zipPath = Path(_cache_path,zipfile)
        if zipPath.is_file():
            if verbose: print('{} already downloaded'.format(str(zipPath)))
        else:
            cmd = 'RETR {}'.format(zipfile)
            if verbose: print('Downloading {} ...'.format(zipfile))
            ftp.retrbinary(cmd, open(str(zipPath), 'wb').write)
            ftp.quit()
        
        file_key = "{}_V{:04d}".format(zipfile[:-4],version)
        m = _file_key_re.search(file_key)
        l = [int(i) for i in m.groups()]

        tgzPath = Path(_cache_path,file_key).with_suffix('.tgz')
        tgzfile = str(tgzPath)

        zpf = ZipFile(str(zipPath), mode='r')
        ziplist = zpf.namelist()

        _re_im = re.compile('(CH_.*SCI_RAW_Imagette_.*.fits)')
        _re_lc = re.compile('(CH_.*_SCI_COR_Lightcurve-.*fits)')
        with tarfile.open(tgzfile, mode='w:gz') as tgz:
            imgfiles = list(filter(_re_im.match, ziplist))
            if len(imgfiles) > 1:
                raise ValueError('More than one imagette file in zip file')
            if len(imgfiles) == 1:
                if verbose: print("Writing Imagette data to .tgz file...")
                imgfile=imgfiles[0]
                tarPath = Path('visit')/Path(file_key)/Path(imgfile).name 
                tarinfo = tarfile.TarInfo(name=str(tarPath))
                zipinfo = zpf.getinfo(imgfile)
                tarinfo.size = zipinfo.file_size
                zf = zpf.open(imgfile)
                tgz.addfile(tarinfo=tarinfo, fileobj=zf)
                zf.close()
            if verbose: print("Writing Lightcurve data to .tgz file...")
            for lcfile in list(filter(_re_lc.match, ziplist)):
                tarPath = Path('visit')/Path(file_key)/Path(lcfile).name
                tarinfo = tarfile.TarInfo(name=str(tarPath))
                zipinfo = zpf.getinfo(lcfile)
                tarinfo.size = zipinfo.file_size
                zf = zpf.open(lcfile)
                tgz.addfile(tarinfo=tarinfo, fileobj=zf)
                zf.close()
                if verbose: print ('.. {} - done'.format(Path(lcfile).name))
        zpf.close()

        return self(file_key=file_key, target=target, verbose=verbose)

#----
        
    def get_imagettes(self, verbose=True):
        imFile = "{}-Imagette.fits".format(self.file_key)
        imPath = Path(self.tgzfile).parent / imFile
        if imPath.is_file():
            with fits.open(imPath) as hdul:
                cube = hdul[1].data
                hdr = hdul[1].header
                meta = Table.read(hdul[2])
            if verbose: print ('Imagette data loaded from ',imPath)
        else:
            if verbose: print ('Extracting imagette data from ',self.tgzfile)
            r=re.compile('(.*SCI_RAW_Imagette.*.fits)' )
            datafile = list(filter(r.match, self.list))
            if len(datafile) == 0:
                raise Exception('Dataset does not contains imagette data.')
            if len(datafile) > 1:
                raise Exception('Multiple imagette data files in dataset')
            tar = tarfile.open(self.tgzfile)
            with tar.extractfile(datafile[0]) as fd:
                hdul = fits.open(fd)
                cube = hdul[1].data
                hdr = hdul[1].header
                meta = Table.read(hdul[2])
                hdul.writeto(imPath)
            tar.close()
            if verbose: print('Saved imagette data to ',imPath)

        self.imagettes = (cube, hdr, meta)
        self.imagettes = {'data':cube, 'header':hdr, 'meta':meta}

        return cube

    def get_subarrays(self, verbose=True):
        subFile = "{}-SubArray.fits".format(self.file_key)
        subPath = Path(self.tgzfile).parent / subFile
        if subPath.is_file():
            with fits.open(subPath) as hdul:
                cube = hdul[1].data
                hdr = hdul[1].header
                meta = Table.read(hdul[2])
            if verbose: print ('Subarray data loaded from ',subPath)
        else:
            if verbose: print ('Extracting subarray data from ',self.tgzfile)
            r=re.compile('(.*SCI_COR_SubArray.*.fits)' )
            datafile = list(filter(r.match, self.list))
            if len(datafile) == 0:
                raise Exception('Dataset does not contains subarray data.')
            if len(datafile) > 1:
                raise Exception('Multiple subarray data files in dataset')
            tar = tarfile.open(self.tgzfile)
            with tar.extractfile(datafile[0]) as fd:
                hdul = fits.open(fd)
                cube = hdul[1].data
                hdr = hdul[1].header
                meta = Table.read(hdul[2])
                hdul.writeto(subPath)
            tar.close()
            if verbose: print('Saved subarray data to ',subPath)

        self.subarrays = (cube, hdr, meta)
        self.subarrays = {'data':cube, 'header':hdr, 'meta':meta}

        return cube 
       
       
    def get_lightcurve(self, aperture=None,
            returnTable=False, reject_highpoints=False, verbose=True):

        if aperture not in ('OPTIMAL','RSUP','RINF','DEFAULT'):
            raise ValueError('Invalid/missing aperture name')

        lcFile = "{}-{}.fits".format(self.file_key,aperture)
        lcPath = Path(self.tgzfile).parent / lcFile
        if lcPath.is_file(): 
            with fits.open(lcPath) as hdul:
                table = Table.read(hdul[1])
                hdr = hdul[1].header
            if verbose: print ('Light curve data loaded from ',lcPath)
        else:
            if verbose: print ('Extracting light curve from ',self.tgzfile)
            tar = tarfile.open(self.tgzfile)
            r=re.compile('(.*_SCI_COR_Lightcurve-{}_.*.fits)'.format(aperture))
            datafile = list(filter(r.match, self.list))
            if len(datafile) == 0:
                raise Exception('Dataset does not contain light curve data.')
            if len(datafile) > 1:
                raise Exception('Multiple light curve files in datset')
            with tar.extractfile(datafile[0]) as fd:
                hdul = fits.open(fd)
                table = Table.read(hdul[1])
                hdr = hdul[1].header
                hdul.writeto(lcPath)
            if verbose: print('Saved lc data to ',lcPath)

        ok = (table['EVENT'] == 0) | (table['EVENT'] == 100)
        bjd = np.array(table['BJD_TIME'][ok])
        bjd_ref = np.int(bjd[0])
        self.bjd_ref = bjd_ref
        time = bjd-bjd_ref
        flux = np.array(table['FLUX'][ok])
        flux_err = np.array(table['FLUXERR'][ok])
        fluxmed = np.nanmedian(flux)
        xoff = np.array(table['CENTROID_X'][ok]- table['LOCATION_X'][ok])
        yoff = np.array(table['CENTROID_Y'][ok]- table['LOCATION_Y'][ok])
        roll_angle = np.array(table['ROLL_ANGLE'][ok])
        bg = np.array(table['BACKGROUND'][ok])
        contam = np.array(table['CONTA_LC'][ok])
        ap_rad = hdr['AP_RADI']
        self.bjd_ref = bjd_ref
        self.ap_rad = ap_rad
        if verbose:
            print('Time stored relative to BJD = {:0.0f}'.format(bjd_ref))
            print('Aperture radius used = {:0.0f} arcsec'.format(ap_rad))

        if reject_highpoints:
            C_cut = (2*np.nanmedian(flux)-np.nanmin(flux))
            ok  = (flux < C_cut).nonzero()
            time = time[ok]
            flux = flux[ok]
            flux_err = flux_err[ok]
            xoff = xoff[ok]
            yoff = yoff[ok]
            roll_angle = roll_angle[ok]
            bg = bg[ok]
            contam = contam[ok]
            N_cut = len(bjd) - len(time)
        if verbose:
            if reject_highpoints:
                print('C_cut = {:0.0f}'.format(C_cut))
                print('N(C > C_cut) = {}'.format(N_cut))
            print('Mean counts = {:0.1f}'.format(flux.mean()))
            print('Median counts = {:0.1f}'.format(fluxmed))
            print('RMS counts = {:0.1f} [{:0.0f} ppm]'.format(np.nanstd(flux), 
                1e6*np.nanstd(flux)/fluxmed))
            print('Median standard error = {:0.1f} [{:0.0f} ppm]'.format(
                np.nanmedian(flux_err), 1e6*np.nanmedian(flux_err)/fluxmed))

        self.flux_mean = flux.mean()
        self.flux_median = fluxmed
        self.flux_rms = np.std(flux)
        self.flux_mse = np.nanmedian(flux_err)
        flux = flux/fluxmed
        flux_err = flux_err/fluxmed
        self.lc = {'time':time, 'flux':flux, 'flux_err':flux_err,
                'bjd_ref':bjd_ref, 'table':table, 'header':hdr,
                'xoff':xoff, 'yoff':yoff, 'bg':bg, 'contam':contam,
                'centroid_x':np.array(table['CENTROID_X'][ok]),
                'centroid_y':np.array(table['CENTROID_Y'][ok]),
                'roll_angle':roll_angle, 'aperture':aperture}

        if returnTable:
            return table
        else:
            return time, flux, flux_err

 #----------------------------------------------------------------------------
 # Eclipse and transit fitting

    def lmfit_transit(self, 
            T_0=None, P=None, D=None, W=None, b=None, f_c=None, f_s=None,
            h_1=None, h_2=None,
            c=None, dfdbg=None, dfdcontam=None, 
            dfdx=None, dfdy=None, d2fdx2=None, d2fdy2=None,
            dfdsinphi=None, dfdcosphi=None, dfdsin2phi=None, dfdcos2phi=None,
            dfdsin3phi=None, dfdcos3phi=None, dfdt=None, d2fdt2=None, 
            glint_scale=None, logrhoprior=None):
        """
        Fit a transit to the light curve in the current dataset.

        Parameter values can be specified in one of three ways

        * Fixed value, e.g., P=1.234
        * Free parameter with uniform prior interval specified as a 2-tuple,
          e.g., dfdx=(-1,1). The initial value is taken as the the mid-point of
          the allowed interval.
        * Free parameter with uniform prior interval and initial value
          specified as a 3-tuple, e.g., (0.1, 0.2, 1)
        * Free parameter with a Gaussian prior specified as a ufloat, e.g.,
          ufloat(0,1).

        To enable decorrelation against a parameter, specifiy it as a free
        parameter, e.g., dfdbg=(0,1).

        Decorrelation is done against is a scaled version of the quantity
        specified with a range of either (-1,1) or, for strictly positive
        quantities, (0,1). This means the coeffieicnts dfdx, dfdy, etc.
        correspond to the amplitude of the flux variation due to the
        correlation with the relevant parameter.

        """

        def _chisq_prior(params, *args):
            r =  (flux - model.eval(params, t=time))/flux_err
            for p in params:
                u = params[p].user_data
                if isinstance(u, UFloat):
                    r = np.append(r, (u.n - params[p].value)/u.s)
            return r

        try:
            time = self.lc['time']
            flux = self.lc['flux']
            flux_err = self.lc['flux_err']
            xoff = self.lc['xoff']
            yoff = self.lc['yoff']
            phi = self.lc['roll_angle']*np.pi/180
            bg = self.lc['bg']
            contam = self.lc['contam']
        except AttributeError:
            raise AttributeError("Use get_lightcurve() to load data first.")

        params = Parameters()
        if T_0 is None:
            params.add(name='T_0', value=np.nanmedian(time),
                    min=min(time),max=max(time))
        else:
            params['T_0'] = _kw_to_Parameter('T_0', T_0)
        if P is None:
            params.add(name='P', value=1, vary=False)
        else:
            params['P'] = _kw_to_Parameter('P', P)
        _P = params['P'].value
        if D is None:
            params.add(name='D', value=1-min(flux), min=0,max=0.5)
        else:
            params['D'] = _kw_to_Parameter('D', D)
        k = np.sqrt(params['D'].value)
        if W is None:
            params.add(name='W', value=np.ptp(time)/2/_P,
                    min=np.ptp(time)/len(time)/_P, max=np.ptp(time)/_P) 
        else:
            params['W'] = _kw_to_Parameter('W', W)
        if b is None:
            params.add(name='b', value=0.5, min=0, max=1)
        else:
            params['b'] = _kw_to_Parameter('b', b)
        if f_c is None:
            params.add(name='f_c', value=0, vary=False)
        else:
            params['f_c'] = _kw_to_Parameter('f_c', f_c)
        if f_s is None:
            params.add(name='f_s', value=0, vary=False)
        else:
            params['f_s'] = _kw_to_Parameter('f_s', f_s)
        if h_1 is None:
            params.add(name='h_1', value=0.7224, vary=False)
        else:
            params['h_1'] = _kw_to_Parameter('h_1', h_1)
        if h_2 is None:
            params.add(name='h_2', value=0.6713, vary=False)
        else:
            params['h_2'] = _kw_to_Parameter('h_2', h_2)
        if c is None:
            params.add(name='c', value=1, min=min(flux)/2,max=2*max(flux))
        else:
            params['c'] = _kw_to_Parameter('c', c)
        if dfdbg is not None:
            params['dfdbg'] = _kw_to_Parameter('dfdbg', dfdbg)
        if dfdcontam is not None:
            params['dfdcontam'] = _kw_to_Parameter('dfdcontam', dfdcontam)
        if dfdx is not None:
            params['dfdx'] = _kw_to_Parameter('dfdx', dfdx)
        if dfdy is not None:
            params['dfdy'] = _kw_to_Parameter('dfdy', dfdy)
        if d2fdx2 is not None:
            params['d2fdx2'] = _kw_to_Parameter('d2fdx2', d2fdx2)
        if d2fdy2 is not None:
            params['d2fdy2'] = _kw_to_Parameter('d2fdy2', d2fdy2)
        if dfdt is not None:
            params['dfdt'] = _kw_to_Parameter('dfdt', dfdt)
        if d2fdt2 is not None:
            params['d2fdt2'] = _kw_to_Parameter('d2fdt2', d2fdt2)
        if dfdsinphi is not None:
            params['dfdsinphi'] = _kw_to_Parameter('dfdsinphi', dfdsinphi)
        if dfdcosphi is not None:
            params['dfdcosphi'] = _kw_to_Parameter('dfdcosphi', dfdcosphi)
        if dfdsin2phi is not None:
            params['dfdsin2phi'] = _kw_to_Parameter('dfdsin2phi', dfdsin2phi)
        if dfdcos2phi is not None:
            params['dfdcos2phi'] = _kw_to_Parameter('dfdcos2phi', dfdcos2phi)
        if dfdsin3phi is not None:
            params['dfdsin3phi'] = _kw_to_Parameter('dfdsin3phi', dfdsin3phi)
        if dfdcos3phi is not None:
            params['dfdcos3phi'] = _kw_to_Parameter('dfdcos3phi', dfdcos3phi)
        if glint_scale is not None:
            params['glint_scale'] = _kw_to_Parameter('glint_scale', glint_scale)

        params.add('k',expr='sqrt(D)',min=0,max=1)
        params.add('aR',expr='sqrt((1+k)**2-b**2)/W/pi',min=1)
        params.add('sini',expr='sqrt(1 - (b/aR)**2)')
        # Avoid use of aR in this expr for logrho - breaks error propogation.
        expr = 'log10(4.3275e-4*((1+k)**2-b**2)**1.5/W**3/P**2)'
        params.add('logrho',expr=expr,min=-9,max=6)
        params['logrho'].user_data=logrhoprior
        params.add('e',min=0,max=1,expr='f_c**2 + f_s**2')
        params.add('q_1',min=0,max=1,expr='(1-h_2)**2')
        params.add('q_2',min=0,max=1,expr='(h_1-h_2)/(1-h_2)')

        model = TransitModel()*FactorModel(
            dx = _make_interp(time, xoff, scale='range'),
            dy = _make_interp(time, yoff, scale='range'),
            sinphi = _make_interp(time,np.sin(phi)),
            cosphi = _make_interp(time,np.cos(phi)),
            bg = _make_interp(time,bg, scale='max'),
            contam = _make_interp(time,contam, scale='max') )

        if 'glint_scale' in params.valuesdict().keys():
            try:
                f_theta = self.f_theta
                f_glint = self.f_glint
            except AttributeError:
                raise AttributeError("Use add_glint() to first.")
            def glint_func(t, glint_scale, f_theta=None, f_glint=None ):
                return glint_scale * f_glint(f_theta(t))
            GlintModel = Model(glint_func, independent_vars=['t'],
                f_theta=f_theta, f_glint=f_glint)
            model += GlintModel


        result = minimize(_chisq_prior, params,nan_policy='propagate',
                args=(model, time, flux, flux_err))
        self.model = model
        fit = model.eval(result.params,t=time)
        result.bestfit = fit
        result.rms = (flux-fit).std()
        # Move priors out of result.residual into their own object and update
        # result.ndata
        npriors = len(result.residual) - len(time)
        if npriors > 0:
            result.prior_residual = result.residual[-npriors:]
            result.residual = result.residual[:-npriors]
            result.npriors = npriors
        self.lmfit = result
        self.__lastfit__ = 'lmfit'
        return result

    # ----------------------------------------------------------------
    
    def add_glint(self, nspline=8, mask=None, fit_flux=False,
            moon=False, angle0=None, gapmax=30, 
            show_plot=True, binwidth=15,  figsize=(6,3), fontsize=11):
        """
        Adds a glint model to the current dataset.

        The glint model is a smooth function v. roll angle that can be scaled
        to account for artefacts in the data caused by internal reflections.

        If moon=True the roll angle is measured relative to the apparent
        direction of the Moon, i.e., assume that the glint is due to
        moonlight.

        To use this model, include the the parameter glint_scale in the
        lmfit least-squares fit.

        * nspline - number of splines in the fit
        * mask - fit only data for which mask array is False
        * fit_flux - fit flux rather than residuals from previous fit
        * moon - use roll-angle relative to apparent Moon direction
        * angle0 = dependent variable is (roll angle - angle0)
        * gapmax = parameter to identify large gaps in data - used to
          calculate angle0 of not specified by the user.
        * show_plot - default is to show a plot of the fit
        * binwidth - in degrees for binned points on plot (or None to ignore)
        * figsize  -
        * fontsize -

        Returns the glint function as a function of roll angle/moon angle.

        """
        try:
            time = np.array(self.lc['time'])
            flux = np.array(self.lc['flux'])
            angle = np.array(self.lc['roll_angle'])
        except AttributeError:
            raise AttributeError("Use get_lightcurve() to load data first.")

        if moon:
            bjd = Time(self.bjd_ref+self.lc['time'],format='jd',scale='tdb')
            moon_coo = get_body('moon', bjd)
            target_coo = SkyCoord(self.ra,self.dec,unit=('hour','degree'))
            v_moon = target_coo.position_angle(moon_coo).radian
            ra_m = moon_coo.ra.radian
            ra_s = target_coo.ra.radian
            dec_m = moon_coo.dec.radian
            dec_s = target_coo.dec.radian
            dv_rot = np.degrees(np.arcsin(np.sin(ra_m-ra_s)*np.cos(dec_m)/
                np.sin(v_moon)))
            angle -= dv_rot

        if fit_flux:
            y = flux - 1
        else:
            l = self.__lastfit__
            fit = self.emcee.bestfit if l == 'emcee' else self.lmfit.bestfit
            y = flux - fit

        if angle0 is None:
            x = np.sort(angle)
            gap = np.hstack((x[0], x[1:]-x[:-1]))
            if max(gap) > gapmax:
                angle0 = x[np.argmax(gap)]
            else:
                angle0 = 0 
        if abs(angle0) < 0.01:
            if moon:
                xlab = r'Moon angle [$^{\circ}$]'
            else:
                xlab = r'Roll angle [$^{\circ}$]'
            xlim = (0,360)
            theta = angle 
        else:
            if moon:
                xlab = r'Moon angle - {:0.0f}$^{{\circ}}$'.format(angle0)
            else:
                xlab = r'Roll angle - {:0.0f}$^{{\circ}}$'.format(angle0)
            theta = (360 + angle - angle0) % 360
            xlim = (min(theta),max(theta))

        f_theta = _make_interp(time, theta)

        if mask is not None:
            time = time[~mask]
            theta = theta[~mask]
            y = y[~mask]


        y = y - np.nanmedian(y)
        y = y[np.argsort(theta)]
        theta = np.sort(theta)
        t = np.linspace(min(theta),max(theta),1+nspline,endpoint=False)[1:]
        f_glint = LSQUnivariateSpline(theta,y,t,ext='const')

        self.glint_moon = moon
        self.glint_angle0 = angle0
        self.f_theta = f_theta
        self.f_glint = f_glint

        if show_plot:
            plt.rc('font', size=fontsize)
            fig,ax=plt.subplots(nrows=1, figsize=figsize, sharex=True)
            ax.plot(theta, y, 'o',c='skyblue',ms=2)
            if binwidth:
                r_, f_, e_, n_ = lcbin(theta, y, binwidth=binwidth)
                ax.errorbar(r_,f_,yerr=e_,fmt='o',c='midnightblue',ms=5,
                    capsize=2)
            ax.set_xlim(xlim)
            ylim = np.max(np.abs(y))+0.05*np.ptp(y)
            ax.set_ylim(-ylim,ylim)
            xt = np.linspace(min(theta),max(theta),10001)
            yt = f_glint(xt)
            ax.plot(xt, yt, color='saddlebrown')
            ax.set_xlabel(xlab)
            ax.set_ylabel('Glint')

        return f_glint(f_theta(time))

    # ----------------------------------------------------------------

    def lmfit_eclipse(self, 
            T_0=None, P=None, D=None, W=None, b=None, L=None,
            f_c=None, f_s=None, a_c=None, dfdbg=None, dfdcontam=None, 
            c=None, dfdx=None, dfdy=None, d2fdx2=None, d2fdy2=None,
            dfdsinphi=None, dfdcosphi=None, dfdsin2phi=None, dfdcos2phi=None,
            dfdsin3phi=None, dfdcos3phi=None, dfdt=None, d2fdt2=None,
            glint_scale=None):

        def _chisq_prior(params, *args):
            r =  (flux - model.eval(params, t=time))/flux_err
            for p in params:
                u = params[p].user_data
                if isinstance(u, UFloat):
                    r = np.append(r, (u.n - params[p].value)/u.s)
            return r

        try:
            time = self.lc['time']
            flux = self.lc['flux']
            flux_err = self.lc['flux_err']
            xoff = self.lc['xoff']
            yoff = self.lc['yoff']
            phi = self.lc['roll_angle']*np.pi/180
            bg = self.lc['bg']
            contam = self.lc['contam']
        except AttributeError:
            raise AttributeError("Use get_lightcurve() to load data first.")

        params = Parameters()
        if T_0 is None:
            params.add(name='T_0', value=np.nanmedian(time),
                    min=min(time),max=max(time))
        else:
            params['T_0'] = _kw_to_Parameter('T_0', T_0)
        if P is None:
            params.add(name='P', value=1, vary=False)
        else:
            params['P'] = _kw_to_Parameter('P', P)
        _P = params['P'].value
        if D is None:
            params.add(name='D', value=1-min(flux), min=0,max=0.5)
        else:
            params['D'] = _kw_to_Parameter('D', D)
        k = np.sqrt(params['D'].value)
        if W is None:
            params.add(name='W', value=np.ptp(time)/2/_P,
                    min=np.ptp(time)/len(time)/_P, max=np.ptp(time)/_P) 
        else:
            params['W'] = _kw_to_Parameter('W', W)
        if b is None:
            params.add(name='b', value=0.5, min=0, max=1)
        else:
            params['b'] = _kw_to_Parameter('b', b)
        if L is None:
            params.add(name='L', value=0.001, min=0, max=1)
        else:
            params['L'] = _kw_to_Parameter('L', L)
        if f_c is None:
            params.add(name='f_c', value=0, vary=False)
        else:
            params['f_c'] = _kw_to_Parameter('f_c', f_c)
        if f_s is None:
            params.add(name='f_s', value=0, vary=False)
        else:
            params['f_s'] = _kw_to_Parameter('f_s', f_s)
        if c is None:
            params.add(name='c', value=1, min=min(flux)/2,max=2*max(flux))
        else:
            params['c'] = _kw_to_Parameter('c', c)
        if a_c is None:
            params.add(name='a_c', value=0, vary=False)
        else:
            params['a_c'] = _kw_to_Parameter('a_c', a_c)
        if dfdbg is not None:
            params['dfdbg'] = _kw_to_Parameter('dfdbg', dfdbg)
        if dfdcontam is not None:
            params['dfdcontam'] = _kw_to_Parameter('dfdcontam', dfdcontam)
        if dfdx is not None:
            params['dfdx'] = _kw_to_Parameter('dfdx', dfdx)
        if dfdy is not None:
            params['dfdy'] = _kw_to_Parameter('dfdy', dfdy)
        if d2fdx2 is not None:
            params['d2fdx2'] = _kw_to_Parameter('d2fdx2', d2fdx2)
        if d2fdy2 is not None:
            params['d2fdy2'] = _kw_to_Parameter('d2fdy2', d2fdy2)
        if dfdt is not None:
            params['dfdt'] = _kw_to_Parameter('dfdt', dfdt)
        if d2fdt2 is not None:
            params['d2fdt2'] = _kw_to_Parameter('d2fdt2', d2fdt2)
        if dfdsinphi is not None:
            params['dfdsinphi'] = _kw_to_Parameter('dfdsinphi', dfdsinphi)
        if dfdcosphi is not None:
            params['dfdcosphi'] = _kw_to_Parameter('dfdcosphi', dfdcosphi)
        if dfdsin2phi is not None:
            params['dfdsin2phi'] = _kw_to_Parameter('dfdsin2phi', dfdsin2phi)
        if dfdcos2phi is not None:
            params['dfdcos2phi'] = _kw_to_Parameter('dfdcos2phi', dfdcos2phi)
        if dfdsin3phi is not None:
            params['dfdsin3phi'] = _kw_to_Parameter('dfdsin3phi', dfdsin3phi)
        if dfdcos3phi is not None:
            params['dfdcos3phi'] = _kw_to_Parameter('dfdcos3phi', dfdcos3phi)
        if glint_scale is not None:
            params['glint_scale'] = _kw_to_Parameter('glint_scale', glint_scale)

        params.add('k',expr='sqrt(D)',min=0,max=1)
        params.add('aR',expr='sqrt((1+k)**2-b**2)/W/pi',min=1)
        params.add('sini',expr='sqrt(1 - (b/aR)**2)')
        params.add('e',min=0,max=1,expr='f_c**2 + f_s**2')

        model = EclipseModel()*FactorModel(
            dx = _make_interp(time, xoff, scale='range'),
            dy = _make_interp(time, yoff, scale='range'),
            sinphi = _make_interp(time,np.sin(phi)),
            cosphi = _make_interp(time,np.cos(phi)),
            bg = _make_interp(time,bg, scale='max'),
            contam = _make_interp(time,contam, scale='max') )

        if 'glint_scale' in params.valuesdict().keys():
            try:
                f_theta = self.f_theta
                f_glint = self.f_glint
            except AttributeError:
                raise AttributeError("Use add_glint() to first.")
            def glint_func(t, glint_scale, f_theta=None, f_glint=None ):
                return glint_scale * f_glint(f_theta(t))
            GlintModel = Model(glint_func, independent_vars=['t'],
                f_theta=f_theta, f_glint=f_glint)
            model += GlintModel

        result = minimize(_chisq_prior, params,nan_policy='propagate',
                args=(model, time, flux, flux_err))
        self.model = model
        fit = model.eval(result.params,t=time)
        result.bestfit = fit
        result.rms = (flux-fit).std()
        # Move priors out of result.residual into their own object and update
        # result.ndata
        npriors = len(result.residual) - len(time)
        if npriors > 0:
            result.prior_residual = result.residual[-npriors:]
            result.residual = result.residual[:-npriors]
            result.npriors = npriors
        self.lmfit = result
        self.__lastfit__ = 'lmfit'
        return result

    # ----------------------------------------------------------------

    def lmfit_report(self, **kwargs):
        report = fit_report(self.lmfit, **kwargs)
        rms = self.lmfit.rms*1e6
        s = "    RMS residual       = {:0.1f} ppm\n".format(rms)
        j = report.index('[[Variables]]')
        report = report[:j] + s + report[j:]
        noPriors = True
        params = self.lmfit.params
        parnames = list(params.keys())
        namelen = max([len(n) for n in parnames])
        for p in params:
            u = params[p].user_data
            if isinstance(u, UFloat):
                if noPriors:
                    report+="\n[[Priors]]"
                    noPriors = False
                report += "\n    %s:%s" % (p, ' '*(namelen-len(p)))
                report += '%s +/-%s' % (gformat(u.n), gformat(u.s))
        report += '\n[[Software versions]]'
        report += '\n    CHEOPS DRP : %s' % self.pipe_ver
        report += '\n    pycheops   : %s' % __version__
        report += '\n    lmfit      : %s' % _lmfit_version_
        return(report)

    # ----------------------------------------------------------------

    def emcee_sampler(self, params=None,
            steps=128, nwalkers=64, burn=256, thin=4, log_sigma=None, 
            add_shoterm=False, log_omega0=None, log_S0=None, log_Q=None,
            init_scale=1e-3, progress=True):

        try:
            time = np.array(self.lc['time'])
            flux = np.array(self.lc['flux'])
            flux_err = np.array(self.lc['flux_err'])
        except AttributeError:
            raise AttributeError("Use get_lightcurve() to load data first.")

        try:
            model = self.model
        except AttributeError:
            raise AttributeError(
                    "Use lmfit_transit() or lmfit_eclipse() first.")

        # Make a copy of the lmfit Minimizer result as a template for the
        # output of this method
        result = copy.copy(self.lmfit)
        result.method ='emcee'
        # Remove components on result not relevant for emcee
        result.status = None
        result.success = None
        result.message = None
        result.ier = None
        result.lmdif_message = None

        if params is None:
            params = self.lmfit.params.copy()
        k = params.valuesdict().keys()
        if add_shoterm:
            if 'log_S0' in k:
                pass
            elif log_S0 is None:
                params.add('log_S0', value=-12,  min=-30, max=0)
            else:
                params['log_S0'] = _kw_to_Parameter('log_S0', log_S0)
            # For time in days, and the default value of Q=1/sqrt(2),
            # log_omega0=8  is a correlation length of about 30s and 
            # -2.3 is about 10 days.
            if 'log_omega0' in k:
                pass
            elif log_omega0 is None:
                params.add('log_omega0', value=3, min=-2.3, max=8)
            else:
                lw0 =  _kw_to_Parameter('log_omega0', log_omega0)
                params['log_omega0'] = lw0
            if 'log_Q' in params:
                pass
            elif log_Q is None:
                params.add('log_Q', value=np.log(1/np.sqrt(2)), vary=False)
            else:
                params['log_Q'] = _kw_to_Parameter('log_Q', log_Q)

        if 'log_sigma' in k:
            pass
        elif log_sigma is None:
            if not 'log_sigma' in params:
                params.add('log_sigma', value=-10, min=-16,max=-1)
                params['log_sigma'].stderr = 1
        else:
            params['log_sigma'] = _kw_to_Parameter('log_sigma', log_sigma)
        params.add('sigma_w',expr='exp(log_sigma)*1e6')

        vv = []
        vs = []
        vn = []
        for p in params:
            if params[p].vary:
                vn.append(p)
                vv.append(params[p].value)
                if params[p].stderr is None:
                    if params[p].user_data is None:
                        vs.append(0.1*(params[p].max-params[p].min))
                    else:
                        vs.append(params[p].user_data.s)
                else:
                    if np.isfinite(params[p].stderr):
                        vs.append(params[p].stderr)
                    else:
                        vs.append(0.1*(params[p].max-params[p].min))

        result.var_names = vn
        result.init_vals = vv
        result.init_values = {}
        for n,v in zip(vn, vv):
            result.init_values[n] = v

        vv = np.array(vv)
        vs = np.array(vs)

        args=(model, time, flux, flux_err,  params, vn)
        p = list(params.keys())
        if 'log_S0' in p and 'log_omega0' in p and 'log_Q' in p :
            kernel = terms.SHOTerm(log_S0=params['log_S0'].value,
                    log_Q=params['log_Q'].value,
                    log_omega0=params['log_omega0'].value)
            kernel += terms.JitterTerm(log_sigma=params['log_sigma'].value)

            gp = GP(kernel, mean=0, fit_mean=False)
            gp.compute(time, flux_err)
            log_posterior_func = _log_posterior_SHOTerm
            args += (gp,)
        else:
            log_posterior_func = _log_posterior_jitter
            gp = None
        return_fit = False
        args += (return_fit, )
    
        # Initialize sampler positions ensuring all walkers produce valid
        # function values.
        pos = []
        n_varys = len(vv)

        for i in range(nwalkers):
            params_tmp = params.copy()
            lnlike_i = -np.inf
            while lnlike_i == -np.inf:
                pos_i = vv + vs*np.random.randn(n_varys)*init_scale
                lnlike_i = log_posterior_func(pos_i, *args)

            pos.append(pos_i)

        sampler = EnsembleSampler(nwalkers, n_varys, log_posterior_func,
            args=args)
        if progress:
            print('Running burn-in ..')
            stdout.flush()
        pos, _, _ = sampler.run_mcmc(pos, burn, store=False, 
            skip_initial_state_check=True, progress=progress)
        sampler.reset()
        if progress:
            print('Running sampler ..')
            stdout.flush()
        state = sampler.run_mcmc(pos, steps, thin_by=thin,
            skip_initial_state_check=True, progress=progress)

        flatchain = sampler.get_chain(flat=True).reshape((-1, len(vn)))
        pos_i = flatchain[np.argmax(sampler.get_log_prob()),:]
        return_fit = True
        if gp is None:
            fit = _log_posterior_jitter(pos_i, model, time, flux, flux_err,
                    params, vn, return_fit)
        else:
            fit = _log_posterior_SHOTerm(pos_i, model, time, flux, flux_err,
                    params, vn, gp, return_fit)

        # Use scaled resiudals for consistency with lmfit
        result.residual = (flux - fit)/flux_err
        result.bestfit =  fit
        result.chain = flatchain
        # Store median and stanadrd error of PPD in result.params
        # Store best fit in result.parbest
        parbest = params.copy()
        quantiles = np.percentile(flatchain, [15.87, 50, 84.13], axis=0)
        for i, n in enumerate(vn):
            std_l, median, std_u = quantiles[:, i]
            params[n].value = median
            params[n].stderr = 0.5 * (std_u - std_l)
            params[n].correl = {}
            parbest[n].value = pos_i[i]
            parbest[n].stderr = 0.5 * (std_u - std_l)
            parbest[n].correl = {}
        result.params = params
        result.params_best = parbest
        corrcoefs = np.corrcoef(flatchain.T)
        for i, n in enumerate(vn):
            for j, n2 in enumerate(vn):
                if i != j:
                    result.params[n].correl[n2] = corrcoefs[i, j]
                    result.params_best[n].correl[n2] = corrcoefs[i, j]
        result.lnprob = np.copy(sampler.get_log_prob())
        result.errorbars = True
        result.nvarys = n_varys
        result.nfev = nwalkers*steps*thin
        result.ndata = len(time)
        result.nfree = len(time) - n_varys
        result.chisqr = np.sum((flux-fit)**2/flux_err**2)
        result.redchi = result.chisqr/(len(time) - n_varys)
        loglmax = np.max(sampler.get_log_prob())
        result.aic = 2*n_varys - 2*loglmax
        result.bic = np.log(len(time))*n_varys - 2*loglmax
        result.covar = np.cov(flatchain.T)
        result.rms = (flux - fit).std()
        self.emcee = result
        self.sampler = sampler
        self.__lastfit__ = 'emcee'
        self.gp = gp
        return result

    # ----------------------------------------------------------------

    def emcee_report(self, **kwargs):
        report = fit_report(self.emcee, **kwargs)
        rms = self.emcee.rms*1e6
        s = "    RMS residual       = {:0.1f} ppm\n".format(rms)
        j = report.index('[[Variables]]')
        report = report[:j] + s + report[j:]
        noPriors = True
        params = self.emcee.params
        parnames = list(params.keys())
        namelen = max([len(n) for n in parnames])
        for p in params:
            u = params[p].user_data
            if isinstance(u, UFloat):
                if noPriors:
                    report+="\n[[Priors]]"
                    noPriors = False
                report += "\n    %s:%s" % (p, ' '*(namelen-len(p)))
                report += '%s +/-%s' % (gformat(u.n), gformat(u.s))
        report += '\n[[Software versions]]'
        report += '\n    CHEOPS DRP : %s' % self.pipe_ver
        report += '\n    pycheops   : %s' % __version__
        report += '\n    lmfit      : %s' % _lmfit_version_
        return(report)

    # ----------------------------------------------------------------

    def trail_plot(self, plotkeys=['T_0', 'D', 'W', 'b'],
            width=8, height=1.5):
        """
        Plot parameter values v. step number for each walker.

        These plots are useful for checking the convergence of the sampler.

        The parameters width and height specifiy the size of the subplot for
        each parameter.

        The parameters to be plotted at specified by the keyword plotkeys, or
        plotkeys='all' to plot every jump parameter.

        """

        params = self.emcee.params
        samples = self.sampler.get_chain()

        varkeys = []
        for key in params:
            if params[key].vary:
                varkeys.append(key)

        if plotkeys == 'all':
            plotkeys = varkeys

        n = len(plotkeys)
        fig,ax = plt.subplots(nrows=n, figsize=(width,n*height), sharex=True)
        labels = _make_labels(plotkeys, self.bjd_ref)
        for i,key in enumerate(plotkeys):
            ax[i].plot(samples[:,:,varkeys.index(key)],'k',alpha=0.1)
            ax[i].set_ylabel(labels[i])
            ax[i].yaxis.set_label_coords(-0.1, 0.5)
        ax[-1].set_xlim(0, len(samples))
        ax[-1].set_xlabel("step number");

        return fig






    # ----------------------------------------------------------------

    def corner_plot(self, plotkeys=['T_0', 'D', 'W', 'b'], 
            show_priors=True, show_ticklabels=False,  kwargs=None):

        params = self.emcee.params

        varkeys = []
        for key in params:
            if params[key].vary:
                varkeys.append(key)

        if plotkeys == 'all':
            plotkeys = varkeys

        chain = self.sampler.get_chain(flat=True)
        xs = []
        for key in plotkeys:
            if key in varkeys:
                xs.append(chain[:,varkeys.index(key)])

            if key == 'sigma_w' and params['log_sigma'].vary:
                xs.append(np.exp(self.emcee.chain[:,-1])*1e6)

            if 'D' in varkeys:
                k = np.sqrt(chain[:,varkeys.index('D')])
            else:
                k = np.sqrt(params['D'].value) # Needed for later calculations

            if key == 'k' and 'D' in varkeys:
                xs.append(k)

            if 'b' in varkeys:
                b = chain[:,varkeys.index('b')]
            else:
                b = params['b'].value  # Needed for later calculations

            if 'W' in varkeys:
                W = chain[:,varkeys.index('W')]
            else:
                W = params['W'].value

            aR = np.sqrt((1+k)**2-b**2)/W/np.pi
            if key == 'aR':
                xs.append(aR)

            sini = np.sqrt(1 - (b/aR)**2)
            if key == 'sini':
                xs.append(sini)

            if 'P' in varkeys:
                P = chain[:,varkeys.index('P')]
            else:
                P = params['P'].value   # Needed for later calculations

            if key == 'logrho':
                logrho = np.log10(4.3275e-4*((1+k)**2-b**2)**1.5/W**3/P**2)
                xs.append(logrho)

        kws = {} if kwargs is None else kwargs

        xs = np.array(xs).T
        labels = _make_labels(plotkeys, self.bjd_ref)
        figure = corner.corner(xs, labels=labels, **kws)

        nax = len(labels)
        axes = np.array(figure.axes).reshape((nax, nax))
        if not show_ticklabels:
            for i in range(nax):
                ax = axes[-1, i]
                ax.set_xticklabels([])
                ax.set_xlabel(labels[i])
                ax.xaxis.set_label_coords(0.5, -0.1)
            for i in range(1,nax):
                ax = axes[i,0]
                ax.set_yticklabels([])
                ax.set_ylabel(labels[i])
                ax.yaxis.set_label_coords(-0.1, 0.5)

        if show_priors:
            for i, key in enumerate(plotkeys):
                u = params[key].user_data
                if isinstance(u, UFloat):
                    ax = axes[i, i]
                    ax.axvline(u.n - u.s, color="g", linestyle='--')
                    ax.axvline(u.n + u.s, color="g", linestyle='--')
        return figure

    # ------------------------------------------------------------
    def plot_fft(self, star=None, gsmooth=5, logxlim = (1.5,4.5),
            title=None, fontsize=12, figsize=(8,5)):
        """ 
        
        Lomb-Scargle power-spectrum of the residuals. 

        If the previous fit included a GP then this is _not_ included in the
        calculation of the residuals, i.e., the power spectrum includes the
        power "fitted-out" using the GP. The assumption here is that the GP
        has been used to model stellar variability that we wish to
        characterize using the power spectrum. 

        The red vertical dotted lines show the CHEOPS  orbital frequency and
        its first two harmonics.

        If star is a pycheops starproperties object and star.teff is <7000K,
        then the likely range of nu_max is shown using green dashed lines.

        """
        try:
            time = np.array(self.lc['time'])
            flux = np.array(self.lc['flux'])
            flux_err = np.array(self.lc['flux_err'])
        except AttributeError:
            raise AttributeError("Use get_lightcurve() to load data first.")

        try:
            l = self.__lastfit__
        except AttributeError:
            raise AttributeError(
                    "Use lmfit_transit() to get best-fit parameters first.")

        model = self.model

        params = self.emcee.params_best if l == 'emcee' else self.lmfit.params
        res = flux - self.model.eval(params, t=time)

        # print('nu_max = {:0.0f} muHz'.format(nu_max))
        t_s = time*86400*u.second
        y = (1e6*res)*u.dimensionless_unscaled
        ls = LombScargle(t_s, y, normalization='psd')
        frequency, power = ls.autopower()
        p_smooth = convolve(power, Gaussian1DKernel(gsmooth))

        plt.rc('font', size=fontsize)
        fig,ax=plt.subplots(figsize=(8,5))
        ax.loglog(frequency*1e6,power/1e6,c='gray',alpha=0.5)
        ax.loglog(frequency*1e6,p_smooth/1e6,c='darkcyan')
        # nu_max from Campante et al. (2016) eq (20)
        if star is not None:
            if star.teff < 7000:
                nu_max = 3090 * 10**(star.logg-4.438)*usqrt(star.teff/5777)
                ax.axvline(nu_max.n-nu_max.s,ls='--',c='g')
                ax.axvline(nu_max.n+nu_max.s,ls='--',c='g')
        f_cheops = 1e6/(CHEOPS_ORBIT_MINUTES*60)
        for h in range(1,4):
            ax.axvline(h*f_cheops,ls=':',c='darkred')
        ax.set_xlim(10**logxlim[0],10**logxlim[1])
        ax.set_xlabel(r'Frequency [$\mu$Hz]')
        ax.set_ylabel('Power [ppm$^2$ $\mu$Hz$^{-1}$]');
        ax.set_title(title)
        return fig
    

    # ------------------------------------------------------------
    
    def plot_lmfit(self, figsize=(6,4), fontsize=11, title=None, 
             show_model=True, binwidth=0.01, detrend=False):
        try:
            time = np.array(self.lc['time'])
            flux = np.array(self.lc['flux'])
            flux_err = np.array(self.lc['flux_err'])
        except AttributeError:
            raise AttributeError("Use get_lightcurve() to load data first.")
        try:
            model = self.model
        except AttributeError:
            raise AttributeError("Use lmfit_transit() to fit a model first.")
        try:
            params = self.lmfit.params
        except AttributeError:
            raise AttributeError(
                    "Use lmfit_transit() to get best-fit parameters first.")

        res = flux - self.model.eval(params, t=time)
        tmin = np.round(np.min(time)-0.05*np.ptp(time),2)
        tmax = np.round(np.max(time)+0.05*np.ptp(time),2)
        tp = np.linspace(tmin, tmax, 10*len(time))
        fp = self.model.eval(params,t=tp)
        glint = model.right.name == 'Model(glint_func)'
        if detrend:
            if glint:
                flux -= model.right.eval(params, t=time)  # de-glint
                fp -= model.right.eval(params, t=tp)  # de-glint
                flux /= model.left.right.eval(params, t=time) # de-trend
                fp /= model.left.right.eval(params, t=tp) # de-trend
            else: 
                flux /= model.right.eval(params, t=time) 
                fp /= model.right.eval(params, t=tp) 

        # Transit model only 
        if glint:
            ft = model.left.left.eval(params, t=tp)
        else:
            ft = model.left.eval(params, t=tp)
        if not detrend:
            ft *= params['c'].value

        plt.rc('font', size=fontsize)    
        fig,ax=plt.subplots(nrows=2,sharex=True, figsize=figsize,
                gridspec_kw={'height_ratios':[2,1]})
        ax[0].plot(time,flux,'o',c='skyblue',ms=2,zorder=0)
        ax[0].plot(tp,fp,c='saddlebrown',zorder=2)
        if binwidth:
            t_, f_, e_, n_ = lcbin(time, flux, binwidth=binwidth)
            ax[0].errorbar(t_,f_,yerr=e_,fmt='o',c='midnightblue',ms=5,zorder=2,
                    capsize=2)
        if show_model:
            ax[0].plot(tp,ft,c='forestgreen',zorder=1, lw=2)
        ax[0].set_xlim(tmin, tmax)
        ymin = np.min(flux-flux_err)-0.05*np.ptp(flux)
        ymax = np.max(flux+flux_err)+0.05*np.ptp(flux)
        ax[0].set_ylim(ymin,ymax)
        ax[0].set_title(title)
        if detrend:
            if glint:
                ax[0].set_ylabel('(Flux-glint)/trend')
            else:
                ax[0].set_ylabel('Flux/trend')
        else:
            ax[0].set_ylabel('Flux')
        ax[1].plot(time,res,'o',c='skyblue',ms=2,zorder=0)
        ax[1].plot([tmin,tmax],[0,0],ls=':',c='saddlebrown',zorder=1)
        if binwidth:
            t_, f_, e_, n_ = lcbin(time, res, binwidth=binwidth)
            ax[1].errorbar(t_,f_,yerr=e_,fmt='o',c='midnightblue',ms=5,zorder=2,
                    capsize=2)
        ax[1].set_xlabel('BJD-{}'.format(self.lc['bjd_ref']))
        ax[1].set_ylabel('Residual')
        ylim = np.max(np.abs(res-flux_err)+0.05*np.ptp(res))
        ax[1].set_ylim(-ylim,ylim)
        fig.tight_layout()
        return fig
        
    # ------------------------------------------------------------
    
    def plot_emcee(self, title=None, nsamples=32, detrend=False, 
            binwidth=0.01, show_model=True,  
            figsize=(6,4), fontsize=11):

        try:
            time = np.array(self.lc['time'])
            flux = np.array(self.lc['flux'])
            flux_err = np.array(self.lc['flux_err'])
        except AttributeError:
            raise AttributeError("Use get_lightcurve() to load data first.")
        try:
            model = self.model
        except AttributeError:
            raise AttributeError("Use lmfit_transit() to get a model first.")
        try:
            parbest = self.emcee.params_best
        except AttributeError:
            raise AttributeError(
                    "Use emcee_transit() or emcee_eclipse() first.")

        res = flux - self.model.eval(parbest, t=time)
        tmin = np.round(np.min(time)-0.05*np.ptp(time),2)
        tmax = np.round(np.max(time)+0.05*np.ptp(time),2)
        tp = np.linspace(tmin, tmax, 10*len(time))
        fp = self.model.eval(parbest,t=tp)
        glint = model.right.name == 'Model(glint_func)'
        flux0 = flux + 0  # Copy flux, don't point to it!
        if detrend:
            if glint:
                flux -= model.right.eval(parbest, t=time)  # de-glint
                fp -= model.right.eval(parbest, t=tp)  # de-glint
                flux /= model.left.right.eval(parbest, t=time) # de-trend
                fp /= model.left.right.eval(parbest, t=tp) # de-trend
            else: 
                flux /=  model.right.eval(parbest, t=time) 
                fp /= model.right.eval(parbest, t=tp) 

        # Transit model only 
        if glint:
            ft = model.left.left.eval(parbest, t=tp)
        else:
            ft = model.left.eval(parbest, t=tp)
        if not detrend:
            ft *= parbest['c'].value

        plt.rc('font', size=fontsize)    
        fig,ax=plt.subplots(nrows=2,sharex=True, figsize=figsize,
                gridspec_kw={'height_ratios':[2,1]})

        ax[0].plot(time,flux,'o',c='skyblue',ms=2,zorder=0)
        ax[0].plot(tp,fp,c='saddlebrown',zorder=1)
        if binwidth:
            t_, f_, e_, n_ = lcbin(time, flux, binwidth=binwidth)
            ax[0].errorbar(t_,f_,yerr=e_,fmt='o',c='midnightblue',ms=5,
                    zorder=2, capsize=2)
        if show_model:
            ax[0].plot(tp,ft,c='forestgreen',zorder=1, lw=2)

        nchain = self.emcee.chain.shape[0]
        partmp = parbest.copy()
        if self.gp is None:
            for i in np.linspace(0,nchain,nsamples,endpoint=False,
                    dtype=np.int):
                for j, n in enumerate(self.emcee.var_names):
                    partmp[n].value = self.emcee.chain[i,j]
                    fp = self.model.eval(partmp,t=tp)
                    if detrend:
                        if glint:
                            fp -= model.right.eval(partmp, t=tp)
                            fp /= model.left.right.eval(partmp, t=tp) 
                        else: 
                            fp /= self.model.right.eval(partmp, t=tp)
                ax[0].plot(tp,fp,c='saddlebrown',zorder=1,alpha=0.1)
        else:
            self.gp.set_parameter('kernel:terms[0]:log_S0',
                    parbest['log_S0'].value)
            self.gp.set_parameter('kernel:terms[0]:log_Q',
                    parbest['log_Q'].value)
            self.gp.set_parameter('kernel:terms[0]:log_omega0',
                    parbest['log_omega0'].value)
            self.gp.set_parameter('kernel:terms[1]:log_sigma',
                    parbest['log_sigma'].value)

            mu0 = self.gp.predict(res,tp,return_cov=False,return_var=False)
            pp = mu0 + self.model.eval(parbest,t=tp)
            if detrend:
                if glint:
                    pp -= model.right.eval(parbest, t=tp)  # de-glint
                    pp /= model.left.right.eval(parbest, t=tp) # de-trend
                else: 
                    pp /= model.right.eval(parbest, t=tp) 
                ax[0].plot(tp,pp,c='saddlebrown',zorder=1)
            for i in np.linspace(0,nchain,nsamples,endpoint=False,
                    dtype=np.int):
                for j, n in enumerate(self.emcee.var_names):
                    partmp[n].value = self.emcee.chain[i,j]
                rr = flux0 - self.model.eval(partmp, t=time)
                self.gp.set_parameter('kernel:terms[0]:log_S0',
                        partmp['log_S0'].value)
                self.gp.set_parameter('kernel:terms[0]:log_Q',
                        partmp['log_Q'].value)
                self.gp.set_parameter('kernel:terms[0]:log_omega0',
                        partmp['log_omega0'].value)
                self.gp.set_parameter('kernel:terms[1]:log_sigma',
                        partmp['log_sigma'].value)
                mu = self.gp.predict(rr,tp,return_var=False,return_cov=False)
                pp = mu + self.model.eval(partmp, t=tp)
                if detrend:
                    if glint:
                        pp -= model.right.eval(partmp, t=tp)  # de-glint
                        pp /= model.left.right.eval(partmp, t=tp) # de-trend
                    else: 
                        pp /= model.right.eval(partmp, t=tp) 
                ax[0].plot(tp,pp,c='saddlebrown',zorder=1,alpha=0.1)
                
        ymin = np.min(flux-flux_err)-0.05*np.ptp(flux)
        ymax = np.max(flux+flux_err)+0.05*np.ptp(flux)
        ax[0].set_xlim(tmin, tmax)
        ax[0].set_ylim(ymin,ymax)
        ax[0].set_title(title)
        if detrend:
            if glint:
                ax[0].set_ylabel('(Flux-glint)/trend')
            else:
                ax[0].set_ylabel('Flux/trend')
        else:
            ax[0].set_ylabel('Flux')
        ax[1].plot(time,res,'o',c='skyblue',ms=2,zorder=0)
        if self.gp is not None:
            ax[1].plot(tp,mu0,c='saddlebrown', zorder=1)
        ax[1].plot([tmin,tmax],[0,0],ls=':',c='saddlebrown', zorder=1)
        if binwidth:
            t_, f_, e_, n_ = lcbin(time, res, binwidth=binwidth)
            ax[1].errorbar(t_,f_,yerr=e_,fmt='o',c='midnightblue',ms=5,zorder=2,
                    capsize=2)
        ax[1].set_xlabel('BJD-{}'.format(self.lc['bjd_ref']))
        ax[1].set_ylabel('Residual')
        ylim = np.max(np.abs(res-flux_err)+0.05*np.ptp(res))
        ax[1].set_ylim(-ylim,ylim)
        fig.tight_layout()
        return fig
        
    # ------------------------------------------------------------
    def massradius(self, m_star=None, r_star=None, K=None, q=0, 
            jovian=True, plot_kws=None, verbose=True):
        '''
        Use the results from the previous emcee/lmfit transit light curve fit
        to estimate the mass and/or radius of the planet.

        Requires that stellar properties are supplied using the keywords
        m_star and/or r_star. If only one parameter is supplied then the other
        is estimated using the stellar density derived from the transit light
        curve analysis. The planet mass can only be estimated if the the
        semi-amplitude of its orbit (in m/s) is supplied using the keyword
        argument K. See pycheops.funcs.massradius for valid formats to specify
        these parameters.

        N.B. by default, the mean stellar density calculated from the light
        curve fit is an uses the approximation q->0, where  q=m_p/m_star is
        the mass ratio. If this approximation is not valid then supply an
        estimate of the mass ratio using the keyword argment q.
        
        Output units are selected using the keyword argument jovian=True
        (Jupiter mass/radius) or jovian=False (Earth mass/radius).

        See pycheops.funcs.massradius for options available using the plot_kws
        keyword argument.
        '''

        # Generate value(s) from previous emcee sampler run
        def _v(p):
            vn = self.emcee.var_names
            chain = self.emcee.chain
            pars = self.emcee.params
            if (p in vn):
                v = chain[:,vn.index(p)]
            elif p in pars.valuesdict().keys():
                v = pars[p].value
            else:
                raise AttributeError(
                        'Parameter {} missing from dataset'.format(p))
            return v
    
        # Generate ufloat  from previous lmfit run 
        def _u(p):
            vn = self.lmfit.var_names
            pars = self.lmfit.params
            if (p in vn):
                u = ufloat(pars[p].value, pars[p].stderr)
            elif p in pars.valuesdict().keys():
                u = pars[p].value
            else:
                raise AttributeError(
                        'Parameter {} missing from dataset'.format(p))
            return u
    
        # Generate a sample of values for a parameter
        def _s(x, nm=100_000):
            if isinstance(x,float) or isinstance(x,int):
                return np.full(nm, x, dtype=np.float)
            elif isinstance(x, UFloat):
                return np.random.normal(x.n, x.s, nm)
            elif isinstance(x, np.ndarray):
                if len(x) == nm:
                    return x
                elif len(x) > nm:
                    return x[random_sample(range(len(x)), nm)]
                else:
                    return x[(np.random.random(nm)*len(x+1)).astype(int)]
            elif isinstance(x, tuple):
                if len(x) == 2:
                    return np.random.normal(x[0], x[1], nm)
                elif len(x) == 3:
                    raise NotImplementedError
            raise ValueError("Unrecognised type for parameter values")

    
        # If last fit was emcee then generate samples for derived parameters
        # not specified by the user from the chain rather than the summary
        # statistics 
        if self.__lastfit__ == 'emcee':
            k = np.sqrt(_v('D'))
            b = _v('b')
            W = _v('W')
            P = _v('P')
            aR = np.sqrt((1+k)**2-b**2)/W/np.pi
            sini = np.sqrt(1 - (b/aR)**2)
            f_c = _v('f_c')
            f_s = _v('f_s')
            ecc = f_c**2 + f_s**2
            _q = _s(q, len(self.emcee.chain))
            rho_star = rhostar(1/aR,P,_q)
            if r_star is None and m_star is not None:
                _m = _s(m_star, len(self.emcee.chain))
                r_star = (_m/rho_star)**(1/3)
            if m_star is None and r_star is not None:
                _r = _s(r_star, len(self.emcee.chain))
                m_star = rho_star*_r**3
    
        # If last fit was lmfit then extract parameter values as ufloats or, for
        # fixed parameters, as floats 
        if self.__lastfit__ == 'lmfit':
            k = usqrt(_u('D'))
            b = _u('b')
            W = _u('W')
            P = _u('P')
            aR = usqrt((1+k)**2-b**2)/W/np.pi
            sini = usqrt(1 - (b/aR)**2)
            ecc = _u('e')
            _q = ufloat(q[0], q[1]) if isinstance(q, tuple) else q
            rho_star = rhostar(1/aR, P, _q)
            if r_star is None and m_star is not None:
                if isinstance(m_star, tuple):
                    _m = ufloat(m_star[0], m_star[1])
                else:
                    _m = m_star
                r_star = (_m/rho_star)**(1/3)

        if m_star is None and r_star is not None:
            if isinstance(r_star, tuple):
                _r = ufloat(r_star[0], r_star[1])
            else:
                _r = r_star
            m_star = rho_star*_r**3
        if verbose:
            print('[[Mass/radius]]')
       
        if plot_kws is None:
            plot_kws = {}
       
        return massradius(P=P, k=k, sini=sini, ecc=ecc,
                m_star=m_star, r_star=r_star, K=K, aR=aR,
                jovian=jovian, verbose=verbose, **plot_kws)
    
    # ------------------------------------------------------------

    def planet_check(self):
        bjd = Time(self.bjd_ref+self.lc['time'][0],format='jd',scale='tdb')
        target_coo = SkyCoord(self.ra,self.dec,unit=('hour','degree'))
        print(f'BJD = {bjd}')
        print('Body     R.A.         Declination  Sep(deg)')
        print('-------------------------------------------')
        for p in ('moon','mars','jupiter','saturn','uranus','neptune'):
            c = get_body(p, bjd)
            ra = c.ra.to_string(precision=2,unit='hour',sep=':',pad=True)
            dec = c.dec.to_string(precision=1,sep=':',unit='degree',
                    alwayssign=True,pad=True)
            sep = target_coo.separation(c).degree
            print(f'{p.capitalize():8s} {ra:12s} {dec:12s} {sep:8.1f}')
        
    
    # ------------------------------------------------------------

    def rollangle_plot(self, binwidth=15, figsize=None, fontsize=11,
            title=None):
        '''
        Plot of residuals from last fit v. roll angle

        The upper panel shows the fit to the glint and/or trends v. roll angle

        The lower panel shows the residuals from the best fit.

        If a glint correction v. moon angle has been applied, this is shown in
        the middle panel.
        
        '''

        try:
            flux = np.array(self.lc['flux'])
            time = np.array(self.lc['time'])
            angle = np.array(self.lc['roll_angle'])
        except AttributeError:
            raise AttributeError("Use get_lightcurve() to load data first.")

        try:
            l = self.__lastfit__
        except AttributeError:
            raise AttributeError(
                    "Use lmfit_transit() to get best-fit parameters first.")

        # Residuals from last fit and trends due to glint and roll angle
        fit = self.emcee.bestfit if l == 'emcee' else self.lmfit.bestfit
        res = flux - fit
        params = self.emcee.params_best if l == 'emcee' else self.lmfit.params
        rolltrend = np.zeros_like(angle)
        glint = np.zeros_like(angle)
        phi = angle*np.pi/180           # radians for calculation

        # Grid of angle values for plotting smooth version of trends
        tang = np.linspace(0,360,3600)  # degrees
        tphi = tang*np.pi/180           # radians for calculation
        tr = np.zeros_like(tang)        # roll angle trend
        tg = np.zeros_like(tang)        # glint

        vd = params.valuesdict()
        vk = vd.keys()
        notrend = True
        noglint = True
        # Roll angle trend
        for n in range(1,4):
            p = "dfdsinphi" if n==1 else "dfdsin{}phi".format(n)
            if p in vk:
                notrend = False
                rolltrend += vd[p] * np.sin(n*phi)
                tr += vd[p] * np.sin(n*tphi)
            p = "dfdcosphi" if n==1 else "dfdcos{}phi".format(n)
            if p in vk:
                notrend = False
                rolltrend += vd[p] * np.cos(n*phi)
                tr += vd[p] * np.cos(n*tphi)

        if 'glint_scale' in vk:
            notrend = False
            if self.glint_moon:
                glint_theta = self.f_theta(time)
                glint = vd['glint_scale']*self.f_glint(glint_theta)
                tg = vd['glint_scale']*self.f_glint(tang)
                noglint = False
            else:
                glint_theta = (360 + angle - self.glint_angle0) % 360
                glint = vd['glint_scale']*self.f_glint(glint_theta)
                gt = (360 + tang - self.glint_angle0) % 360
                tg = vd['glint_scale']*self.f_glint(gt)

        plt.rc('font', size=fontsize)
        if notrend:
            figsize = (9,4) if figsize is None else figsize
            fig,ax=plt.subplots(nrows=1, figsize=figsize, sharex=True)
            ax.plot(angle, res, 'o',c='skyblue',ms=2)
            if binwidth:
                r_, f_, e_, n_ = lcbin(angle, res, binwidth=binwidth)
                ax.errorbar(r_,f_,yerr=e_,fmt='o',c='midnightblue',ms=5,
                    capsize=2)
            ax.set_xlim(0, 360)
            ylim = np.max(np.abs(res))+0.05*np.ptp(res)
            ax.set_ylim(-ylim,ylim)
            ax.axhline(0, color='saddlebrown',ls=':')
            ax.set_xlabel(r'Roll angle [$^{\circ}$]')
            ax.set_ylabel('Residual')
            ax.set_title(title)

        elif 'glint_scale' in vk and self.glint_moon:
            figsize = (9,8) if figsize is None else figsize
            fig,ax=plt.subplots(nrows=3, figsize=figsize)
            y = res + rolltrend 
            ax[0].plot(angle, y, 'o',c='skyblue',ms=2)
            ax[0].plot(tang, tr, c='saddlebrown')
            if binwidth:
                r_, f_, e_, n_ = lcbin(angle, y, binwidth=binwidth)
                ax[0].errorbar(r_,f_,yerr=e_,fmt='o',c='midnightblue',ms=5,
                    capsize=2)
            ax[0].set_xlabel(r'Roll angle [$^{\circ}$] (Sky)')
            ax[0].set_ylabel('Roll angle trend')
            ylim = np.max(np.abs(y))+0.05*np.ptp(y)
            ax[0].set_xlim(0, 360)
            ax[0].set_ylim(-ylim,ylim)
            ax[0].set_title(title)

            y = res + glint
            ax[1].plot(glint_theta, y, 'o',c='skyblue',ms=2)
            ax[1].plot(tang, tg, c='saddlebrown')
            if binwidth:
                r_, f_, e_, n_ = lcbin(glint_theta, y, binwidth=binwidth)
                ax[1].errorbar(r_,f_,yerr=e_,fmt='o',c='midnightblue',ms=5,
                    capsize=2)
            ylim = np.max(np.abs(y))+0.05*np.ptp(y)
            ax[1].set_xlim(0, 360)
            ax[1].set_ylim(-ylim,ylim)
            ax[1].set_xlabel(r'Roll angle [$^{\circ}$] (Moon)')
            ax[1].set_ylabel('Moon glint')

            ax[2].plot(angle, res, 'o',c='skyblue',ms=2)
            if binwidth:
                r_, f_, e_, n_ = lcbin(angle, res, binwidth=binwidth)
                ax[2].errorbar(r_,f_,yerr=e_,fmt='o',c='midnightblue',ms=5,
                    capsize=2)
            ax[2].axhline(0, color='saddlebrown',ls=':')
            ax[2].set_xlim(0, 360)
            ylim = np.max(np.abs(res))+0.05*np.ptp(res)
            ax[2].set_ylim(-ylim,ylim)
            ax[2].set_xlabel(r'Roll angle [$^{\circ}$] (Sky)')
            ax[2].set_ylabel('Residuals')

        else:

            figsize = (8,6) if figsize is None else figsize
            fig,ax=plt.subplots(nrows=2, figsize=figsize, sharex=True)
            y = res + rolltrend + glint 
            ax[0].plot(angle, y, 'o',c='skyblue',ms=2)
            ax[0].plot(tang, tr+tg, c='saddlebrown')
            if binwidth:
                r_, f_, e_, n_ = lcbin(angle, y, binwidth=binwidth)
                ax[0].errorbar(r_,f_,yerr=e_,fmt='o',c='midnightblue',ms=5,
                    capsize=2)
            if noglint:
                ax[0].set_ylabel('Roll angle trend')
            else:
                ax[0].set_ylabel('Roll angle trend + glint')
            ylim = np.max(np.abs(y))+0.05*np.ptp(y)
            ax[0].set_ylim(-ylim,ylim)
            ax[0].set_title(title)

            ax[1].plot(angle, res, 'o',c='skyblue',ms=2)
            if binwidth:
                r_, f_, e_, n_ = lcbin(angle, res, binwidth=binwidth)
                ax[1].errorbar(r_,f_,yerr=e_,fmt='o',c='midnightblue',ms=5,
                    capsize=2)
            ax[1].axhline(0, color='saddlebrown',ls=':')
            ax[1].set_xlim(0, 360)
            ylim = np.max(np.abs(res))+0.05*np.ptp(res)
            ax[1].set_ylim(-ylim,ylim)
            ax[1].set_xlabel(r'Roll angle [$^{\circ}$]')
            ax[1].set_ylabel('Residuals')
        fig.tight_layout()
        return fig
    
# ------------------------------------------------------------
    
# Data display and diagnostics

    def transit_noise_plot(self, width=3, steps=500,
            fname=None, figsize=(6,4), fontsize=11,
            requirement=None, local=False, verbose=True):

        try:
            time = np.array(self.lc['time'])
            flux = np.array(self.lc['flux'])
            flux_err = np.array(self.lc['flux_err'])
        except AttributeError:
            raise AttributeError("Use get_lightcurve() to load data first.")

        T = np.linspace(np.min(time)+width/48,np.max(time)-width/48 , steps)
        Nsc = np.zeros_like(T)
        Fsc = np.zeros_like(T)
        Nmn = np.zeros_like(T)

        for i,_t in enumerate(T):
            if local:
                j = (np.abs(time-_t) < (width/48)).nonzero()[0]
                _n,_f = transit_noise(time[j], flux[j], flux_err[j], T_0=_t,
                              width=width, method='scaled')
                _m = transit_noise(time[j], flux[j], flux_err[j], T_0=_t,
                           width=width, method='minerr')
            else:
                _n,_f = transit_noise(time, flux, flux_err, T_0=_t,
                              width=width, method='scaled')
                _m = transit_noise(time, flux, flux_err, T_0=_t,
                           width=width, method='minerr')
            if np.isfinite(_n):
                Nsc[i] = _n
                Fsc[i] = _f
            if np.isfinite(_m):
                Nmn[i] = _m

        msk = (Nsc > 0) 
        Tsc = T[msk]
        Nsc = Nsc[msk]
        Fsc = Fsc[msk]
        msk = (Nmn > 0) 
        Tmn = T[msk]
        Nmn = Nmn[msk]

        if verbose:
            print('Scaled noise method')
            print('Mean noise = {:0.1f} ppm'.format(Nsc.mean()))
            print('Min. noise = {:0.1f} ppm'.format(Nsc.min()))
            print('Max. noise = {:0.1f} ppm'.format(Nsc.max()))
            print('Mean noise scaling factor = {:0.3f} '.format(Fsc.mean()))
            print('Min. noise scaling factor = {:0.3f} '.format(Fsc.min()))
            print('Max. noise scaling factor = {:0.3f} '.format(Fsc.max()))

            print('\nMinimum error noise method')
            print('Mean noise = {:0.1f} ppm'.format(Nmn.mean()))
            print('Min. noise = {:0.1f} ppm'.format(Nmn.min()))
            print('Max. noise = {:0.1f} ppm'.format(Nmn.max()))

        plt.rc('font', size=fontsize)    
        fig,ax=plt.subplots(2,1,figsize=figsize,sharex=True)

        ax[0].set_xlim(np.min(time),np.max(time))
        ax[0].plot(time, flux,'b.',ms=1)
        ax[0].set_ylabel("Flux ")
        ylo = np.min(flux) - 0.2*np.ptp(flux)
        ypl = np.max(flux) + 0.2*np.ptp(flux)
        yhi = np.max(flux) + 0.4*np.ptp(flux)
        ax[0].set_ylim(ylo, yhi)
        ax[0].errorbar(np.median(T),ypl,xerr=width/48,
               capsize=5,color='b',ecolor='b')
        ax[1].plot(Tsc,Nsc,'b.',ms=1)
        ax[1].plot(Tmn,Nmn,'g.',ms=1)
        ax[1].set_ylabel("Transit noise [ppm] ")
        ax[1].set_xlabel("Time");
        if requirement is not None:
            ax[1].axhline(requirement, color='darkcyan',ls=':')
        fig.tight_layout()
        if fname is None:
            plt.show()
        else:
            plt.savefig(fname)
        
    #------

    def flatten(self, mask_centre, mask_width, npoly=2):
        """
        Renormalize using a polynomial fit excluding a section of the data
     
        The position and width of the mask to exclude the transit/eclipse is
        specified on the same time scale as the light curve data.

        :param mask_centre: time at the centre of the mask
        :param mask_width: full width of the mask
        :param npoly: number of terms in the normalizing polynomial

        :returns: time, flux, flux_err

        """
        time = self.lc['time']
        flux = self.lc['flux']
        flux_err = self.lc['flux_err']
        mask = abs(time-mask_centre) > mask_width/2
        n = np.polyval(np.polyfit(time[mask],flux[mask],npoly-1),time)
        self.lc['flux'] /= n
        self.lc['flux_err'] /= n

        return self.lc['time'], self.lc['flux'], self.lc['flux_err']


    #------
    def mask_data(self, mask, verbose=True):
        """
        Mask light curve data

        Replace the light curve in the dataset with a subset of the data for
        which the input mask is False.

        The orignal data are saved in lc_unmask

        """
        self.lc_unmask = copy.copy(self.lc)
        for k in self.lc:
            if isinstance(self.lc[k],np.ndarray):
                self.lc[k] = self.lc[k][~mask]
        if verbose:
            print('\nMasked {} points'.format(sum(mask)))
        return self.lc['time'], self.lc['flux'], self.lc['flux_err']

    def clip_outliers(self, clip=5, width=11, verbose=True):
        """
        Remove outliers from the light curve.

        Data more than clip*mad from a smoothed version of the light curve are
        removed where mad is the mean absolute deviation from the
        median-smoothed light curve.

        :param clip: tolerance on clipping
        :param width: width of window for median-smoothing filter

        :returns: time, flux, flux_err

        """
        flux = self.lc['flux']
        # medfilt pads the array to be filtered with zeros, so edge behaviour
        # is better if we filter flux-1 rather than flux.
        d = abs(medfilt(flux-1, width)+1-flux)
        mad = d.mean()
        ok = d < clip*mad
        for k in self.lc:
            if isinstance(self.lc[k],np.ndarray):
                self.lc[k] = self.lc[k][ok]
        if verbose:
            print('\nRejected {} points more than {:0.1f} x MAD = {:0.0f} '
                    'ppm from the median'.format(sum(~ok),clip,1e6*mad*clip))
        return self.lc['time'], self.lc['flux'], self.lc['flux_err']

    #------

    def diagnostic_plot(self, fname=None,
            figsize=(8,8), fontsize=10, compare=None):
        try:
            D = Table(self.lc['table'], masked=True)
        except AttributeError:
            raise AttributeError("Use get_lightcurve() to load data first.")

        EventMask = (D['EVENT'] > 0) & (D['EVENT'] != 100)
        D['FLUX'].mask = EventMask
        D['FLUX_BAD'] = MaskedColumn(self.lc['table']['FLUX'], 
                mask = (EventMask == False))
        D['BACKGROUND'].mask = EventMask
        D['BACKGROUND_BAD'] = MaskedColumn(self.lc['table']['BACKGROUND'],
                mask = (EventMask == False))

        tjdb = D['BJD_TIME']
        flux = D['FLUX']
        flux_bad = D['FLUX_BAD']
        flux_err = D['FLUXERR']
        back = D['BACKGROUND']
        back_bad = D['BACKGROUND_BAD']
        dark = D['DARK']
        contam = D['CONTA_LC']
        contam_err = D['CONTA_LC_ERR']
        rollangle = D['ROLL_ANGLE']
        xloc = D['LOCATION_X']
        yloc = D['LOCATION_Y']
        xcen = D['CENTROID_X']
        ycen = D['CENTROID_Y']
        
        if compare:
            time_detrend = np.array(self.lc['time'])+self.lc['bjd_ref']
            flux_detrend = np.array(self.lc['flux'])*np.nanmean(flux)
            flux_err_detrend = np.array(self.lc['flux_err'])*np.nanmean(flux)
            rollangle_detrend = np.array(self.lc['roll_angle'])
            xcen_detrend = np.array(self.lc['centroid_x'])
            ycen_detrend = np.array(self.lc['centroid_y'])
        
        plt.rc('font', size=fontsize)    
        fig, ax = plt.subplots(4,2,figsize=figsize)
        cgood = 'c'
        cbad = 'r'
        cdetrend = 'b'
        
        ylim_min, ylim_max = 0.995*np.nanmean(flux), 1.005*np.nanmean(flux)
        ax[0,0].scatter(tjdb,flux,s=2,c=cgood)
        #ax[0,0].scatter(tjdb,flux_bad,s=2,c=cbad)
        if compare:
            ax[0,0].scatter(time_detrend,flux_detrend,s=2,c=cdetrend)   
            ax[0,0].set_ylim(ylim_min,ylim_max)
        ax[0,0].set_xlabel('BJD')
        ax[0,0].set_ylabel('Flux in ADU')
        
        ax[0,1].scatter(rollangle,flux,s=2,c=cgood)
        #ax[0,1].scatter(rollangle,flux_bad,s=2,c=cbad)
        if compare:
            ax[0,1].scatter(rollangle_detrend,flux_detrend,s=2,c=cdetrend)
            ax[0,1].set_ylim(ylim_min,ylim_max)
        ax[0,1].set_xlabel('Roll angle in degrees')
        ax[0,1].set_ylabel('Flux in ADU')
        
        ax[1,0].scatter(tjdb,back,s=2,c=cgood)
        #ax[1,0].scatter(tjdb,back_bad,s=2,c=cbad)
        ax[1,0].set_xlabel('BJD')
        ax[1,0].set_ylabel('Background in ADU')
        ax[1,0].set_ylim(0.9*np.quantile(back,0.005),
                         1.1*np.quantile(back,0.995))
        
        ax[1,1].scatter(rollangle,back,s=2,c=cgood)
        #ax[1,1].scatter(rollangle,back_bad,s=2,c=cbad)
        ax[1,1].set_xlabel('Roll angle in degrees')
        ax[1,1].set_ylabel('Background in ADU')
        ax[1,1].set_ylim(0.9*np.quantile(back,0.005),
                         1.1*np.quantile(back,0.995))
        
        ax[2,0].scatter(xcen,flux,s=2,c=cgood)
        #ax[2,0].scatter(xcen,flux_bad,s=2,c=cbad)
        if compare:
            ax[2,0].scatter(xcen_detrend,flux_detrend,s=2,c=cdetrend)
            ax[2,0].set_ylim(ylim_min,ylim_max)
        ax[2,0].set_xlabel('Centroid x')
        ax[2,0].set_ylabel('Flux in ADU')
        
        ax[2,1].scatter(ycen,flux,s=2,c=cgood)
        #ax[2,1].scatter(ycen,flux_bad,s=2,c=cbad)
        if compare:
            ax[2,1].scatter(ycen_detrend,flux_detrend,s=2,c=cdetrend)
            ax[2,1].set_ylim(ylim_min,ylim_max)
        ax[2,1].set_xlabel('Centroid y')
        ax[2,1].set_ylabel('Flux in ADU')
        
        ax[3,0].scatter(contam,flux,s=2,c=cgood)
        #ax[3,0].scatter(contam,flux_bad,s=2,c=cbad)
        ax[3,0].set_xlabel('Contamination estimate')
        ax[3,0].set_ylabel('Flux in ADU')
        ax[3,0].set_xlim(np.min(contam),np.max(contam))
        
        ax[3,1].scatter(rollangle,xcen,s=2,c=cgood)
        ax[3,1].scatter(rollangle,ycen,s=2,c=cbad)
        ax[3,1].set_xlabel('Roll angle in degrees')
        ax[3,1].set_ylabel('Centroid x (cyan), y (red)')

        fig.tight_layout()
        if fname is None:
            plt.show()
        else:
            plt.savefig(fname)

    #------

    def decorr(self, dfdt=False, d2fdt2=False, dfdx=False, d2fdx2=False, 
                dfdy=False, d2fdy2=False, d2fdxdy=False, dfdsinphi=False, 
                dfdcosphi=False, dfdsin2phi=False, dfdcos2phi=False,
                dfdsin3phi=False, dfdcos3phi=False, dfdbg=False, dfdcontam=False):

        time = np.array(self.lc['time'])
        flux = np.array(self.lc['flux'])
        flux_err = np.array(self.lc['flux_err'])
        phi = self.lc['roll_angle']*np.pi/180
        sinphi = interp1d(time,np.sin(phi), fill_value=0, bounds_error=False)
        cosphi = interp1d(time,np.cos(phi), fill_value=0, bounds_error=False)

        dx = interp1d(time,self.lc['xoff'], fill_value=0, bounds_error=False)
        dy = interp1d(time,self.lc['yoff'], fill_value=0, bounds_error=False)

        model = FactorModel(
            dx = _make_interp(time, self.lc['xoff'], scale='range'),
            dy = _make_interp(time, self.lc['yoff'], scale='range'),
            sinphi = _make_interp(time,np.sin(phi)),
            cosphi = _make_interp(time,np.cos(phi)),
            bg = _make_interp(time,self.lc['bg'], scale='max'),
            contam = _make_interp(time,self.lc['contam'], scale='max'))
        params = model.make_params()
        params.add('dfdt', value=0, vary=dfdt)
        params.add('d2fdt2', value=0, vary=d2fdt2)
        params.add('dfdx', value=0, vary=dfdx)
        params.add('d2fdx2', value=0, vary=d2fdx2)
        params.add('dfdy', value=0, vary=dfdy)
        params.add('d2fdy2', value=0, vary=d2fdy2)
        params.add('d2fdxdy', value=0, vary=d2fdxdy)
        params.add('dfdsinphi', value=0, vary=dfdsinphi)
        params.add('dfdcosphi', value=0, vary=dfdcosphi)
        params.add('dfdsin2phi', value=0, vary=dfdsin2phi)
        params.add('dfdcos2phi', value=0, vary=dfdcos2phi)
        params.add('dfdsin3phi', value=0, vary=dfdsin3phi)
        params.add('dfdcos3phi', value=0, vary=dfdcos3phi)
        params.add('dfdbg', value=0, vary=dfdbg)
        params.add('dfdcontam', value=0, vary=dfdcontam)
        
        result = model.fit(flux, params, t=time)
        print("Fit Report")
        print(result.fit_report())
        result.plot()

        print("\nCompare the lightcurve RMS before and after decorrelation")
        print('RMS before = {:0.1f} ppm'.format(1e6*self.lc['flux'].std()))
        self.lc['flux'] =  flux/result.best_fit
        self.lc['flux_err'] =  flux_err/result.best_fit
        print('RMS after = {:0.1f} ppm'.format(1e6*self.lc['flux'].std()))

        flux = flux/result.best_fit
        fig,ax=plt.subplots(1,2,figsize=(8,4))
        y = 1e6*(flux-1)
        ax[0].plot(time, y,'b.',ms=1)
        ax[0].set_xlabel("BJD-{}".format((self.lc['bjd_ref'])),fontsize=12)
        ax[0].set_ylabel("Flux-1 [ppm]",fontsize=12)
        fig.suptitle('Detrended fluxes')
        n, bins, patches = ax[1].hist(y, 50, density=True, stacked=True)
        ax[1].set_xlabel("Flux-1 [ppm]",fontsize=12)
        v  = np.var(y)
        ax[1].plot(bins,np.exp(-0.5*bins**2/v)/np.sqrt(2*np.pi*v))
        fig.tight_layout()
        fig.subplots_adjust(top=0.88)
        
#-----------------------------------

    def should_I_decorr(self,cut=20,compare=False):

        cut_val = cut
        time = np.array(self.lc['time'])
        flux = np.array(self.lc['flux'])
        flux_err = np.array(self.lc['flux_err'])
        phi = self.lc['roll_angle']*np.pi/180
        sinphi = interp1d(time,np.sin(phi), fill_value=0, bounds_error=False)
        cosphi = interp1d(time,np.cos(phi), fill_value=0, bounds_error=False)
        bg = interp1d(time,self.lc['bg'], fill_value=0, bounds_error=False)
        contam = interp1d(time,self.lc['contam'], fill_value=0, bounds_error=False)
        dx = interp1d(time,self.lc['xoff'], fill_value=0, bounds_error=False)
        dy = interp1d(time,self.lc['yoff'], fill_value=0, bounds_error=False)

        dfdx_bad, dfdy_bad, dfdsinphi_bad, dfdcosphi_bad = np.array([]), np.array([]), np.array([]), np.array([])
        for dfdx in [False, True]:
            for dfdy in [False, True]:
                for dfdsinphi in [False, True]:
                    for dfdcosphi in [False, True]:

                        model = FactorModel(
                            dx = _make_interp(time, self.lc['xoff'], scale='range'),
                            dy = _make_interp(time, self.lc['yoff'], scale='range'),
                            sinphi = _make_interp(time,np.sin(phi)),
                            cosphi = _make_interp(time,np.cos(phi)),
                            bg = _make_interp(time,self.lc['bg'], scale='max'),
                            contam = _make_interp(time,self.lc['contam'], scale='max'))
                        params = model.make_params()
                        params.add('dfdt', value=0, vary=False)
                        params.add('d2fdt2', value=0, vary=False)
                        params.add('dfdx', value=0, vary=dfdx)
                        params.add('d2fdx2', value=0, vary=False)
                        params.add('dfdy', value=0, vary=dfdy)
                        params.add('d2fdy2', value=0, vary=False)
                        params.add('d2fdxdy', value=0, vary=False)
                        params.add('dfdsinphi', value=0, vary=dfdsinphi)
                        params.add('dfdcosphi', value=0, vary=dfdcosphi)
                        params.add('dfdsin2phi', value=0, vary=False)
                        params.add('dfdcos2phi', value=0, vary=False)
                        params.add('dfdsin3phi', value=0, vary=False)
                        params.add('dfdcos3phi', value=0, vary=False)
                        params.add('dfdg', value=0, vary=False)
                        params.add('dfdcontam', value=0, vary=False)

                        result = model.fit(flux, params, t=time)

                        if result.params['dfdx'].vary == True:
                            if abs(100*result.params['dfdx'].stderr/result.params['dfdx'].value) < cut_val:
                                dfdx_bad = np.append(dfdx_bad, abs(100*result.params['dfdx'].stderr/result.params['dfdx'].value))
                        if result.params['dfdy'].vary == True:
                            if abs(100*result.params['dfdy'].stderr/result.params['dfdy'].value) < cut_val:
                                dfdy_bad = np.append(dfdy_bad, abs(100*result.params['dfdy'].stderr/result.params['dfdy'].value))
                        if result.params['dfdsinphi'].vary == True:
                            if abs(100*result.params['dfdsinphi'].stderr/result.params['dfdsinphi'].value) < cut_val:
                                dfdsinphi_bad = np.append(dfdsinphi_bad, abs(100*result.params['dfdsinphi'].stderr/result.params['dfdsinphi'].value))
                        if result.params['dfdcosphi'].vary == True:
                            if abs(100*result.params['dfdcosphi'].stderr/result.params['dfdcosphi'].value) < cut_val:
                                dfdcosphi_bad = np.append(dfdcosphi_bad,
                                        abs(100*result.params['dfdcosphi'].stderr/result.params['dfdcosphi'].value))

        if len(dfdx_bad) == 0 and len(dfdy_bad) == 0 and len(dfdsinphi_bad) == 0 and len(dfdcosphi_bad) == 0:
            print("No! You don't need to decorrelate.")
        else:
            if len(dfdx_bad) > 0:
                print("Yes! Check flux against centroid x.")

            if len(dfdy_bad) > 0:
                print("Yes! Check flux against centroid y.")

            if len(dfdsinphi_bad) > 0 or len(dfdcosphi_bad) > 0:
                print("Yes! Check flux against roll angle.")

            self.diagnostic_plot(fontsize=9,compare=compare)

            decorr_check = input('Do you want to decorrelate? ')
            if decorr_check.lower()[0] == "y":
                which_decorr = input('Which to you wish to decorrelate? Please enter from the follow: centroid_x, centroid_y, and/or roll_angle. Multiple entries should be comma separated. ')
                dfdx_arg, dfdy_arg, dfdsinphi_arg, dfdcosphi_arg = False, False, False, False
                which_decorr = which_decorr.split(",")

                for index, i in enumerate(which_decorr):
                    which_decorr[index] = i.lower().replace(' ', '')
                if "centroid_x" in which_decorr:
                    dfdx_arg = True
                if "centroid_y" in which_decorr:
                    dfdy_arg = True
                if "roll_angle" in which_decorr:
                    dfdsinphi_arg, dfdcosphi_arg = True, True
                self.decorr(dfdx=dfdx_arg, dfdy=dfdy_arg,
                        dfdsinphi=dfdsinphi_arg, dfdcosphi=dfdcosphi_arg)

            elif "centroid_x" in decorr_check or "centroid_y" in decorr_check or "roll_angle" in decorr_check:
                dfdx_arg, dfdy_arg, dfdsinphi_arg, dfdcosphi_arg = False, False, False, False
                decorr_check = decorr_check.split(",")

                for index, i in enumerate(decorr_check):
                    decorr_check[index] = i.lower().replace(' ', '')
                if "centroid_x" in decorr_check:
                    dfdx_arg = True
                if "centroid_y" in decorr_check:
                    dfdy_arg = True
                if "roll_angle" in decorr_check:
                    dfdsinphi_arg, dfdcosphi_arg = True, True
                self.decorr(dfdx=dfdx_arg, dfdy=dfdy_arg,
                        dfdsinphi=dfdsinphi_arg, dfdcosphi=dfdcosphi_arg)

            else:
                print("Ok then")
        print('\n')

