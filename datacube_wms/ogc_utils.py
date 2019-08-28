from __future__ import absolute_import, division, print_function

import re
from importlib import import_module
from itertools import chain
from datetime import timedelta, datetime
from dateutil.parser import parse
from urllib.parse import urlparse
from timezonefinder import TimezoneFinder
from datacube.utils import geometry
from pytz import timezone, utc
try:
    from datacube_wms.wms_cfg_local import response_cfg
except ImportError:
    from datacube_wms.wms_cfg import response_cfg

tf = TimezoneFinder(in_memory=True)

# Use metadata time if possible as this is what WMS uses to calculate it's temporal extents
# datacube-core center time accessed through the dataset API is caluclated and may
# not agree with the metadata document
def dataset_center_time(dataset):
    center_time = dataset.center_time
    try:
        metadata_time = dataset.metadata_doc['extent']['center_dt']
        center_time = parse(metadata_time)
    except KeyError:
        pass
    return center_time


def dataset_center_coords(dataset):
    centroid = dataset.extent.centroid
    crs_geo = geometry.CRS("EPSG:4326")
    geom_centroid = centroid.to_crs(crs_geo)
    return geom_centroid.coords[0]


def local_date(ds):
    dt_utc = dataset_center_time(ds)
    dc_lon, dc_lat = dataset_center_coords(ds)
    return coord_date(dt_utc, dc_lon, dc_lat).date()


def tz_for_coord(lon, lat):
    tzn = tf.closest_timezone_at(lng=lon, lat=lat, delta_degree=9)
    if not tzn:
        print("closest tz failed with delta 9deg")
        tzn = tf.closest_timezone_at(lng=lon, lat=lat, delta_degree=15)
        if not tzn:
            raise Exception ("closest tz failed with delta 15deg")
    return timezone(tzn)


def coord_date(time, lon, lat):
    tz = tz_for_coord(lon,lat)
    return time.astimezone(tz)


def local_solar_date_range(geobox, date):
    centroid = geobox.geographic_extent.centroid
    tz = tz_for_coord(centroid.coords[0][0], centroid.coords[0][1])
    start = datetime(date.year, date.month, date.day, 0, 0, 0, tzinfo=tz)
    end = datetime(date.year, date.month, date.day, 23, 59, 59, tzinfo=tz)
    return (start.astimezone(utc), end.astimezone(utc))


def resp_headers(d):
    hdrs = {}
    hdrs.update(response_cfg)
    hdrs.update(d)
    return hdrs


def get_function(func):
    """Converts a config entry to a function, if necessary

    :param func: Either a Callable object or a fully qualified function name str, or None
    :return: a Callable object, or None
    """
    if func is not None and not callable(func):
        mod_name, func_name = func.rsplit('.', 1)
        mod = import_module(mod_name)
        func = getattr(mod, func_name)
        assert callable(func)
    return func

def parse_for_base_url(url):
    parsed = urlparse(url)
    parsed = (parsed.netloc + parsed.path).rstrip("/")
    return parsed


def get_service_base_url(allowed_urls, request_url):
    if not isinstance(allowed_urls, list):
        return allowed_urls
    parsed_request_url = parse_for_base_url(request_url)
    parsed_allowed_urls = [parse_for_base_url(u) for u in allowed_urls]
    try:
        idx = parsed_allowed_urls.index(parsed_request_url)
    except ValueError:
        idx = None
    url = allowed_urls[idx] if idx is not None else allowed_urls[0]
    # template includes tailing /, strip any trail slash here to avoid duplicates
    url = url.rstrip("/")
    return url


# Collects additional headers from flask request objects
def capture_headers(request, args_dict):
    args_dict['referer'] = request.headers.get('Referer', None)
    args_dict['origin'] = request.headers.get('Origin', None)
    args_dict['requestid'] = request.environ.get("FLASK_REQUEST_ID")
    args_dict['host'] = request.headers.get('Host', None)
    args_dict['url_root'] = request.url_root

    return args_dict

# Exceptions raised when attempting to create a
# product layer from a bad config or without correct
# product range
class ProductLayerException(Exception):
    pass


# Function wrapper for configurable functional elements

class FunctionWrapper(object):
    def __init__(self,  product_cfg, func_cfg):
        if callable(func_cfg):
            self._func = func_cfg
            self._args = []
            self._kwargs = {}
            self.product_cfg = None
        elif isinstance(func_cfg, str):
            self._func = get_function(func_cfg)
            self._args = []
            self._kwargs = {}
            self.product_cfg = None
        else:
            self._func = get_function(func_cfg["function"])
            self._args = func_cfg.get("args", [])
            self._kwargs = func_cfg.get("kwargs", {})
            if func_cfg.get("pass_product_cfg", False):
                self.product_cfg = product_cfg
            else:
                self.product_cfg = None

    def __call__(self, *args, **kwargs):
        if args and self._args:
            calling_args = chain(args, self._args)
        elif args:
            calling_args = args
        else:
            calling_args = self._args
        if kwargs and self._kwargs:
            calling_kwargs = self._kwargs.copy()
            calling_kwargs.update(kwargs)
        elif kwargs:
            calling_kwargs = kwargs
        else:
            calling_kwargs = self._kwargs

        if self.product_cfg:
            calling_kwargs["product_cfg"] = self.product_cfg


        return self._func(*calling_args, **calling_kwargs)


# Extent Mask Functions

def mask_by_val(data, band):
    return data[band] != data[band].attrs['nodata']


def mask_by_bitflag(data, band):
    return ~data[band] & data[band].attrs['nodata']


def mask_by_quality(data, band):
    return data["quality"] != 1


def mask_by_extent_flag(data, band):
    return data["extent"] == 1

# Sub-product extractors

ls8_s3_path_pattern = re.compile('L8/(?P<path>[0-9]*)')


def ls8_subproduct(ds):
    return int(ls8_s3_path_pattern.search(ds.uris[0]).group("path"))

