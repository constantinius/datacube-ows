from __future__ import absolute_import, division, print_function

from flask import render_template

from datacube_wms.ogc_utils import resp_headers, get_service_base_url

from datacube_wms.ogc_exceptions import WCS1Exception
from datacube_wms.wcs_utils import WCS1GetCoverageRequest, get_coverage_data

from datacube_wms.wms_layers import get_layers, get_service_cfg

from datacube_wms.utils import log_call, opencensus_trace_call, get_opencensus_tracer


WCS_REQUESTS = ("DESCRIBECOVERAGE", "GETCOVERAGE")

tracer = get_opencensus_tracer()

@log_call
@opencensus_trace_call(tracer=tracer)
def handle_wcs(nocase_args):
    operation = nocase_args.get("request", "").upper()
    if not operation:
        raise WCS1Exception("No operation specified", locator="Request parameter")
    elif operation == "GETCAPABILITIES":
        return get_capabilities(nocase_args)
    elif operation == "DESCRIBECOVERAGE":
        return desc_coverages(nocase_args)
    elif operation == "GETCOVERAGE":
        return get_coverage(nocase_args)
    else:
        raise WCS1Exception("Unrecognised operation: %s" % operation, locator="Request parameter")


@log_call
@opencensus_trace_call(tracer=tracer)
def get_capabilities(args):
    # TODO: Handle updatesequence request parameter for cache consistency.
    # Note: Only WCS v1.0.0 is fully supported at this stage, so no version negotiation is necessary
    section = args.get("section")
    if section:
        section = section.lower()
    show_service = False
    show_capability = False
    show_content_metadata = False
    if section is None or section == "/":
        show_service = True
        show_capability = True
        show_content_metadata = True
    elif section == "/wcs_capabilities/service":
        show_service = True
    elif section == "/wcs_capabilities/capability":
        show_capability = True
    elif section == "/wcs_capabilities/contentmetadata":
        show_content_metadata = True
    else:
        raise WCS1Exception("Invalid section: %s" % section,
                            WCS1Exception.INVALID_PARAMETER_VALUE,
                            locator="Section parameter")

    # Extract layer metadata from Datacube.
    platforms = get_layers(refresh=True)
    service_cfg = get_service_cfg()
    url = args.get('Host', args['url_root'])
    base_url = get_service_base_url(service_cfg.allowed_urls, url)
    return (
        render_template("wcs_capabilities.xml",
                        show_service=show_service,
                        show_capability=show_capability,
                        show_content_metadata=show_content_metadata,
                        service=service_cfg,
                        platforms=platforms,
                        base_url=base_url),
        200,
        resp_headers({
            "Content-Type": "application/xml",
            "Cache-Control": "no-cache, max-age=0"
        }))


@log_call
@opencensus_trace_call(tracer=tracer)
def desc_coverages(args):
    # Note: Only WCS v1.0.0 is fully supported at this stage, so no version negotiation is necessary
    # Extract layer metadata from Datacube.
    platforms = get_layers(refresh=True)

    coverages = args.get("coverage")
    products = []
    if coverages:
        coverages = coverages.split(",")
        for c in coverages:
            p = platforms.product_index.get(c)
            if p:
                products.append(p)
            else:
                raise WCS1Exception("Invalid coverage: %s" % c,
                                    WCS1Exception.COVERAGE_NOT_DEFINED,
                                    locator="Coverage parameter")
    else:
        for plat in platforms:
            for p in plat.products:
                products.append(p)

    return (
        render_template("wcs_desc_coverage.xml",
                        service=get_service_cfg(),
                        products=products),
        200,
        resp_headers({
            "Content-Type": "application/xml",
            "Cache-Control": "no-cache.max-age=0"
        })
    )


@log_call
@opencensus_trace_call(tracer=tracer)
def get_coverage(args):
    # Note: Only WCS v1.0.0 is fully supported at this stage, so no version negotiation is necessary
    req = WCS1GetCoverageRequest(args)
    data = get_coverage_data(req)
    return (
        req.format["renderer"](req, data),
        200,
        resp_headers({
            "Content-Type": req.format["mime"],
            'content-disposition': 'attachment; filename=%s.%s' % (req.product_name, req.format["extension"])
        })
    )
