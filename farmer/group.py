import config as conf
from .image import BaseImage
from .utils import load_brick_position

import logging
import os
from astropy.wcs import WCS
import astropy.units as u
from astropy.nddata import Cutout2D
import numpy as np
from collections import OrderedDict


class Group(BaseImage):
    def __init__(self, group_id, image=None, imgtype='science', load=False, brick_id=None, silent=False) -> None:

        # Load the logger
        self.logger = logging.getLogger(f'farmer.group_{group_id}')
        # if silent:
        #     self.logger.setLevel(logging.ERROR)

        if load and (brick_id is not None):
            self.filename = f'G{group_id}_B{brick_id}.h5'
            self.logger.info(f'Trying to load group from {self.filename}...')
            attributes = self.read_hdf5()
            for key in attributes:
                self.__dict__[key] = attributes[key]
                self.logger.debug(f'  ... {key}')

        else:
            # Housekeeping
            self.group_id = group_id
            if image.type == 'brick':
                self.brick_id = image.brick_id
            self.bands = []
            self.wcs = {}
            self.data = {} 
            self.headers = {}
            self.properties = {}
            self.type = 'group'
            self.segmaps = {}
            self.backgrounds = {}
            self.backimg = {}
            self.rmsimg = {}
            self.back = {}
            self.rms = {}
            self.groupmaps = {}
            self.catalogs = {}
            self.n_sources = {}
            self.pixel_scales = {}
            self.model_catalog = OrderedDict()
            self.model_tracker = OrderedDict()
            self.catalog_band='detection'
            self.catalog_imgtype='science'
            self.rejected = False
            self.phot_priors = conf.PHOT_PRIORS
            self.model_priors = conf.MODEL_PRIORS
            # self.config = conf.__dict__

            # use groupmap from brick to get position and buffsize
            groupmap = image.get_image(imgtype='groupmap', band='detection')
            group_npix = np.sum(groupmap==group_id) #TODO -- save this somewhere
            if group_npix == 0:
                self.logger.warning(f'No pixels belong to group #{group_id}!')
                self.rejected = True
            else:
                try:
                    idx, idy = np.array(groupmap==group_id).nonzero()
                except:
                    raise RuntimeError(f'Cannot extract dimensions of Group #{group_id}!')
                xlo, xhi = np.min(idx), np.max(idx)
                ylo, yhi = np.min(idy), np.max(idy)
                group_width = xhi - xlo
                group_height = yhi - ylo
                xc = xlo + group_width/2.
                yc = ylo + group_height/2.

                wcs = image.get_wcs(band='detection', imgtype=imgtype)
                self.position = wcs.pixel_to_world(yc, xc)
                upper = wcs.pixel_to_world(group_height, group_width)
                lower = wcs.pixel_to_world(0, 0)
                self.size = (upper.dec - lower.dec), (lower.ra - upper.ra) * np.cos(np.deg2rad(self.position.dec.to(u.degree).value))
                self.buffsize = (self.size[0]+2*conf.GROUP_BUFFER, self.size[1]+2*conf.GROUP_BUFFER)

            self.filename = f'G{self.group_id}_B{self.brick_id}.h5'


    def get_figprefix(self, imgtype, band):
        if hasattr(self, 'brick_id'):
            return f'G{self.group_id}_B{self.brick_id}_{band}_{imgtype}'
        else:
            return f'G{self.group_id}_mosaic_{band}_{imgtype}'

    def get_bands(self):
        return np.array(self.bands)
    
    def farm(self):
        self.determine_models()
        self.force_models()
        self.plot_summary()

    def summary(self):
        print(f'Summary of group {self.group_id}')
        print(f'Located at ({self.position.ra:2.2f}, {self.position.dec:2.2f}) with size {self.size[0]:2.2f} x {self.size[1]:2.2f}')
        print(f'   (w/ buffer: {self.buffsize[0]:2.2f} x {self.buffsize[1]:2.2f})')
        print(f'Has {len(self.bands)} bands: {self.bands}')
        for band in self.bands:
            print()
            print(f' --- Data {band} ---')
            for imgtype in self.data[band].keys():
                if imgtype.startswith('psf') | (imgtype in ('groupmap', 'segmap')):
                    continue
                img = self.get_image(imgtype, band)
                if imgtype == 'weight':
                    img[img == 0] = np.nan
                tsum, mean, med, std = np.nansum(img), np.nanmean(img), np.nanmedian(img), np.nanstd(img)
                print(f'  {imgtype} ... {np.shape(img)} ( {tsum:2.2f} / {mean:2.2f} / {med:2.2f} / {std:2.2f})')
            # print(f'--- Properties {band} ---')
            for attr in self.properties[band].keys():
                print(f'  {attr} ... {self.properties[band][attr]}')

    def add_bands(self, brick, bands=None):
        if bands is None:
            bands = brick.bands
        elif np.isscalar(bands):
            bands = [bands,]
        if bands[0] != 'detection':
            bands.remove('detection')
        if ('detection' not in bands) & ('detection' not in self.data):
            bands.insert(0, 'detection')
            
        for band in bands:
        # Add band information
            self.logger.debug(f'Adopting data and properties for {band}')
            self.data[band] = {}
            self.properties[band] = {}
            self.headers[band] = {}
            self.n_sources[band] = {}
            self.catalogs[band] = {}
            self.bands.append(band)
            self.pixel_scales[band] = brick.pixel_scales[band]

            # Loop over properties
            for attr in brick.properties[band].keys():
                self.properties[band][attr] = brick.properties[band][attr]
                self.logger.debug(f'... property \"{attr}\" adopted from brick')

            # Loop over provided data
            for imgtype in brick.data[band].keys():
                if imgtype in ('science', 'weight', 'mask', 'segmap', 'groupmap', 'background', 'back', 'rms', 'model', 'residual', 'chi'):
                    fill_value = np.nan
                    if imgtype == 'mask':
                        fill_value = True
                    elif imgtype in ('segmap', 'groupmap'):
                        fill_value = 0
                    if (imgtype not in ('segmap', 'groupmap')) | ((imgtype in ('segmap', 'groupmap')) & (band == 'detection')):
                        cutout = Cutout2D(brick.data[band][imgtype].data, self.position, self.buffsize, wcs=brick.data[band][imgtype].wcs,
                                        mode='partial', fill_value = fill_value, copy=True)
                        self.logger.debug(f'... data \"{imgtype}\" subimage cut from {band} at {cutout.input_position_original}')
                        self.data[band][imgtype] = cutout
                    if imgtype in ('science', 'weight', 'mask'):
                        self.headers[band][imgtype] = brick.headers[band][imgtype] #TODO update WCS!
                    if imgtype in brick.catalogs[band].keys():
                        catalog = brick.catalogs[band][imgtype]
                        self.catalogs[band][imgtype] = catalog[catalog['group_id'] == self.group_id]
                        self.n_sources[band][imgtype] = len(self.catalogs[band][imgtype])

                    if (imgtype == 'groupmap') & (band != 'detection'):
                        groupmap = brick.data[band]['groupmap']
                        self.data[band]['groupmap'] = {}
                        self.data[band]['groupmap'][self.group_id] = groupmap[self.group_id] \
                            - np.array([cutout.origin_original[1], cutout.origin_original[0]])[:, None]
                        self.logger.debug(f'... data \"groupmap\" adopted from brick')

                    if (imgtype == 'segmap') & (band != 'detection'):
                        segmap = brick.data[band]['segmap']
                        self.data[band]['segmap'] = {}
                        for source_id in self.catalogs['detection']['science']['id']: # This is sort of hard coded...
                            self.data[band]['segmap'][source_id] = segmap[source_id] \
                                - np.array([cutout.origin_original[1], cutout.origin_original[0]])[:, None]
                        self.logger.debug(f'... data \"segmap\" adopted from brick')

                    if imgtype == 'science':
                        self.wcs[band] = cutout.wcs

                elif imgtype in ('psfcoords', 'psflist'):
                    if imgtype == 'psflist': continue # do these together!
                    self.data[band]['psfcoords'] = brick.data[band]['psfcoords'] # grab them all! groups are SMALL.
                    self.data[band]['psflist'] = brick.data[band]['psflist']
                else:
                    self.data[band][imgtype] = brick.data[band][imgtype]
                    self.logger.debug(f'... data \"{imgtype}\" adopted from brick')

            # # Clean up
            # if 'groupmap' in self.data[band].keys():
            #     ingroup = self.data[band]['groupmap'].data == self.group_id
            #     self.data[band]['mask'].data[~ingroup] = True
            #     self.data[band]['weight'].data[~ingroup] = 0
            #     self.data[band]['segmap'].data[~ingroup] = 0
            #     self.data[band]['groupmap'].data[~ingroup] = 0