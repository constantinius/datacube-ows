from __future__ import absolute_import, division, print_function

try:
    from datacube_wms.wms_cfg_local import layer_cfg
except ImportError:
    from datacube_wms.wms_cfg import layer_cfg
try:
    from datacube_wms.wms_cfg_local import service_cfg
except ImportError:
    from datacube_wms.wms_cfg import service_cfg

from collections.abc import Mapping

from datacube_wms.cube_pool import cube
from datacube_wms.band_mapper import StyleDef
from datacube_wms.ogc_utils import get_function, ProductLayerException, FunctionWrapper

import logging

_LOG = logging.getLogger(__name__)


def accum_min(a, b):
    if a is None:
        return b
    elif b is None:
        return a
    else:
        return min(a, b)


def accum_max(a, b):
    if a is None:
        return b
    elif b is None:
        return a
    else:
        return max(a, b)


class BandIndex(object):
    def __init__(self, product, band_cfg, dc):
        self.product = product
        self.product_name = product.name
        self.native_bands = dc.list_measurements().loc[self.product_name]
        if band_cfg is None:
            self.band_cfg = {}
            for b in self.native_bands.index:
                self.band_cfg[b] = []
        else:
            self.band_cfg = band_cfg
        self._idx = {}
        self._nodata_vals = {}
        for b, aliases in self.band_cfg.items():
            if b not in self.native_bands.index:
                raise ProductLayerException(f"Unknown band: {b}")
            if b in self._idx:
                raise ProductLayerException(f"Duplicate band name/alias: {b}")
            self._idx[b] = b
            for a in aliases:
                if a in self._idx:
                    raise ProductLayerException(f"Duplicate band name/alias: {a}")
                self._idx[a] = b
            self._nodata_vals[b] = self.native_bands['nodata'][b]

    def band(self, name_alias):
        if name_alias not in self._idx:
            raise ProductLayerException(f"Unknown band name/alias: {name_alias}")
        return self._idx[name_alias]

    def band_label(self, name_alias):
        name = self.band(name_alias)
        if self.band_cfg[name]:
            return self.band_cfg[name][0]
        else:
            return name

    def nodata_val(self, name_alias):
        name = self.band(name_alias)
        return self._nodata_vals[name]

    def band_labels(self):
        return [self.band_label(b) for b in self.native_bands.index if b in self.band_cfg]

    def band_nodata_vals(self):
        return [self.nodata_val(b) for b in self.native_bands.index if b in self.band_cfg]


class AttributionCfg(object):
    def __init__(self, cfg):
        self.title = cfg.get("title")
        self.url = cfg.get("url")
        logo = cfg.get("logo")
        if not logo:
            self.logo_width = None
            self.logo_height = None
            self.logo_url = None
            self.logo_fmt = None
        else:
            self.logo_width = logo.get("width")
            self.logo_height = logo.get("height")
            self.logo_url = logo.get("url")
            self.logo_fmt = logo.get("format")

    @classmethod
    def parse(cls, cfg):
        if not cfg:
            return None
        else:
            return cls(cfg)


class SuppURL(object):
    @classmethod
    def parse_list(cls, cfg):
        if not cfg:
            return []
        return [ cls(u) for u in cfg ]

    def __init__(self, cfg):
        self.url = cfg["url"]
        self.format = cfg["format"]


class ProductLayerDef(object):
    # pylint: disable=invalid-name, too-many-instance-attributes, bare-except, too-many-statements
    def __init__(self, product_cfg, platform_def, dc):
        self.platform = platform_def
        self.name = product_cfg["name"]
        self.multi_product = product_cfg.get("multi_product", False)
        if self.multi_product:
            self.product_names = product_cfg["product_name"]
            self.product_name = product_cfg["product_name"][0]
        else:
            self.product_name = product_cfg["product_name"]
            self.product_names = [ self.product_name ]
        for prod_name in self.product_names:
            if "__" in prod_name:
                raise ProductLayerException("Product names cannot have a double underscore '__' in them.")
        self.product_label = product_cfg["label"]
        self.product_type = product_cfg["type"]
        self.product_variant = product_cfg["variant"]
        self.products = []
        for pn in self.product_names:
            product = dc.index.products.get_by_name(pn)
            if product is None:
                raise ProductLayerException(f"Could not find product {pn} in datacube")
            self.products.append(product)
        self.product = self.products[0]
        self.band_idx = BandIndex(self.product, product_cfg.get("bands"), dc)
        self.definition = self.product.definition
        self.abstract = product_cfg["abstract"] if "abstract" in product_cfg else self.definition['description']
        self.title = "%s %s %s (%s)" % (platform_def.title,
                                        self.product_variant,
                                        self.product_type,
                                        self.product_label)
        from datacube_wms.product_ranges import get_ranges, get_sub_ranges, merge_ranges
        self.ranges = get_ranges(dc, self)
        if self.ranges is None:
            if self.multi_product:
                _LOG.warning("Warning: Ranges for multi-product %s not yet in database", self.name)
            else:
                _LOG.warning("Could not find ranges for %s in database", self.product_name)
        # TODO: subranges not supported with multi-product
        self.sub_ranges = get_sub_ranges(dc, self)
        if self.multi_product:
            self.pq_names = product_cfg.get("pq_dataset")
        else:
            self.pq_names = [ product_cfg.get("pq_dataset") ]
        self.pq_name = self.pq_names[0] if self.pq_names is not None and len(self.pq_names) > 0 else None
        self.pq_band = product_cfg.get("pq_band")

        self.min_zoom = product_cfg.get("min_zoom_factor", 300.0)
        self.max_datasets_wms = product_cfg.get("max_datasets_wms", 0)
        self.zoom_fill = product_cfg.get("zoomed_out_fill_colour", [150, 180, 200])
        self.ignore_flags_info = product_cfg.get("ignore_flags_info", [])
        self.feature_info_include_utc_dates = product_cfg.get("feature_info_include_utc_dates", False)
        self.feature_info_include_custom = product_cfg.get("feature_info_include_custom", None)
        raw_always_fetch_bands = product_cfg.get("always_fetch_bands", [])
        self.always_fetch_bands = []
        for b in raw_always_fetch_bands:
            self.always_fetch_bands.append(self.band_idx.band(b))
        self.data_manual_merge = product_cfg.get("data_manual_merge", False)
        self.solar_correction = product_cfg.get("apply_solar_corrections", False)
        if "sub_product_extractor" in product_cfg:
            self.sub_product_extractor = FunctionWrapper(self, product_cfg["sub_product_extractor"])
        else:
            self.sub_product_extractor = None
        self.sub_product_label = product_cfg.get("sub_product_label", None)

        self.pq_products = []
        if self.pq_names:
            for pqn in self.pq_names:
                if pqn is not None:
                    pq_product = dc.index.products.get_by_name(pqn)
                    if pq_product is None:
                        raise ProductLayerException(f"Could not find pq_product {pqn} for {self.name} in database")
                    self.pq_products.append(pq_product)
        self.info_mask = ~0
        if self.pq_products:
            self.pq_product = self.pq_products[0]
            fd = self.pq_product.measurements[self.pq_band]["flags_definition"]
            for bitname in self.ignore_flags_info:
                bit = fd[bitname]["bits"]
                if not isinstance(bit, int):
                    continue
                flag = 1 << bit
                self.info_mask &= ~flag
        else:
            self.pq_product = None

        self.legend = product_cfg.get("legend", None)
        self.styles = product_cfg["styles"]
        self.default_style = product_cfg["default_style"]
        self.style_index = {s["name"]: StyleDef(self, s) for s in self.styles}

        if (isinstance(product_cfg["extent_mask_func"], Mapping)
                or callable(product_cfg["extent_mask_func"])
                or isinstance(product_cfg["extent_mask_func"], str)
        ):
            # Single extent mask function.
            self.extent_mask_func = [FunctionWrapper(self, product_cfg["extent_mask_func"])]
        else:
            # Multiple extent mask functions.
            self.extent_mask_func = list([FunctionWrapper(self, f_cfg) for f_cfg in product_cfg["extent_mask_func"]])
        if "fuse_func" in product_cfg:
            self.fuse_func = FunctionWrapper(self, product_cfg["fuse_func"])
        else:
            self.fuse_func = None
        if "pq_fuse_func" in product_cfg:
            self.pq_fuse_func = FunctionWrapper(self, product_cfg["pq_fuse_func"])
        else:
            self.pq_fuse_func = None
        self.pq_manual_merge = product_cfg.get("pq_manual_merge", False)
        self.pq_ignore_time = product_cfg.get("pq_ignore_time", False)

        self.attribution = AttributionCfg.parse(product_cfg.get("attribution"))
        if not self.attribution:
            self.attribution = platform_def.attribution
        self.identifiers = product_cfg.get("identifiers", {})
        svc_cfg = get_service_cfg()

        for auth in self.identifiers.keys():
            if auth not in svc_cfg.authorities:
                raise ProductLayerException("Identifier with non-declared authority: %s" % repr(auth))

        self.feature_list_urls = SuppURL.parse_list(product_cfg.get("feature_list_urls"))
        self.data_urls = SuppURL.parse_list(product_cfg.get("data_urls"))

        # For WCS
        if svc_cfg.wcs:
            try:
                self.native_CRS = self.product.definition["storage"]["crs"]
            except KeyError:
                self.native_CRS = None
            if not self.native_CRS:
                self.native_CRS = product_cfg.get("native_wcs_crs")
            if self.native_CRS not in svc_cfg.published_CRSs:
                logging.warning("Native CRS for product %s (%s) not in published CRSs", self.product_name,
                                self.native_CRS)
                self.native_CRS = None
            if self.native_CRS:
                self.native_CRS_def = svc_cfg.published_CRSs[self.native_CRS]
            if svc_cfg.create_grid and not svc_cfg.dummy_grid and self.native_CRS:
                data = dc.load(self.product_name, dask_chunks={})
                self.grid_high_x = len(data[svc_cfg.published_CRSs[self.native_CRS]["horizontal_coord"]])
                self.grid_high_y = len(data[svc_cfg.published_CRSs[self.native_CRS]["vertical_coord"]])
                self.origin_x = data.affine[3]
                self.origin_y = data.affine[5]
                self.resolution_x = data.affine[0]
                self.resolution_y = data.affine[4]
            elif not svc_cfg.dummy_grid and self.native_CRS and self.ranges is not None:
                native_bounding_box = self.bboxes[self.native_CRS]
                self.origin_x = native_bounding_box["left"]
                self.origin_y = native_bounding_box["bottom"]
                self.resolution_x, self.resolution_y = product_cfg.get("native_wcs_resolution", [25.0, 25.0])
                self.grid_high_x = int((native_bounding_box["right"] - native_bounding_box["left"]) / self.resolution_x)
                self.grid_high_y = int((native_bounding_box["top"] - native_bounding_box["bottom"]) / self.resolution_y)
            self.max_datasets_wcs = product_cfg.get("max_datasets_wcs", 0)
            bands = dc.list_measurements().loc[self.product_name]
            self.bands = bands.index.values
            self.nodata_values = bands['nodata'].values
            self.nodata_dict = {a: b for a, b in zip(self.bands, self.nodata_values)}
            self.wcs_sole_time = product_cfg.get("wcs_sole_time")
            self.wcs_default_bands = [self.band_idx.band(b) for b in product_cfg["wcs_default_bands"]]

    @property
    def bboxes(self):
        if self.ranges is None:
            return dict()
        return {
            crs_id: {"right": bbox["bottom"],
                     "left": bbox["top"],
                     "top": bbox["left"],
                     "bottom": bbox["right"]
                     } \
                if service_cfg["published_CRSs"][crs_id].get("vertical_coord_first") \
                else \
                {"right": bbox["right"],
                 "left": bbox["left"],
                 "top": bbox["top"],
                 "bottom": bbox["bottom"]
                 }
            for crs_id, bbox in self.ranges["bboxes"].items()
        }


class PlatformLayerDef(object):
    def __init__(self, platform_cfg, layer_defs, dc=None):
        self.name = platform_cfg["name"]
        self.title = platform_cfg["title"]
        self.abstract = platform_cfg["abstract"]

        self.attribution = AttributionCfg.parse(platform_cfg.get("attribution"))
        if not self.attribution:
            self.attribution = layer_defs.attribution
        self.products = []
        for prod_cfg in platform_cfg["products"]:
            try:
                prod = ProductLayerDef(prod_cfg, self, dc=dc)
                self.products.append(prod)
                layer_defs.product_index[prod.name] = prod
            except ProductLayerException as e:
                _LOG.error("Could not load layer: %s", str(e))
        self.valid_products = [product for product in self.products if product.ranges]


class LayerDefs(object):
    _instance = None
    initialised = False

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(LayerDefs, cls).__new__(cls)
        return cls._instance

    def __init__(self, platforms_cfg, refresh=False):
        if not self.initialised or refresh:
            self.initialised = True
            self.platforms = []
            self.platform_index = {}
            self.product_index = {}
            svc_cfg = get_service_cfg()
            self.attribution = svc_cfg.attribution
            with cube() as dc:
                for platform_cfg in platforms_cfg:
                    platform = PlatformLayerDef(platform_cfg, self, dc=dc)
                    self.platforms.append(platform)
                    self.platform_index[platform.name] = platform

    def __iter__(self):
        for p in self.platforms:
            yield p

    def __getitem__(self, name):
        if isinstance(name, int):
            return self.platforms[name]
        else:
            return self.platform_index[name]


def get_layers(refresh=False):
    return LayerDefs(layer_cfg, refresh)


class ServiceCfg(object):
    # pylint: disable=invalid-name, too-many-instance-attributes, too-many-branches
    _instance = None
    initialised = False

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ServiceCfg, cls).__new__(cls)
        return cls._instance

    def __init__(self, srv_cfg, refresh=False):
        if not self.initialised or refresh:
            self.initialised = True

            self.wms = srv_cfg.get("wms", True)
            self.wcs = srv_cfg.get("wcs", False)
            self.wmts = srv_cfg.get("wmts", False)
            self.create_grid = srv_cfg.get("create_wcs_grid", False)
            self.dummy_grid = srv_cfg.get("dummy_wcs_grid", False)
            self.use_default_extent = srv_cfg.get("use_default_extent", False)

            self.title = srv_cfg["title"]
            self.url = srv_cfg["url"]
            self.s3_bucket = srv_cfg.get("s3_bucket", "")
            self.s3_url = srv_cfg.get("s3_url", "")
            # For services that can be accessed through
            # multiple domain names
            self.allowed_urls = self.url
            if isinstance(self.url, list):
                self.url = self.allowed_urls[0]
            if not self.url.startswith("http"):
                raise Exception("URL in service_cfg does not start with http or https.")
            self.human_url = srv_cfg.get("human_url", self.url)
            self.published_CRSs = {}
            geo_crs = False
            for crs_str, crsdef in srv_cfg["published_CRSs"].items():
                if crs_str.startswith("EPSG:"):
                    gml_name = "http://www.opengis.net/def/crs/EPSG/0/" + crs_str[5:]
                else:
                    gml_name = crs_str
                self.published_CRSs[crs_str] = {
                    "geographic": crsdef["geographic"],
                    "horizontal_coord": crsdef.get("horizontal_coord", "longitude"),
                    "vertical_coord": crsdef.get("vertical_coord", "latitude"),
                    "vertical_coord_first": crsdef.get("vertical_coord_first", False),
                    "gml_name": gml_name
                }
                if self.published_CRSs[crs_str]["geographic"]:
                    geo_crs = True
                    if self.published_CRSs[crs_str]["horizontal_coord"] != "longitude":
                        raise Exception("Published CRS {} is geographic"
                                        "but has a horizontal coordinate that is not 'longitude'".format(crs_str))
                    if self.published_CRSs[crs_str]["vertical_coord"] != "latitude":
                        raise Exception("Published CRS {} is geographic"
                                        "but has a vertical coordinate that is not 'latitude'".format(crs_str))

            if not geo_crs:
                raise Exception("At least one published CRS must be geographic.  (Suggest EPSG:4326)")
            if self.wcs:
                self.default_geographic_CRS = srv_cfg["default_geographic_CRS"]
                if self.default_geographic_CRS not in self.published_CRSs:
                    raise Exception("Configured default geographic CRS not listed in published CRSs.")
                if not self.published_CRSs[self.default_geographic_CRS]["geographic"]:
                    raise Exception("Configured default geographic CRS not listed in published CRSs as geographic.")
                self.default_geographic_CRS_def = self.published_CRSs[self.default_geographic_CRS]
                self.wcs_formats = {}
                for fmt_name, fmt in srv_cfg["wcs_formats"].items():
                    self.wcs_formats[fmt_name] = {
                        "mime": fmt["mime"],
                        "extension": fmt["extension"],
                        "multi-time": fmt["multi-time"],
                        "name": fmt_name,
                    }
                    self.wcs_formats[fmt_name]["renderer"] = get_function(fmt["renderer"])
                if not self.wcs_formats:
                    raise Exception("Must configure at least one wcs format to support WCS.")

                self.native_wcs_format = srv_cfg["native_wcs_format"]
                if self.native_wcs_format not in self.wcs_formats:
                    raise Exception("Configured native WCS format not a supported format.")
            else:
                self.default_geographic_CRS = None
                self.default_geographic_CRS_def = {}
                self.wcs_formats = {}
                self.native_wcs_format = None

            # WMS specific config
            self.layer_limit = 1
            self.max_width = srv_cfg.get("max_width", 256)
            self.max_height = srv_cfg.get("max_height", 256)

            self.abstract = srv_cfg.get("abstract")
            self.keywords = srv_cfg.get("keywords", [])
            self.contact_info = srv_cfg.get("contact_info", {})
            self.fees = srv_cfg.get("fees", "")
            self.access_constraints = srv_cfg.get("access_constraints", "")

            self.attribution = AttributionCfg.parse(srv_cfg.get("attribution"))
            self.authorities = srv_cfg.get("authorities", {})

    def __getitem__(self, name):
        return getattr(self, name)


def get_service_cfg(refresh=False):
    return ServiceCfg(service_cfg, refresh)
