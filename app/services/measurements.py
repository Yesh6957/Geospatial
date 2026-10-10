"""Metric measurements for vector features.

All geometries are transformed to a projected metric CRS (chosen by
``app.services.crs.select_utm_crs``) *before* any calculation, so results are
true square metres and metres rather than degree-based approximations.

The geometry columns are processed with vectorized GeoPandas/Shapely
operations; only the final assembly of per-feature result objects is a Python
loop.

Rules
-----
* ``Polygon`` / ``MultiPolygon``       -> area in square metres.
* ``LineString`` / ``MultiLineString`` -> length in metres.
* ``Point`` / ``MultiPoint``           -> ingested, nothing measured.
* Anything else (GeometryCollection, ...) is reported as ``UNSUPPORTED``.
* Missing/empty geometries, or geometries that cannot be projected, are
  reported as ``INVALID``. Neither case raises.

Measurements are planar in the projected CRS and ignore elevation (Z).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import geopandas as gpd
from pyproj import CRS

AREA_TYPES = frozenset({"Polygon", "MultiPolygon"})
LENGTH_TYPES = frozenset({"LineString", "MultiLineString"})
POINT_TYPES = frozenset({"Point", "MultiPoint"})

ROUND_DIGITS = 4


class MeasurementStatus(str, Enum):
    """Outcome of the measurement step for one feature."""

    MEASURED = "MEASURED"
    NOT_REQUIRED = "NOT_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID = "INVALID"


@dataclass(frozen=True)
class FeatureMeasurement:
    feature_index: int
    geometry_type: str | None
    status: MeasurementStatus
    area_m2: float | None
    length_m: float | None
    note: str | None


@dataclass(frozen=True)
class MeasurementTotals:
    total_area_m2: float
    total_length_m: float
    polygon_count: int
    line_count: int
    point_count: int
    unsupported_count: int
    invalid_count: int


@dataclass(frozen=True)
class MeasurementReport:
    features: list[FeatureMeasurement]
    totals: MeasurementTotals


def compute_measurements(gdf: gpd.GeoDataFrame, projected_crs: CRS) -> MeasurementReport:
    """Measure every feature of ``gdf`` in ``projected_crs``.

    ``gdf`` must carry its native CRS. The projected CRS must be metric; this
    is checked so degrees can never be measured by accident.
    """
    if gdf.crs is None:
        raise ValueError("GeoDataFrame must have a CRS before measuring.")
    if not projected_crs.is_projected:
        raise ValueError("Measurements require a projected (metric) CRS, not degrees.")

    geometry = gdf.geometry
    count = len(geometry)

    # --- vectorized classification on the native geometries -----------------
    missing = (geometry.isna() | geometry.is_empty).to_numpy(dtype=bool)
    geom_types = geometry.geom_type.to_numpy(dtype=object)
    is_area = geometry.geom_type.isin(AREA_TYPES).to_numpy(dtype=bool)
    is_line = geometry.geom_type.isin(LENGTH_TYPES).to_numpy(dtype=bool)
    is_point = geometry.geom_type.isin(POINT_TYPES).to_numpy(dtype=bool)
    is_valid = geometry.is_valid.to_numpy(dtype=bool)

    # --- vectorized metrics in the projected CRS ----------------------------
    projected = geometry.to_crs(projected_crs)
    areas = projected.area.to_numpy(dtype=float)
    lengths = projected.length.to_numpy(dtype=float)

    results: list[FeatureMeasurement] = []
    total_area: list[float] = []
    total_length: list[float] = []
    polygon_count = line_count = point_count = unsupported_count = invalid_count = 0

    for index in range(count):
        raw_type = geom_types[index]
        # pandas yields NaN/None for missing geometries; only real strings are types.
        geometry_type = raw_type if isinstance(raw_type, str) else None

        if missing[index]:
            invalid_count += 1
            results.append(
                FeatureMeasurement(
                    index,
                    geometry_type,
                    MeasurementStatus.INVALID,
                    None,
                    None,
                    "Geometry is missing, empty or could not be parsed.",
                )
            )
        elif is_point[index]:
            point_count += 1
            results.append(
                FeatureMeasurement(
                    index,
                    geometry_type,
                    MeasurementStatus.NOT_REQUIRED,
                    None,
                    None,
                    "Points have no area or length.",
                )
            )
        elif is_area[index]:
            value = areas[index]
            if not math.isfinite(value):
                invalid_count += 1
                results.append(
                    FeatureMeasurement(
                        index,
                        geometry_type,
                        MeasurementStatus.INVALID,
                        None,
                        None,
                        "Projection produced non-finite coordinates.",
                    )
                )
                continue
            polygon_count += 1
            rounded = round(float(value), ROUND_DIGITS)
            total_area.append(rounded)
            note = None
            if not is_valid[index]:
                note = (
                    "Geometry is topologically invalid (for example self-intersecting); "
                    "the area may be unreliable."
                )
            results.append(
                FeatureMeasurement(
                    index, geometry_type, MeasurementStatus.MEASURED, rounded, None, note
                )
            )
        elif is_line[index]:
            value = lengths[index]
            if not math.isfinite(value):
                invalid_count += 1
                results.append(
                    FeatureMeasurement(
                        index,
                        geometry_type,
                        MeasurementStatus.INVALID,
                        None,
                        None,
                        "Projection produced non-finite coordinates.",
                    )
                )
                continue
            line_count += 1
            rounded = round(float(value), ROUND_DIGITS)
            total_length.append(rounded)
            results.append(
                FeatureMeasurement(
                    index, geometry_type, MeasurementStatus.MEASURED, None, rounded, None
                )
            )
        else:
            unsupported_count += 1
            results.append(
                FeatureMeasurement(
                    index,
                    geometry_type,
                    MeasurementStatus.UNSUPPORTED,
                    None,
                    None,
                    f"Geometry type '{geometry_type}' is not supported for measurement.",
                )
            )

    totals = MeasurementTotals(
        total_area_m2=round(math.fsum(total_area), ROUND_DIGITS),
        total_length_m=round(math.fsum(total_length), ROUND_DIGITS),
        polygon_count=polygon_count,
        line_count=line_count,
        point_count=point_count,
        unsupported_count=unsupported_count,
        invalid_count=invalid_count,
    )
    return MeasurementReport(features=results, totals=totals)
