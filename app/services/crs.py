"""Coordinate reference system helpers.

Responsibilities
----------------
* Produce a stable, human readable label for any CRS (``EPSG:4326``).
* Infer a CRS for datasets that ship without one (only when the coordinates
  are unambiguously longitude/latitude).
* Pick the best local *projected metric* CRS for a layer: the UTM zone that
  contains the geographic centre of the layer, resolved through
  ``pyproj.database.query_utm_crs_info``.

Area and length are never computed on degrees. Every measurement is made
after the geometries have been transformed into the UTM CRS chosen here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import geopandas as gpd
import numpy as np
from pyproj import CRS, Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info
from pyproj.exceptions import CRSError, ProjError

WGS84 = CRS.from_epsg(4326)

# UTM is only defined between 80°S and 84°N. Outside that band the Universal
# Polar Stereographic (UPS) systems are the standard metric alternative.
UTM_MAX_LATITUDE = 84.0
UTM_MIN_LATITUDE = -80.0
UPS_NORTH_EPSG = 32661
UPS_SOUTH_EPSG = 32761


class CRSResolutionError(ValueError):
    """Raised when a CRS cannot be determined or a UTM zone cannot be chosen."""


@dataclass(frozen=True)
class UTMSelection:
    """Result of the dynamic UTM matcher."""

    crs: CRS
    epsg: int
    label: str
    zone: int
    hemisphere: str
    center_lon: float
    center_lat: float
    warnings: tuple[str, ...]


def describe_crs(crs: CRS) -> str:
    """Return ``AUTHORITY:CODE`` when the CRS is a known registry entry.

    Falls back to the CRS name for custom definitions so the API never returns
    an empty string for a valid CRS.
    """
    try:
        authority = crs.to_authority(min_confidence=70)
    except (CRSError, ValueError):
        authority = None
    if authority:
        return f"{authority[0]}:{authority[1]}"
    return crs.name or "UNKNOWN"


def infer_missing_crs(bounds: tuple[float, float, float, float] | np.ndarray) -> CRS:
    """Assume EPSG:4326 for a dataset with no CRS, but only if it is safe.

    The assumption is accepted when all coordinates fall inside the valid
    longitude/latitude envelope. Anything else (for example projected metres
    without a ``.prj``) is ambiguous and is rejected instead of being measured
    incorrectly.
    """
    minx, miny, maxx, maxy = (float(v) for v in bounds)
    if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
        raise CRSResolutionError(
            "The dataset has no CRS definition and contains no valid geometries."
        )
    if -180.0 <= minx and maxx <= 180.0 and -90.0 <= miny and maxy <= 90.0:
        return WGS84
    raise CRSResolutionError(
        "The dataset has no CRS definition and its coordinates are not valid "
        "longitude/latitude values, so it cannot be measured safely. "
        "Include the .prj file in the ZIP archive."
    )


def utm_zone_for_longitude(lon: float) -> int:
    """Return the UTM zone number (1-60) for a longitude in degrees."""
    return min(60, max(1, int(math.floor((lon + 180.0) / 6.0)) + 1))


def geographic_bounds(gdf: gpd.GeoDataFrame) -> tuple[float, float, float, float]:
    """Return the layer bounds as (west, south, east, north) in EPSG:4326.

    ``west`` may be greater than ``east`` when the layer crosses the
    antimeridian.
    """
    if gdf.crs is None:
        raise CRSResolutionError("The dataset has no CRS; cannot select a UTM zone.")

    native_bounds = gdf.total_bounds
    if not np.all(np.isfinite(native_bounds)):
        raise CRSResolutionError(
            "The dataset contains no valid geometries; cannot select a UTM zone."
        )

    source = CRS.from_user_input(gdf.crs)
    try:
        transformer = Transformer.from_crs(source, WGS84, always_xy=True)
        west, south, east, north = transformer.transform_bounds(
            *(float(v) for v in native_bounds), densify_pts=21
        )
    except (ProjError, CRSError) as exc:
        raise CRSResolutionError(
            f"Could not transform the dataset extent to longitude/latitude: {exc}"
        ) from exc

    values = (west, south, east, north)
    if not all(math.isfinite(v) for v in values):
        raise CRSResolutionError("Transforming the dataset extent produced invalid values.")
    if not (-90.0 <= south <= north <= 90.0) or not (-180.0 <= west <= 180.0) or not (
        -180.0 <= east <= 180.0
    ):
        raise CRSResolutionError(
            "The dataset extent is outside the valid longitude/latitude range."
        )
    return west, south, east, north


def _layer_center(west: float, south: float, east: float, north: float) -> tuple[float, float]:
    """Geographic centre of an extent, aware of antimeridian crossing."""
    if west <= east:
        center_lon = (west + east) / 2.0
    else:
        center_lon = ((west + east + 360.0) / 2.0 + 180.0) % 360.0 - 180.0
    center_lat = (south + north) / 2.0
    return center_lon, center_lat


def _lookup_utm_epsg(lon: float, lat: float) -> int | None:
    """Ask the PROJ database for the WGS 84 / UTM zone covering a point."""
    try:
        infos = query_utm_crs_info(
            datum_name="WGS 84",
            area_of_interest=AreaOfInterest(
                west_lon_degree=lon,
                south_lat_degree=lat,
                east_lon_degree=lon,
                north_lat_degree=lat,
            ),
        )
    except (ProjError, CRSError, ValueError):
        return None

    north = lat >= 0.0
    low, high = (32601, 32660) if north else (32701, 32760)
    codes = [int(info.code) for info in infos if info.code.isdigit() and low <= int(info.code) <= high]
    if not codes:
        return None

    expected = (32600 if north else 32700) + utm_zone_for_longitude(lon)
    return expected if expected in codes else codes[0]


def select_utm_crs(gdf: gpd.GeoDataFrame) -> UTMSelection:
    """Choose the projected metric CRS used for every measurement of a layer.

    The geographic centre of the layer extent is converted to EPSG:4326 and
    matched with a UTM zone through ``pyproj``. Layers that span several zones
    still get one CRS (for consistent totals) but a warning is attached.
    """
    west, south, east, north = geographic_bounds(gdf)
    center_lon, center_lat = _layer_center(west, south, east, north)
    warnings: list[str] = []

    is_north = center_lat >= 0.0
    hemisphere = "N" if is_north else "S"

    if center_lat > UTM_MAX_LATITUDE or center_lat < UTM_MIN_LATITUDE:
        epsg = UPS_NORTH_EPSG if is_north else UPS_SOUTH_EPSG
        zone = 0
        warnings.append(
            "Layer centre lies outside the UTM latitude band (80°S-84°N); "
            "the Universal Polar Stereographic CRS was used instead."
        )
    else:
        epsg = _lookup_utm_epsg(center_lon, center_lat)
        if epsg is None:
            epsg = (32600 if is_north else 32700) + utm_zone_for_longitude(center_lon)
        zone = epsg - (32600 if is_north else 32700)

        west_zone = utm_zone_for_longitude(west)
        east_zone = utm_zone_for_longitude(east)
        if west > east:
            warnings.append(
                "Layer crosses the antimeridian; measurements use a single UTM zone "
                f"({zone}) and accuracy decreases far from its central meridian."
            )
        elif west_zone != east_zone:
            warnings.append(
                f"Layer spans UTM zones {west_zone}-{east_zone}; measurements use zone "
                f"{zone} and accuracy decreases far from its central meridian."
            )

    try:
        crs = CRS.from_epsg(epsg)
    except CRSError as exc:  # pragma: no cover - defensive, EPSG codes above are valid
        raise CRSResolutionError(f"Could not build projected CRS EPSG:{epsg}: {exc}") from exc

    return UTMSelection(
        crs=crs,
        epsg=epsg,
        label=f"EPSG:{epsg}",
        zone=zone,
        hemisphere=hemisphere,
        center_lon=round(center_lon, 6),
        center_lat=round(center_lat, 6),
        warnings=tuple(warnings),
    )
