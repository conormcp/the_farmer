import config as conf
import os
import logging

import numpy as np
from astropy.io import fits, ascii
from astropy.table import Table
from astropy.coordinates import SkyCoord
from scipy.ndimage import label, binary_dilation, binary_erosion, binary_fill_holes
import astropy.units as u
from astropy.wcs import WCS
from astropy.nddata import utils
from astropy.nddata import Cutout2D
from tractor.ellipses import EllipseESoft

from tractor.psfex import PixelizedPsfEx, PixelizedPSF #PsfExModel
# from tractor.psf import HybridPixelizedPSF
from tractor.galaxy import ExpGalaxy, FracDev, SoftenedFracDev
from tractor import PointSource, DevGalaxy, EllipseE, FixedCompositeGalaxy, Fluxes
from astrometry.util.util import Tan
from tractor import ConstantFitsWcs

import time
from collections import OrderedDict
from reproject import reproject_interp
from tqdm import tqdm
import h5py
from astropy.table import meta
from tractor.wcs import RaDecPos

# from concurrent.futures import ProcessPoolExecutor
from multiprocessing import Pool


def start_logger():
    print('Starting up logging system...')

    # Start the logging
    import logging.config
    logger = logging.getLogger('farmer')

    if not len(logger.handlers):
        if conf.LOGFILE_LOGGING_LEVEL is not None:
            logging_level = logging.getLevelName(conf.LOGFILE_LOGGING_LEVEL)
        else:
            logging_level = logging.DEBUG
        logger.setLevel(logging_level)
        logger.propagate = False
        formatter = logging.Formatter('[%(asctime)s] %(name)s :: %(levelname)s - %(message)s', '%H:%M:%S')

        # Logging to the console at logging level
        ch = logging.StreamHandler()
        ch.setLevel(logging.getLevelName(conf.CONSOLE_LOGGING_LEVEL))
        ch.setFormatter(formatter)
        logger.addHandler(ch)

        if (conf.LOGFILE_LOGGING_LEVEL is None) | (not os.path.exists(conf.PATH_LOGS)):
            print('Logging information wills stream only to console.\n')
            
        else:
            # create file handler which logs even debug messages
            logging_path = os.path.join(conf.PATH_LOGS, 'logfile.log')
            print(f'Logging information will stream to console and {logging_path}\n')
            # If overwrite is on, remove old logger
            if conf.OVERWRITE & os.path.exists(logging_path):
                print('WARNING -- Existing logfile will be overwritten.')
                os.remove(logging_path)

            fh = logging.FileHandler(logging_path)
            fh.setLevel(logging.getLevelName(conf.LOGFILE_LOGGING_LEVEL))
            fh.setFormatter(formatter)
            logger.addHandler(fh)

    return logger

def read_wcs(wcs, scl=1):
    t = Tan()
    t.set_crpix(wcs.wcs.crpix[0] * scl, wcs.wcs.crpix[1] * scl)
    t.set_crval(wcs.wcs.crval[0], wcs.wcs.crval[1])
    try:
        cd = wcs.wcs.pc / scl
    except:
        cd = wcs.wcs.cd / scl
    # assume your images have no rotation...
    t.set_cd(cd[0,0], cd[0,1], cd[1,0], cd[1,1])
    t.set_imagesize(wcs.array_shape[0] * scl, wcs.array_shape[1] * scl)
    wcs = ConstantFitsWcs(t)
    return wcs

# def load_brick_position(brick_id):
#     logger = logging.getLogger('farmer.load_brick_position')
#     # Do this relative to the detection image
#     ext = None
#     if 'extension' in conf.DETECTION:
#         ext = conf.DETECTION['extension']
#     wcs = WCS(fits.getheader(conf.DETECTION['science'], ext=ext))
#     nx, ny = wcs.array_shape
#     brick_width = nx / conf.N_BRICKS[0]
#     brick_height = ny / conf.N_BRICKS[1]
#     if brick_id <= 0:
#         raise RuntimeError(f'Cannot request brick #{brick_id} (<=0)!')
#     if brick_id > (nx * ny):
#         raise RuntimeError(f'Cannot request brick #{brick_id} on grid {nx} X {ny}!')
#     logger.debug(f'Using bricks of size ({brick_width:2.2f}, {brick_height:2.2f}) px, in grid {nx} X {ny} px')
#     xc = 0.5 * brick_width + int(((brick_id - 1) * brick_height) / nx) * brick_width
#     yc = 0.5 * brick_height + int(((brick_id - 1) * brick_height) % ny)
#     logger.debug(f'Brick #{brick_id} found at ({xc:2.2f}, {yc:2.2f}) px with size {brick_width:2.2f} X {brick_height:2.2f} px')
#     position = wcs.pixel_to_world(xc, yc)
#     upper = wcs.pixel_to_world(xc+brick_width/2., yc+brick_height/2.)
#     lower = wcs.pixel_to_world(xc-brick_width/2., yc-brick_height/2.)
#     size = abs(lower.ra - upper.ra) * np.cos(np.deg2rad(position.dec.to(u.degree).value)), abs(upper.dec - lower.dec)

#     logger.debug(f'Brick #{brick_id} found at ({position.ra:2.1f}, {position.dec:2.1f}) with size {size[0]:2.1f} X {size[1]:2.1f}')
#     return position, size

def load_brick_position(brick_id):
    logger = logging.getLogger('farmer.load_brick_position')
    
    ext = None
    if 'extension' in conf.DETECTION:
        ext = conf.DETECTION['extension']
    wcs = WCS(fits.getheader(conf.DETECTION['science'], ext=ext))
    ny, nx = wcs.array_shape
    
    # Number of bricks in x and y directions
    num_bricks_x = conf.N_BRICKS[0]
    num_bricks_y = conf.N_BRICKS[1]
    
    # Calculate the exact width and height of each brick
    brick_width = nx // num_bricks_x
    brick_height = ny // num_bricks_y
    
    if brick_id <= 0:
        raise RuntimeError(f'Cannot request brick #{brick_id} (<=0)!')
    if brick_id > (num_bricks_x * num_bricks_y):
        raise RuntimeError(f'Cannot request brick #{brick_id} on grid {num_bricks_x} X {num_bricks_y}!')
    
    # Calculate the row and column for this brick_id (0-indexed)
    row = (brick_id - 1) // num_bricks_x
    column = (brick_id - 1) % num_bricks_x
    
    # Calculate the center of the brick in pixel coordinates
    xc = (column * brick_width) + (brick_width / 2)
    yc = (row * brick_height) + (brick_height / 2)
    
    logger.debug(f'Brick #{brick_id} found at ({xc:2.2f}, {yc:2.2f}) px with size {brick_width} X {brick_height} px')
    
    # Calculate the center of the region in sky coordinates
    center = wcs.pixel_to_world(xc, yc)

    # Define the four corners of the pixel square
    corners_x = np.array([xc - brick_width / 2., xc + brick_width / 2.,
                          xc + brick_width / 2., xc - brick_width / 2.])
    corners_y = np.array([yc - brick_height / 2., yc - brick_height / 2.,
                          yc + brick_height / 2., yc + brick_height / 2.])

    # Convert pixel coordinates to sky coordinates
    sky_corners = wcs.pixel_to_world(corners_x, corners_y)

    # Calculate the width and height in sky coordinates
    # The RA width must be corrected for the declination
    ra_width = sky_corners[0].separation(sky_corners[1])
    dec_height = sky_corners[1].separation(sky_corners[2])

    size = (dec_height.to(u.deg), ra_width.to(u.deg))

    pixel_scale = wcs.proj_plane_pixel_scales()[0]
    buff_size = conf.BRICK_BUFFER.to(u.deg) / pixel_scale.to(u.deg)
    brick_buffwidth = brick_width + 2 * buff_size
    brick_buffheight = brick_height + 2 * buff_size

    # Define the four corners of the pixel square
    corners_x = np.array([xc - brick_buffwidth / 2., xc + brick_buffwidth / 2.,
                          xc + brick_buffwidth / 2., xc - brick_buffwidth / 2.])
    corners_y = np.array([yc - brick_buffheight / 2., yc - brick_buffheight / 2.,
                          yc + brick_buffheight / 2., yc + brick_buffheight / 2.])

    # Convert pixel coordinates to sky coordinates
    sky_corners = wcs.pixel_to_world(corners_x, corners_y)

    # Calculate the width and height in sky coordinates
    # The RA width must be corrected for the declination
    ra_width = sky_corners[0].separation(sky_corners[1])
    dec_height = sky_corners[1].separation(sky_corners[2])

    buffsize = (dec_height.to(u.deg), ra_width.to(u.deg))
    
    logger.debug(f'Brick #{brick_id} found at ({center.ra:2.1f}, {center.dec:2.1f}) with size {size[0]:2.1f} X {size[1]:2.1f}')

    return center, size, buffsize


def clean_catalog(catalog, mask, segmap=None):
    logger = logging.getLogger('farmer.clean_catalog')
    if segmap is not None:
        assert mask.shape == segmap.shape, f'Mask {mask.shape} is not the same shape as the segmentation map {segmap.shape}!'
    zero_seg = np.sum(segmap==0)
    logger.debug('Cleaning catalog...')
    tstart = time.time()

    # map the pixel coordinates to the map
    x, y = np.round(catalog['x']).astype(int), np.round(catalog['y']).astype(int)
    keep = ~mask[y, x]
    segmap[np.isin(segmap, np.argwhere(~keep)+1)] = 0
    cleancat = catalog[keep]

    # relabel segmentation map
    uniques = np.unique(segmap)
    uniques = uniques[uniques>0]
    ids = 1 + np.arange(len(cleancat))
    for (id, uni) in zip(ids, uniques):
        segmap[segmap == uni] = id


    pc = (np.sum(segmap==0) - zero_seg) / np.size(segmap)
    logger.info(f'Cleaned {np.sum(~keep)} sources ({pc*100:2.2f}% by area), {np.sum(keep)} remain. ({time.time()-tstart:2.2f}s)')
    if segmap is not None:
        return cleancat, segmap
    else:
        return cleancat

def dilate_and_group(catalog, segmap, radius=0, fill_holes=False):
    logger = logging.getLogger('farmer.identify_groups')
    """Takes the catalog and segmap and performs a dilation + grouping. ASSUMES RADIUS IN PIXELS!
    """

    # segmask
    segmask = np.where(segmap>0, 1, 0)

    # dilation
    if (radius is not None) & (radius > 0):
        logger.debug(f'Dilating segments with radius of {radius:2.2f} px')
        struct2 = create_circular_mask(2*radius, 2*radius, radius=radius)
        segmask = binary_dilation(segmask, structure=struct2).astype(int)

    if fill_holes:
        logger.debug(f'Filling holes...')
        segmask = binary_fill_holes(segmask).astype(int)

    # relabel
    groupmap, n_groups = label(segmask)

    # need to check for detached mislabels
    logger.debug('Checking for bad groups...')
    for gid in np.arange(1, n_groups):
        sids = np.unique(segmap[groupmap==gid])
        sids = sids[sids>0]
        for sid in sids:
            gids = np.unique(groupmap[segmap==sid])
            if len(gids) > 1:
                bad_gids = gids[gids!=gid]
                for bgid in bad_gids:
                    groupmap[groupmap==bgid] = gid
                    n_groups -= 1
                    logger.debug(f'  * set bad group {bgid} to owner group {gid}')

    # report back
    logger.debug(f'Found {np.max(groupmap)} groups for {np.max(segmap)} sources.')
    segid, idx = np.unique(segmap.flatten(), return_index=True)
    group_ids = groupmap.flatten()[idx[segid>0]]

    group_pops = -99 * np.ones(len(catalog), dtype=np.int16)
    for i, group_id in enumerate(group_ids):
        group_pops[i] =  np.sum(group_ids == group_id)  # np.unique with indices might be faster.
    
    __, idx_first = np.unique(group_ids, return_index=True)
    ugroup_pops, ngroup_pops = np.unique(group_pops[idx_first], return_counts=True)

    for i in np.arange(1, 5):
        if np.sum(ugroup_pops == i) == 0:
            ngroup = 0
        else:
            ngroup = ngroup_pops[ugroup_pops==i][0]
        pc = ngroup / n_groups
        logger.debug(f'... N  = {i}: {ngroup} ({pc*100:2.2f}%) ')
    ngroup = np.sum(ngroup_pops[ugroup_pops>=5])
    pc = ngroup / n_groups
    logger.debug(f'... N >= {5}: {ngroup} ({pc*100:2.2f}%) ')

    return group_ids, group_pops, groupmap

def get_fwhm(img):
    # super dirty!
    dx, dy = np.nonzero(img > np.nanmax(img)/2.)
    try:
        fwhm = np.mean([dx[-1] - dx[0], dy[-1] - dy[0]])
    except:
        fwhm = np.nan
    return np.nanmin([1.0, fwhm]) #HACK

def get_resolution(img, sig=3.):
    fwhm = get_fwhm(img)
    return np.pi * (sig / (2 * 2.5)* fwhm)**2

def validate_psfmodel(band, return_psftype=False):
    logger = logging.getLogger('farmer.validate_psfmodel')
    psfmodel_path = conf.BANDS[band]['psfmodel']

    if not os.path.exists(psfmodel_path):
        raise RuntimeError(f'PSF path for {band} does not exist!\n({psfmodel_path})')

    # maybe it's a table of ra, dec, and path_ending?
    try:
        psfgrid = Table.read(psfmodel_path)
        ra_try = ('ra', 'RA', 'ra_deg')
        dec_try = ('dec', 'DEC', 'dec_deg')

        if np.any([ra in psfgrid.colnames for ra in ra_try]) & np.any([dec in psfgrid.colnames for dec in dec_try]):
            ra_col = [ra for ra in ra_try if ra in psfgrid.colnames][0]
            dec_col = [dec for dec in dec_try if dec in psfgrid.colnames][0]

            psfgrid_ra = psfgrid[ra_col]
            psfgrid_dec = psfgrid[dec_col]
        
        else:
            raise RuntimeError(f'Could not find ra, dec columns in {psfmodel_path}!')
            
        psfcoords = SkyCoord(ra=psfgrid_ra*u.degree, dec=psfgrid_dec*u.degree)
        # I'm expecting that all of these psfnames are based in PATH_PSFMODELS
        psflist = np.array(psfgrid['filename'])
        psftype = 'variable'

    except: # better be a single file
        psfcoords = 'none'
        psflist = os.path.join(conf.PATH_PSFMODELS, psfmodel_path)
        psftype = 'constant'

    psfmodel = (psfcoords, psflist)

    # try out the first one
    if psftype == 'constant':
        fname = str(psfmodel[1][0])

        if fname.endswith('.psf'):
            try:
                PixelizedPsfEx(fn=fname)
                logger.debug(f'PSF model for {band} identified as {psftype} PixelizedPsfEx.')

            except:
                img = fits.open(fname)[0].data
                img = img.astype('float32')
                PixelizedPSF(img)
                logger.debug(f'PSF model for {band} identified as {psftype} PixelizedPSF.')
            
        elif fname.endswith('.fits'):
            img = fits.open(fname)[0].data
            img = img.astype('float32')
            PixelizedPSF(img)
            logger.debug(f'PSF model for {band} identified as {psftype} PixelizedPSF.')

    if return_psftype:
        return psfmodel, psftype
    else:
        return psfmodel


def header_from_dict(params):
    logger = logging.getLogger('farmer.header_from_dict')
    """ Take in dictionary and churn out a header. Never forget configs again. """
    hdr = fits.Header()
    total_public_entries = np.sum([ not k.startswith('__') for k in params.keys()])
    logger.debug(f'header_from_dict :: Dictionary has {total_public_entries} entires')
    tstart = time()
    for i, attr in enumerate(params.keys()):
        if not attr.startswith('__'):
            logger.debug(f'header_from_dict ::   {attr}')
            value = params[attr]
            if type(value) == str:
                # store normally
                hdr.set(f'CONF{i+1}', value, attr)
            if type(value) in (float, int):
                # store normally
                hdr.set(f'CONF{i+1}', value, attr)
            if type(value) in (list, tuple):
                # freak out.
                for j, val in enumerate(value):
                    hdr.set(f'CONF{i+1}_{j+1}', str(val), f'{attr}_{j+1}')
            
    logger.debug(f'header_from_dict :: Completed writing header ({time() - tstart:2.3f}s)')
    return hdr


def create_circular_mask(h, w, center=None, radius=None):

    if center is None: # use the middle of the image
        center = [int(w/2), int(h/2)]
    if radius is None: # use the smallest distance between the center and image walls
        radius = min(center[0], center[1], w-center[0], h-center[1])

    Y, X = np.ogrid[:h, :w]
    dist_from_center = np.sqrt((X - center[0])**2 + (Y-center[1])**2)

    mask = np.zeros((h, w), dtype=int)
    mask[dist_from_center <= radius] = 1
    return mask
    

class SimpleGalaxy(ExpGalaxy):
    '''This defines the 'SIMP' galaxy profile -- an exponential profile
    with a fixed shape of a 0.45 arcsec effective radius and spherical
    shape.  It is used to detect marginally-resolved galaxies.
    '''
    shape = EllipseE(0.45, 0., 0.)

    def __init__(self, *args):
        super(SimpleGalaxy, self).__init__(*args)

    def __str__(self):
        return (self.name + ' at ' + str(self.pos)
                + ' with ' + str(self.brightness))

    def __repr__(self):
        return (self.name + '(pos=' + repr(self.pos) +
                ', brightness=' + repr(self.brightness) + ')')

    @staticmethod
    def getNamedParams():
        return dict(pos=0, brightness=1)

    def getName(self):
        return 'SimpleGalaxy'

    ### HACK -- for Galaxy.getParamDerivatives()
    def isParamFrozen(self, pname):
        if pname == 'shape':
            return True
        return super(SimpleGalaxy, self).isParamFrozen(pname) 


def map_discontinuous(input, out_wcs, out_shape, thresh=0.1, force_simple=False):
    # print(input[0].shape)
    # print(input[1])
    # print(out_wcs)
    # print(out_shape)
    # for some resolution regimes, you cannot make a new, complete segmap with the new pixel scale!
    array, in_wcs = input
    logger = logging.getLogger('farmer.map_discontinuous')
    logger.info(f'Mapping {len(np.unique(array)[1:])} regions to new resolution...')

    scl_in = np.array([val.value for val in in_wcs.proj_plane_pixel_scales()])
    scl_out = np.array([val.value for val in out_wcs.proj_plane_pixel_scales()])

    if (array.shape == out_shape) & (np.abs(scl_in - scl_out).max() < 0.001):
        logger.debug('No reprojection needed -- same shape and pixel scale')
        segs = np.unique(array.flatten()).astype(int)
        segs = segs[segs!=0]

        outdict = {}
        logger.info('Building a mapping dictionary...')
        # Get all unique segment values and their indices at once
        y, x = np.nonzero(array)
        all_segments = array[y, x]

        # Use np.unique to split the coordinates by segment
        unique_segs, inverse = np.unique(all_segments, return_inverse=True)
        for idx, seg in enumerate(unique_segs):
            seg_indices = np.where(inverse == idx)
            outdict[seg] = (y[seg_indices], x[seg_indices])

    elif force_simple:
        logger.warning(f'Simple mapping has been forced! Small regions may be cannibalized... ')
        segs = np.unique(array.flatten()).astype(int)
        segs = segs[segs!=0]

        outdict = {}
        logger.info('Building a mapping dictionary...')
        # Get all unique segment values and their indices at once
        y, x = np.nonzero(array)
        all_segments = array[y, x]

        # Use np.unique to split the coordinates by segment
        unique_segs, inverse = np.unique(all_segments, return_inverse=True)
        for idx, seg in enumerate(unique_segs):
            seg_indices = np.where(inverse == idx)
            outdict[seg] = (y[seg_indices], x[seg_indices])

    else: # avoids cannibalizing small objects when going to lower resolution
        logger.info(f'Mapping to different resolution using full reprojection (NCPU = {conf.NCPUS})')
        outdict = parallel_process(array, out_wcs, in_wcs, n_processes=conf.NCPUS)
        # print(outdict[1247])

    return outdict


def map_ids_to_coarse_pixels(fine_pixel_data, coarse_wcs, fine_wcs, offset=0):
    """
    Map the object IDs in the fine grid to the corresponding pixels in the coarse grid.
    """
    id_to_coarse_pixel_map = {}

    # Iterate over each pixel in the fine grid
    for y in range(fine_pixel_data.shape[0]):
        for x in range(fine_pixel_data.shape[1]):
            obj_id = fine_pixel_data[y, x]
            if obj_id == 0:
                continue  # Skip background pixels
            # if obj_id == 1247:
            #     print('orig', y, x)

            # Define the corners of the fine pixel in pixel coordinates
            pixel_corners = [
                (x, y + offset),        # bottom-left
                (x + 1, y + offset),    # bottom-right
                (x, y + offset + 1),    # top-left
                (x + 1, y + offset + 1) # top-right
            ]

            # Convert pixel corners to world coordinates
            world_corners = fine_wcs.pixel_to_world_values(
                [corner[0] for corner in pixel_corners],
                [corner[1] for corner in pixel_corners]
            )

            # Convert world coordinates back to coarse grid pixel coordinates
            coarse_pixel_coords = coarse_wcs.world_to_pixel_values(
                world_corners[0],
                world_corners[1]
            )

            # Find the bounding box in the coarse grid that this fine pixel overlaps
            min_x, max_x = int(np.floor(min(coarse_pixel_coords[0]))), int(np.ceil(max(coarse_pixel_coords[0])))
            min_y, max_y = int(np.floor(min(coarse_pixel_coords[1]))), int(np.ceil(max(coarse_pixel_coords[1])))

            # Accumulate the object ID in all the overlapping coarse grid pixels
            for coarse_x in range(min_x, max_x):
                for coarse_y in range(min_y, max_y):
                    if obj_id not in id_to_coarse_pixel_map:
                        id_to_coarse_pixel_map[obj_id] = [], []
                    # if obj_id == 1247:
                    #     print('coarse', coarse_y, coarse_x)
                    id_to_coarse_pixel_map[obj_id][0].append(coarse_y)
                    id_to_coarse_pixel_map[obj_id][1].append(coarse_x)

    return id_to_coarse_pixel_map

def parallel_process(fine_pixel_data, coarse_wcs, fine_wcs, n_processes=1):
    """
    Parallelize the processing of fine grid chunks.
    """
    if n_processes < 1:
        n_processes = 1

    # Split the fine grid into chunks
    chunks = np.array_split(fine_pixel_data, n_processes)
    arrs = np.array_split(np.arange(len(fine_pixel_data)), n_processes)
    offsets = [0]
    for i, arr in enumerate(arrs[:-1]):
        offsets.append(offsets[i] + len(arr))
    # print(offsets)
    
    # # Use multiprocessing to process chunks in parallel
    with Pool(n_processes) as pool:
        results = pool.starmap(map_ids_to_coarse_pixels, [(chunk, coarse_wcs, fine_wcs, offset) for (chunk, offset) in zip(chunks, offsets)])

    # Combine results from all processes
    combined_results = {}
    for result in results:
        for obj_id, pixels in result.items():
                if obj_id in combined_results:
                    # print(combined_results[obj_id][0])
                    # print(type(combined_results[obj_id][0]))
                    # print(pixels[0])
                    combined_results[obj_id][0].extend(pixels[0])
                    combined_results[obj_id][1].extend(pixels[1])
                else:
                    combined_results[obj_id] = pixels

    # Sort the combined results by object ID and return as an OrderedDict
    sorted_combined_results = OrderedDict(sorted(combined_results.items()))
    
    return sorted_combined_results

def recursively_save_dict_contents_to_group(h5file, dic, path='/'):

    logger = logging.getLogger('farmer.hdf5')

    # argument type checking
    if not isinstance(dic, dict):
        raise ValueError("must provide a dictionary")        
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    if not isinstance(h5file, h5py._hl.files.File):
        raise ValueError("must be an open h5py file")
    # save items to the hdf5 file
    for key, item in dic.items():
        logger.debug(f'  ... {path}{key} ({type(item)})')
        key = str(key)
        if key == 'logger':
            continue
        if path not in h5file.keys():
            h5file.create_group(path)
        if isinstance(item, (list, tuple)):
            try:
                item = np.array(item)
            except:
                item = np.array([i.to(u.deg).value for i in item])
            #print(item)
        # save strings, numpy.int64, and numpy.float64 types
        if isinstance(item, (np.int32, np.int64, np.float64, str, float, np.float32, int)):
            h5file[path].attrs[key]= item
            # print(h5file[path+key].value)
            # if not h5file[path + key][...] == item:
            #     raise ValueError('The data representation in the HDF5 file does not match the original dict.')
        # save numpy arrays
        elif isinstance(item, (PointSource, SimpleGalaxy, ExpGalaxy, DevGalaxy, FixedCompositeGalaxy)):
            if key == 'variance': continue
            item.unfreezeParams()
            item.variance.unfreezeParams()
            model_params = dict(zip(item.getParamNames(), item.getParams()))
            model_params['name'] = item.name
            model_params['variance'] = dict(zip(item.variance.getParamNames(), item.variance.getParams()))
            model_params['variance']['name'] = item.name
            recursively_save_dict_contents_to_group(h5file, model_params, path + key + '/')
        elif isinstance(item, utils.Cutout2D):
            recursively_save_dict_contents_to_group(h5file, item.__dict__, path + key + '/')
        elif isinstance(item, SkyCoord):
            h5file[path].attrs[key] = item.to_string(precision=10) # overkill, but I don't care!
        elif isinstance(item, WCS):
            h5file[path].attrs[key] = item.to_header_string()
        elif isinstance(item, fits.header.Header):
            h5file[path].attrs[key] = item.tostring()
        elif isinstance(item, u.quantity.Quantity):
            h5file[path].attrs[key] = item.value
            h5file[path].attrs[key+'_unit'] = item.unit.to_string()
        elif isinstance(item, np.bool_):
            h5file[path].attrs[key] = int(item)
        elif isinstance(item, np.ndarray):
            if (key == 'bands'): # & np.isscalar(item):
                item = np.array(item).astype('|S99')
                try:
                    h5file[path].create_dataset(key, data=item)
                except:
                    item = np.array(item)
                    del h5file[path][key]
                    h5file[path].create_dataset(key, data=item)
            else:
                try:
                    try:
                        h5file[path].create_dataset(key, data=item)
                    except:
                        h5file[path][key][...] = item
                except:
                    try:
                        item = np.array(item).astype('|S99')
                        h5file[path].create_dataset(key, data=item)
                    except:
                        h5file[path][key][...] = item

        elif isinstance(item, Table):
            # item.write(h5file.filename, path=path+key, append=True, overwrite=True)
            # NOTE: Default astropy.table handler MAKES a new file... we already have one open!
            # I've copied the key parts from astropy's source code here. Could use testing.
            if any(col.info.dtype.kind == "U" for col in item.itercols()):
                item = item.copy(copy_data=False)
                item.convert_unicode_to_bytestring()

            header_yaml = meta.get_yaml_from_table(item)
            header_encoded = np.array([h.encode("utf-8") for h in header_yaml])
            if key in h5file[path]:
                del h5file[path][key]
                del h5file[path][f"{key}.__table_column_meta__"]
            h5file[path].create_dataset(key, data=item.as_array())
            h5file[path].create_dataset(f"{key}.__table_column_meta__", data=header_encoded)

 
        # save dictionaries
        elif isinstance(item, dict):
            if len(item.keys()) == 0:
                try:
                    h5file[path][key]
                except:
                    h5file[path].create_group(key) # emtpy stuff
            else:
                recursively_save_dict_contents_to_group(h5file, item, path + key + '/')
        # other types cannot be saved and will result in an error
        else:
            #print(item)
            logger.debug(f'Cannot save {key} of type {type(item)}')

def recursively_load_dict_contents_from_group(h5file, path='/', ans=None): 

    logger = logging.getLogger('farmer.hdf5')

    # return h5file[path]
    if ans is None:
        ans = {}
    if path == '/':
        for attr in h5file.attrs:
            value = h5file.attrs[attr]
            if attr == 'position':
                ans[attr] = SkyCoord(value, unit=u.deg)
            else:
                ans[attr] = value
    for key, item in h5file[path].items():
        try:
            key = int(key)
        except:
            pass
        logger.debug(f'  ... {item.name} ({type(item)})')
        if isinstance(item, h5py._hl.dataset.Dataset):
            if '.__table_column_meta__' in item.name:
                continue
            if item.shape is None:
                ans[key] = {}
            elif item[...].dtype in ('|S99', '|S9'):
                if key == 'bands':
                    ans[key] = item[...].astype(str).tolist()
                else:
                    ans[key] = item[...].astype(str)
            elif ('pixel_scales' in item.name) | (key in ('size', 'buffsize')):
                dx, dy = item[...]
                ans[key] = (dx*u.deg, dy*u.deg)
            elif ('catalogs' in item.name):
                ans[key] = Table.read(h5file.filename, item.name)
                ans[key]['ra'].unit = u.deg
                ans[key]['dec'].unit = u.deg
            else:
                ans[key] = item[...] 
        elif isinstance(item, h5py._hl.group.Group):
            ans[key] = {}
            if key == 'model_catalog':
                ans[key] = OrderedDict()
            if ('data' in item.name) & (key in ('science', 'weight', 'mask', 'segmap', 'groupmap', 'background', 'rms', 'model', 'residual', 'chi')):
                if 'data' in item.keys():
                    awcs = WCS(fits.header.Header.fromstring(item.attrs['wcs']))
                    ppix = item['input_position_cutout'][...]
                    pos = awcs.pixel_to_world(ppix[0], ppix[1])
                    ans[key] = Cutout2D(item['data'][...], pos, np.shape(item['data'][...]), wcs=awcs)
                    logger.debug(f'  ...... building cutout')
                    for key2 in item:
                        logger.debug(f'          * {key2}')
                        ans[key].__dict__[key2] = item[key2][...]
                    for key2 in item.attrs.keys():
                        logger.debug(f'          * {key2}')
                        if key2 != 'wcs':
                            ans[key].__dict__[key2] = item.attrs[key2]
                elif (('detection') not in path) & (key in ('segmap', 'groupmap')):
                    ans[key] = recursively_load_dict_contents_from_group(h5file, path + str(key) + '/', ans[key])

            elif 'variance' in item.name:
                # TODO
                continue

            elif ('model' in item.name) & ('name' in item.attrs):
                is_variance = False
                for item in (item, item['variance']):
                    name = item.attrs['name']
                    pos = RaDecPos(item.attrs['pos.ra'], item.attrs['pos.dec'])
                    fluxes = {}
                    for param in item.attrs:
                        if param.startswith('brightness'):
                            fluxes[param.split('.')[-1]] = item.attrs[param]
                    flux = Fluxes(**fluxes)
        
                    if name == 'PointSource':
                        model = PointSource(pos, flux)
                        model.name = name
                    elif name == 'SimpleGalaxy':
                        model = SimpleGalaxy(pos, flux)
                    elif name == 'ExpGalaxy':
                        shape = EllipseESoft(item.attrs['shape.logre'], item.attrs['shape.ee1'], item.attrs['shape.ee2'])
                        model = ExpGalaxy(pos, flux, shape)
                    elif name == 'DevGalaxy':
                        shape = EllipseESoft(item.attrs['shape.logre'], item.attrs['shape.ee1'], item.attrs['shape.ee2'])
                        model = DevGalaxy(pos, flux, shape)
                    elif name == 'FixedCompositeGalaxy':
                        shape_exp = EllipseESoft(item.attrs['shapeExp.logre'], item.attrs['shapeExp.ee1'], item.attrs['shapeExp.ee2'])
                        shape_dev = EllipseESoft(item.attrs['shapeDev.logre'], item.attrs['shapeDev.ee1'], item.attrs['shapeDev.ee2'])
                        model = FixedCompositeGalaxy(pos, flux, SoftenedFracDev(item.attrs['fracDev.SoftenedFracDev']), shape_exp, shape_dev)
                    
                    if not is_variance:
                        ans[key] = model
                        is_variance = True
                    else:
                        ans[key].variance = model

            else:
                ans[key] = recursively_load_dict_contents_from_group(h5file, path + str(key) + '/', ans[key])
                for key2 in item.attrs:
                    logger.debug(f'  ...... attribute: {key2}')
                    if '_unit' in key2: continue
                    value = item.attrs[key2]
                    if key2 == 'psfcoords':
                        if np.any(value == 'none'):
                            ans[key][key2] = value
                        else:
                            if np.isscalar(value):
                                value = np.array([value])
                            ra, dec = np.array([val.split() for val in value]).astype(np.float64).T
                            ans[key][key2] = SkyCoord(ra*u.deg, dec*u.deg)
                    elif 'headers' in item.name:
                        ans[key][key2] = fits.header.Header.fromstring(value)
                    elif 'wcs' in item.name:
                        ans[key][key2] = WCS(fits.header.Header.fromstring(value))
                    elif 'position' in item.name:
                        ans[key][key2] = SkyCoord(value, unit=u.deg)
                    elif (key2+'_unit' in item.attrs):
                        ans[key][key2] = value * u.Unit(item.attrs[key2+'_unit'])
                    else:
                        ans[key][key2] = value
    return ans   
     
def dcoord_to_offset(coord1, coord2, offset='arcsec', pixel_scale=None):
    if offset == 'arcsec':
        corr = np.cos(0.5*(coord1.dec.to(u.rad).value + coord2.dec.to(u.rad).value))
        dra = (corr * (coord1.ra - coord2.ra)).to(u.arcsec).value
        ddec = (coord1.dec - coord2.dec).to(u.arcsec).value
    elif offset == 'pixel':
        corr = np.cos(0.5*(coord1.dec.to(u.rad).value + coord2.dec.to(u.rad).value))
        dra = ((coord1.ra - coord2.ra) * corr / pixel_scale[0]).value
        ddec = ((coord1.dec - coord2.dec) / pixel_scale[1]).value
    return -dra, ddec

def cumulative(x):
    x = x[~np.isnan(x)]
    N = len(x)
    return np.sort(x), np.array(np.linspace(0,N,N) )/float(N)

def get_params(model):
    source = OrderedDict()

    if isinstance(model, PointSource):
        name = 'PointSource'
    else:
        name = model.name
    source['name'] = name
    source['_bands'] = np.array(list(model.getBrightness().getParamNames()))

    # position
    source['ra'] = model.pos.ra * u.deg
    source['ra_err'] = np.sqrt(model.variance.pos.ra) * u.deg
    source['dec'] = model.pos.dec * u.deg
    source['dec_err'] = np.sqrt(model.variance.pos.dec) * u.deg

    # total statistics
    for stat in model.statistics:
        if (stat not in source['_bands']) & (stat not in ('model', 'variance')):
            source[f'total_{stat}'] = model.statistics[stat]

    # shape
    if model.name == 'SimpleGalaxy': # this is stupid for stupid reasons.
        pass
    elif isinstance(model, (ExpGalaxy, DevGalaxy)):
        # if isinstance(model, ExpGalaxy):
        #     skind = '_exp'
        # elif isinstance(model, DevGalaxy):
        #     skind = '_dev'
        variance_shape = model.variance.shape
        source['logre'] = model.shape.logre # log(arcsec)
        source['logre_err'] = np.sqrt(model.variance.shape.logre)
        source['ellip'] = model.shape.e
        source['ellip_err'] = np.sqrt(model.variance.shape.e)
        source['ee1'] = model.shape.ee1
        source['ee1_err'] = np.sqrt(model.variance.shape.ee1)
        source['ee2'] = model.shape.ee2
        source['ee2_err'] = np.sqrt(model.variance.shape.ee2)

        source['theta'] = np.rad2deg(model.shape.theta) * u.deg
        source['theta_err'] = np.sqrt(np.rad2deg(model.variance.shape.theta)) * u.deg

        source[f'reff'] = np.exp(model.shape.logre) * u.arcsec # in arcsec
        source[f'reff_err'] = np.sqrt(variance_shape.logre) * source[f'reff'] * np.log(10)

        boa = (1. - np.abs(model.shape.e)) / (1. + np.abs(model.shape.e))
        if model.shape.e == 1:
            boa_sig = np.inf
        else:
            boa_sig = boa * np.sqrt(variance_shape.e) * np.sqrt((1/(1.-model.shape.e))**2 + (1/(1.+model.shape.e))**2)
        source[f'ba'] = boa
        source[f'ba_err'] = boa_sig
        
        source['pa'] = 90. * u.deg + np.rad2deg(model.shape.theta) * u.deg
        source['pa_err'] = np.rad2deg(model.variance.shape.theta) * u.deg

    elif isinstance(model, FixedCompositeGalaxy):
        source['softfracdev'] = model.fracDev.getValue()
        source['fracdev'] = model.fracDev.clipped()
        source['softfracdev_err'] = np.sqrt(model.variance.fracDev.getValue())
        source['fracdev_err'] = np.sqrt(model.variance.fracDev.clipped())
        for skind, shape, variance_shape in zip(('_exp', '_dev'), (model.shapeExp, model.shapeDev), (model.variance.shapeExp, model.variance.shapeDev)):
            source[f'logre{skind}'] = shape.logre # log(arcsec)
            source[f'logre{skind}_err'] = np.sqrt(variance_shape.logre)
            source[f'ellip{skind}'] = shape.e
            source[f'ellip{skind}_err'] = np.sqrt(variance_shape.e)
            source[f'ee1{skind}'] = shape.ee1
            source[f'ee1{skind}_err'] = np.sqrt(variance_shape.ee1)
            source[f'ee2{skind}'] = shape.ee2
            source[f'ee2{skind}_err'] = np.sqrt(variance_shape.ee2)

            source[f'theta{skind}'] = np.rad2deg(shape.theta) * u.deg
            source[f'theta{skind}_err'] = np.sqrt(np.rad2deg(variance_shape.theta)) * u.deg

            source[f'reff{skind}'] = np.exp(shape.logre) * u.arcsec # in arcsec
            source[f'reff{skind}_err'] = np.sqrt(variance_shape.logre) * source[f'reff{skind}'] * np.log(10)

            boa = (1. - np.abs(shape.e)) / (1. + np.abs(shape.e))
            if shape.e == 1:
                boa_sig = np.inf
            else:
                boa_sig = boa * np.sqrt(variance_shape.e) * np.sqrt((1/(1.-shape.e))**2 + (1/(1.+shape.e))**2)
            source[f'ba{skind}'] = boa
            source[f'ba{skind}_err'] = boa_sig
            
            source[f'pa{skind}'] = 90. * u.deg + np.rad2deg(shape.theta) * u.deg
            source[f'pa{skind}_err'] = np.rad2deg(variance_shape.theta) * u.deg


    for band in source['_bands']:

        # photometry
        flux_err = np.sqrt(model.variance.getBrightness().getFlux(band))
        mask = ((flux_err > 0) & np.isfinite(flux_err)).astype(np.int8)
        source[f'{band}_flux'] = model.getBrightness().getFlux(band) * mask
        source[f'{band}_flux_err'] = flux_err * mask
        
        source[f'_{band}_zpt'] = conf.BANDS[band]['zeropoint']

        source[f'{band}_flux_ujy'] = source[f'{band}_flux'] * 10**(-0.4 * (source[f'_{band}_zpt'] - 23.9)) * u.microjansky * mask
        source[f'{band}_flux_ujy_err'] = source[f'{band}_flux_err'] * 10**(-0.4 * (source[f'_{band}_zpt'] - 23.9)) * u.microjansky * mask

        source[f'{band}_mag'] = -2.5 * np.log10(source[f'{band}_flux']) * u.mag + source[f'_{band}_zpt'] * u.mag * mask
        source[f'{band}_mag_err'] = 2.5 * np.log10(np.e) / (source[f'{band}_flux'] / source[f'{band}_flux_err']) * mask

        # statistics
        if band in model.statistics:
            for stat in model.statistics[band]:
                source[f'{band}_{stat}'] = model.statistics[band][stat]

    return source

def set_priors(model, priors):
    logger = logging.getLogger('farmer.priors')

    if priors is None:
        logger.warning('I was asked to set priors but I have none!')
        return model
        
    params = model.getNamedParams()
    for name in params:
        idx = params[name]
        if name == 'pos':
            if 'pos' in priors:
                if priors['pos'] in ('fix', 'freeze'):
                    model[idx].freezeAllParams()
                    logger.debug('Froze position')
                elif priors['pos'] != 'none':
                    sigma = priors['pos'].to(u.deg).value
                    psigma = priors['pos'].to(u.arcsec)
                    model[idx].addGaussianPrior('ra', mu=model[idx][0], sigma=sigma)
                    model[idx].addGaussianPrior('dec', mu=model[idx][1], sigma=sigma)
                    logger.debug(f'Set positon prior +/- {psigma}')
                else:
                    logger.debug('Position is free to vary')

        elif name == 'fracDev':
            if 'fracDev' in priors:
                if priors['fracDev'] in ('fix', 'freeze'):
                    model.freezeParam(idx)
                    logger.debug(f'Froze {name}')
                    # params = model[idx].getParamNames()
                    # for i, param in enumerate(params):
                    #     model[idx].freezeParam(i)
                    #     logger.debug(f'Froze {param}')
                else:
                    logger.debug('fracDev is free to vary') 

        elif name in ('shape', 'shapeDev', 'shapeExp'):
            if 'shape' in priors:
                if priors['shape'] in ('fix', 'freeze'):
                    sparams = model[idx].getParamNames()
                    for i, param in enumerate(sparams):
                        if i != 0: # leave reff alone
                            model[idx].freezeParam(i)
                            logger.debug(f'Froze {param}')
                else:
                    logger.debug(f'{name} is free to vary')   

            if 'reff' in priors:
                if priors['reff'] in ('fix', 'freeze'):
                    model[idx].freezeParam(0)
                    logger.debug(f'Froze {name} radius')
                elif priors['reff'] != 'none':
                    sigma = np.log(priors['reff'].to(u.arcsec).value)
                    psigma = priors['reff'].to(u.arcsec)
                    model[idx].addGaussianPrior('logre', mu=model[idx][0], sigma=sigma)    
                    logger.debug(f'Set {name} radius prior +/- {psigma}')   
                else:
                    logger.debug(f'{name} radius is free to vary')           
    return model

def get_detection_kernel(filter_kernel):
    kernel_kwargs = {}
    # if string, grab from config
    if isinstance(filter_kernel, str):
        dirname = os.path.dirname(__file__)
        filename = os.path.join(dirname, '../config/conv_filters/'+conf.FILTER_KERNEL)
        if os.path.exists(filename):
            convfilt = np.array(np.array(ascii.read(filename, data_start=1)).tolist())
        else:
            raise FileExistsError(f"Convolution file at {filename} does not exist!")
        return convfilt

    elif np.isscalar(filter_kernel):
        from astropy.convolution import Gaussian2DKernel
        # else, assume FWHM in pixels
        kernel_kwargs['x_stddev'] = filter_kernel/2.35
        kernel_kwargs['factor']=1
        convfilt = np.array(Gaussian2DKernel(**kernel_kwargs))
        return convfilt
    
    else:
        raise RuntimeError(f'Requested kernel {filter_kernel} not understood!')

def build_regions(catalog, pixel_scale, outpath='objects.reg', scale_factor=2.0):
    from regions import EllipseSkyRegion, Regions
    detcoords = SkyCoord(catalog['ra'], catalog['dec'])
    regs = []
    for coord, obj in tqdm(zip(detcoords, catalog), total=len(catalog)):
        width = scale_factor * 2 * obj['a'] * pixel_scale
        height = scale_factor * 2 * obj['b'] * pixel_scale
        angle = np.rad2deg(obj['theta']) * u.deg
        objid = str(obj['id'])
        regs.append(EllipseSkyRegion(coord, width, height, angle, meta={'text':objid}))
    bigreg = Regions(regs)
    bigreg.write(outpath, overwrite=True, format='ds9')


def _clear_h5():
    import gc
    for obj in gc.get_objects():   # Browse through ALL objects
        if isinstance(obj, h5py.File):   # Just HDF5 files
            try:
                obj.close()
            except:
                pass # Was already closed


def prepare_psf(filename, outfilename=None, pixel_scale=None, mask_radius=None, clip_radius=None, norm=None, norm_radius=None, target_pixel_scale=None, ext=0):
    # NOTE: THESE INPUTS NEED ASTROPY UNITS!

    hdul = fits.open(filename)
    psfmodel = hdul[ext].data

    if pixel_scale is None:
        w = WCS(hdul[ext].header)
        pixel_scale = w.proj_plane_pixel_scales[0]
        hdul[ext].header.update(w.wcs.to_header())

    if target_pixel_scale is not None:
        from scipy.ndimage import zoom
        zoom_factor = pixel_scale / target_pixel_scale
        psfmodel = zoom(psfmodel, zoom_factor.value, order=1)
        print(f'Resampled image from {pixel_scale}/pixel to {target_pixel_scale}/pixel')

    # estimate plateau
    if mask_radius is not None:
        pw, ph = np.shape(psfmodel)
        cmask = create_circular_mask(pw, ph, radius=mask_radius / pixel_scale)
        bcmask = ~cmask.astype(bool) & (psfmodel > 0)
        back_level = np.nanpercentile(psfmodel[bcmask], q=95)
        print(f'Subtracted back level of {back_level} based on {np.sum(bcmask)} pixels outside {mask_radius}')
        psfmodel -= back_level
        psfmodel[(psfmodel < 0) | np.isnan(psfmodel)] = 1e-31

    if clip_radius is not None:
        pw, ph = np.shape(psfmodel)
        psf_rad_pix = int(clip_radius / pixel_scale)
        if psf_rad_pix%2 == 0:
            psf_rad_pix += 0.5
        print(f'Clipping PSF ({psf_rad_pix}px radius)')
        psfmodel = psfmodel[int(pw/2.-psf_rad_pix):int(pw/2+psf_rad_pix), int(ph/2.-psf_rad_pix):int(ph/2+psf_rad_pix)]
        print(f'New shape: {np.shape(psfmodel)}')
        
    if norm is not None:
        print(f'Normalizing PSF to {norm:4.4f} within {norm_radius} radius circle')
        norm_radpix = norm_radius / pixel_scale
        pw, ph = np.shape(psfmodel)
        cmask = create_circular_mask(pw, ph, radius=norm_radpix).astype(bool)
        psfmodel *= norm / np.sum(psfmodel[cmask])

    if outfilename is None:
        outfilename = filename

    hdul[ext].data = psfmodel
    hdul.writeto(outfilename)
    print(f'Wrote updated PSF to {outfilename}')

    return psfmodel

def run_group(group, mode='all'):

    if not group.rejected:
    
        if mode == 'all':
            status = group.determine_models()
            if status:
                status = group.force_models()
    
        elif mode == 'model':
            status = group.determine_models()

        elif mode == 'photometry':
            status = group.force_models()

        elif mode == 'pass':
            status = False

        # if not status:
        #     group.rejected = True

    # else:
    #     self.logger.warning(f'Group {group.group_id} has been rejected!')
    output = group.group_id.copy(), group.model_catalog.copy(), group.model_tracker.copy()
    del group
    return output

    # return group

