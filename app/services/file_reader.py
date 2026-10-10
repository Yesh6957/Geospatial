"""Geospatial file ingestion.

Two input formats are supported:

* ``.zip`` containing one Shapefile (``.shp`` + ``.shx`` + ``.dbf``, optional
  ``.prj`` / ``.cpg``), read with ``fiona``.
* ``.kml``, parsed with a hardened ``lxml`` parser (KML is always WGS 84 /
  EPSG:4326 by specification).

Both readers return a :class:`ParsedLayer` wrapping a ``GeoDataFrame`` with
the *native* CRS, one row per feature, and two bookkeeping columns:
``source_id`` (native feature id, if any) and ``attributes`` (a JSON-safe
dict of the feature properties).

Safety measures
---------------
* ZIP entries are inspected before extraction: absolute paths and ``..``
  segments are rejected, only known Shapefile component extensions are
  extracted, and extraction is bounded by an uncompressed size budget
  (zip-bomb protection).
* Extracted components are written to a private temp directory under fixed
  names, so archive-supplied file names never touch the file system.
* The XML parser does not resolve entities, load DTDs or touch the network.
"""

from __future__ import annotations

import io
import math
import os
import re
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

import fiona
import fiona.errors
import geopandas as gpd
from lxml import etree
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.errors import ShapelyError
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    shape,
)
from shapely.geometry.base import BaseGeometry

from app.services.crs import WGS84, describe_crs, infer_missing_crs

# --------------------------------------------------------------------------- #
# Limits (override through environment variables)
# --------------------------------------------------------------------------- #
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = int(os.getenv("MAX_UNCOMPRESSED_MB", "250")) * 1024 * 1024
MAX_ZIP_MEMBERS = int(os.getenv("MAX_ZIP_MEMBERS", "500"))
MAX_WARNINGS = 50

FORMAT_SHAPEFILE = "SHAPEFILE"
FORMAT_KML = "KML"

SHAPEFILE_REQUIRED = (".shp", ".shx", ".dbf")
SHAPEFILE_OPTIONAL = (".prj", ".cpg")
SHAPEFILE_COMPONENTS = SHAPEFILE_REQUIRED + SHAPEFILE_OPTIONAL

KML_GEOMETRY_TAGS = frozenset({"Point", "LineString", "LinearRing", "Polygon", "MultiGeometry"})
KML_ROOT_TAGS = frozenset({"kml", "Document", "Folder", "Placemark"})

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class UnsupportedFormatError(ValueError):
    """The uploaded file name does not map to a supported format."""


class FileReadError(ValueError):
    """The uploaded content could not be read as a valid geospatial file."""


class _GeometryParseError(ValueError):
    """A single KML geometry is malformed (the rest of the file is kept)."""


@dataclass
class ParsedLayer:
    """A parsed vector layer in its native CRS."""

    gdf: gpd.GeoDataFrame
    source_crs: CRS
    crs_label: str
    file_format: str
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def detect_format(filename: str) -> str:
    """Map a file name to ``SHAPEFILE`` or ``KML`` based on its extension."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".zip":
        return FORMAT_SHAPEFILE
    if suffix == ".kml":
        return FORMAT_KML
    shown = suffix or "(none)"
    raise UnsupportedFormatError(
        f"Unsupported file type '{shown}'. Upload a .zip containing a Shapefile or a .kml file."
    )


def read_geospatial_file(file_format: str, content: bytes) -> ParsedLayer:
    """Parse ``content`` according to ``file_format``."""
    if not content:
        raise FileReadError("The uploaded file is empty.")
    if file_format == FORMAT_SHAPEFILE:
        return _read_shapefile_zip(content)
    if file_format == FORMAT_KML:
        return _read_kml(content)
    raise UnsupportedFormatError(f"Unsupported file format '{file_format}'.")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _json_safe(value: Any) -> Any:
    """Convert attribute values into something ``json.dumps`` always accepts."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    if hasattr(value, "item"):  # numpy scalars
        try:
            return _json_safe(value.item())
        except (ValueError, TypeError):
            pass
    return str(value)


def _cap_warnings(warnings: list[str]) -> list[str]:
    if len(warnings) <= MAX_WARNINGS:
        return warnings
    hidden = len(warnings) - MAX_WARNINGS
    return warnings[:MAX_WARNINGS] + [f"... {hidden} additional warnings suppressed."]


def _build_layer(
    geometries: list[BaseGeometry | None],
    source_ids: list[str | None],
    attributes: list[dict[str, Any]],
    crs: CRS | None,
    file_format: str,
    warnings: list[str],
) -> ParsedLayer:
    frame = gpd.GeoDataFrame(
        {"source_id": source_ids, "attributes": attributes},
        geometry=gpd.GeoSeries(geometries, crs=crs),
    )
    if crs is None:
        inferred = infer_missing_crs(frame.total_bounds)
        frame = frame.set_crs(inferred)
        crs = inferred
        warnings.append(
            "No CRS was found in the file; EPSG:4326 was assumed because all coordinates "
            "are valid longitude/latitude values."
        )
    return ParsedLayer(
        gdf=frame,
        source_crs=crs,
        crs_label=describe_crs(crs),
        file_format=file_format,
        warnings=_cap_warnings(warnings),
    )


# --------------------------------------------------------------------------- #
# Shapefile (ZIP) reader
# --------------------------------------------------------------------------- #
def _copy_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path, budget: int) -> int:
    """Extract one member, never writing more than ``budget`` bytes."""
    written = 0
    try:
        with archive.open(info) as source, open(target, "wb") as destination:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > budget:
                    raise FileReadError(
                        "The archive expands beyond the allowed uncompressed size."
                    )
                destination.write(chunk)
    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, EOFError) as exc:
        raise FileReadError(f"Could not extract '{info.filename}' from the archive: {exc}") from exc
    return written


def _extract_shapefile_components(content: bytes, workdir: Path) -> Path:
    """Validate the ZIP and extract exactly one Shapefile to ``workdir``.

    Returns the path of the extracted ``.shp`` (always named ``layer.shp``).
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise FileReadError("The uploaded file is not a valid ZIP archive.") from exc

    with archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) > MAX_ZIP_MEMBERS:
            raise FileReadError(f"The archive contains too many files (limit {MAX_ZIP_MEMBERS}).")
        if sum(info.file_size for info in members) > MAX_UNCOMPRESSED_BYTES:
            raise FileReadError("The archive expands beyond the allowed uncompressed size.")

        # (parent directory, stem) -> extension -> member
        candidates: dict[tuple[str, str], dict[str, zipfile.ZipInfo]] = {}
        for info in members:
            name = info.filename.replace("\\", "/")
            path = PurePosixPath(name)
            parts = path.parts
            if name.startswith("/") or ".." in parts or (parts and _WINDOWS_DRIVE.match(parts[0])):
                raise FileReadError("The archive contains unsafe file paths.")
            if parts and parts[0] == "__MACOSX":
                continue
            if path.name.startswith("._"):
                continue

            stem, extension = os.path.splitext(path.name)
            extension = extension.lower()
            if extension not in SHAPEFILE_COMPONENTS:
                continue

            key = (str(path.parent).lower(), stem.lower())
            bucket = candidates.setdefault(key, {})
            if extension in bucket:
                raise FileReadError(
                    f"The archive contains duplicate '{extension}' files for '{stem}'."
                )
            bucket[extension] = info

        shapefile_keys = [key for key, parts in candidates.items() if ".shp" in parts]
        if not shapefile_keys:
            raise FileReadError("The ZIP archive does not contain a .shp file.")
        if len(shapefile_keys) > 1:
            raise FileReadError(
                "The ZIP archive contains multiple Shapefiles; upload one Shapefile per archive."
            )

        components = candidates[shapefile_keys[0]]
        missing = [ext for ext in SHAPEFILE_REQUIRED if ext not in components]
        if missing:
            raise FileReadError(
                "The Shapefile is missing required component file(s): " + ", ".join(missing) + "."
            )

        budget = MAX_UNCOMPRESSED_BYTES
        for extension, info in components.items():
            budget -= _copy_bounded(archive, info, workdir / f"layer{extension}", budget)

    return workdir / "layer.shp"


def _read_shapefile_zip(content: bytes) -> ParsedLayer:
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="shp_upload_") as tmp:
        shp_path = _extract_shapefile_components(content, Path(tmp))

        geometries: list[BaseGeometry | None] = []
        source_ids: list[str | None] = []
        attributes: list[dict[str, Any]] = []
        try:
            with fiona.open(shp_path) as source:
                crs_wkt = source.crs_wkt
                for position, feature in enumerate(source):
                    raw_geometry = feature.geometry
                    geometry: BaseGeometry | None = None
                    if raw_geometry is not None:
                        try:
                            geometry = shape(raw_geometry)
                        except (ShapelyError, ValueError, TypeError, KeyError) as exc:
                            warnings.append(f"Feature {position}: geometry could not be read ({exc}).")
                    geometries.append(geometry)
                    source_ids.append(None if feature.id is None else str(feature.id))
                    properties = feature.properties or {}
                    attributes.append({str(k): _json_safe(v) for k, v in dict(properties).items()})
        except (fiona.errors.FionaError, ValueError, OSError, UnicodeDecodeError) as exc:
            raise FileReadError(f"The Shapefile could not be read: {exc}") from exc

    if not geometries:
        raise FileReadError("The Shapefile contains no features.")

    crs: CRS | None = None
    if crs_wkt:
        try:
            crs = CRS.from_wkt(crs_wkt)
        except CRSError as exc:
            raise FileReadError(f"The Shapefile CRS definition is invalid: {exc}") from exc

    return _build_layer(geometries, source_ids, attributes, crs, FORMAT_SHAPEFILE, warnings)


# --------------------------------------------------------------------------- #
# KML reader
# --------------------------------------------------------------------------- #
def _local_name(element: etree._Element) -> str:
    return etree.QName(element).localname


def _child_text(element: etree._Element, local_name: str) -> str | None:
    child = element.find(f"{{*}}{local_name}")
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def _parse_coordinates(element: etree._Element | None) -> list[tuple[float, float]]:
    """Parse a KML ``<coordinates>`` element into (lon, lat) pairs.

    Altitude is ignored: measurements are planar.
    """
    if element is None or not element.text or not element.text.strip():
        raise _GeometryParseError("missing <coordinates>")

    pairs: list[tuple[float, float]] = []
    for token in element.text.split():
        values = token.split(",")
        if len(values) < 2:
            raise _GeometryParseError(f"malformed coordinate '{token}'")
        try:
            lon, lat = float(values[0]), float(values[1])
        except ValueError as exc:
            raise _GeometryParseError(f"non-numeric coordinate '{token}'") from exc
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise _GeometryParseError(f"non-finite coordinate '{token}'")
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            raise _GeometryParseError(f"coordinate out of range '{token}'")
        pairs.append((lon, lat))
    return pairs


def _ring_from(boundary: etree._Element | None, label: str) -> list[tuple[float, float]]:
    if boundary is None:
        raise _GeometryParseError(f"missing {label}")
    coordinates = boundary.find(".//{*}coordinates")
    ring = _parse_coordinates(coordinates)
    if len(ring) < 3:
        raise _GeometryParseError(f"{label} needs at least 3 coordinates")
    return ring


def _combine_geometries(parts: list[BaseGeometry]) -> BaseGeometry:
    """Combine the members of a ``<MultiGeometry>`` into one Shapely geometry."""
    atoms: list[BaseGeometry] = []
    for part in parts:
        if part.geom_type.startswith("Multi") or part.geom_type == "GeometryCollection":
            atoms.extend(part.geoms)
        else:
            atoms.append(part)

    if not atoms:
        raise _GeometryParseError("empty <MultiGeometry>")
    if len(atoms) == 1:
        return atoms[0]

    kinds = {atom.geom_type for atom in atoms}
    if kinds == {"Point"}:
        return MultiPoint(atoms)
    if kinds == {"LineString"}:
        return MultiLineString(atoms)
    if kinds == {"Polygon"}:
        return MultiPolygon(atoms)
    return GeometryCollection(atoms)


def _parse_kml_geometry(element: etree._Element) -> BaseGeometry:
    kind = _local_name(element)

    if kind == "Point":
        coordinates = _parse_coordinates(element.find(".//{*}coordinates"))
        if len(coordinates) != 1:
            raise _GeometryParseError("a Point must have exactly one coordinate")
        return Point(coordinates[0])

    if kind in ("LineString", "LinearRing"):
        coordinates = _parse_coordinates(element.find(".//{*}coordinates"))
        if len(coordinates) < 2:
            raise _GeometryParseError(f"a {kind} needs at least 2 coordinates")
        return LineString(coordinates)

    if kind == "Polygon":
        shell = _ring_from(element.find("{*}outerBoundaryIs"), "outer boundary")
        holes = [
            _ring_from(inner, "inner boundary") for inner in element.findall("{*}innerBoundaryIs")
        ]
        try:
            return Polygon(shell, holes)
        except (ShapelyError, ValueError) as exc:
            raise _GeometryParseError(f"invalid polygon ({exc})") from exc

    if kind == "MultiGeometry":
        parts = [
            _parse_kml_geometry(child)
            for child in element
            if isinstance(child.tag, str) and _local_name(child) in KML_GEOMETRY_TAGS
        ]
        return _combine_geometries(parts)

    raise _GeometryParseError(f"unsupported geometry element <{kind}>")


def _read_kml(content: bytes) -> ParsedLayer:
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
    )
    try:
        root = etree.fromstring(content, parser)
    except etree.XMLSyntaxError as exc:
        raise FileReadError(f"The file is not valid KML/XML: {exc}") from exc
    if root is None or _local_name(root) not in KML_ROOT_TAGS:
        raise FileReadError("The XML document is not a KML file.")

    placemarks = list(root.iter("{*}Placemark"))
    if not placemarks:
        raise FileReadError("The KML file contains no Placemark features.")

    geometries: list[BaseGeometry | None] = []
    source_ids: list[str | None] = []
    attributes: list[dict[str, Any]] = []
    warnings: list[str] = []

    for position, placemark in enumerate(placemarks):
        properties: dict[str, Any] = {}
        name = _child_text(placemark, "name")
        description = _child_text(placemark, "description")
        if name is not None:
            properties["name"] = name
        if description is not None:
            properties["description"] = description

        for data in placemark.iter("{*}Data"):
            key = data.get("name")
            if key:
                properties[key] = _child_text(data, "value")
        for simple in placemark.iter("{*}SimpleData"):
            key = simple.get("name")
            if key:
                properties[key] = (simple.text or "").strip() or None

        geometry: BaseGeometry | None = None
        geometry_element = next(
            (
                child
                for child in placemark
                if isinstance(child.tag, str) and _local_name(child) in KML_GEOMETRY_TAGS
            ),
            None,
        )
        if geometry_element is None:
            warnings.append(f"Placemark {position}: no supported geometry element found.")
        else:
            try:
                geometry = _parse_kml_geometry(geometry_element)
            except (_GeometryParseError, ShapelyError, ValueError) as exc:
                warnings.append(f"Placemark {position}: {exc}.")

        geometries.append(geometry)
        source_ids.append(placemark.get("id"))
        attributes.append({str(k): _json_safe(v) for k, v in properties.items()})

    return _build_layer(geometries, source_ids, attributes, WGS84, FORMAT_KML, warnings)
