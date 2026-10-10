"""End-to-end and unit tests for the Geospatial File Measurement API.

The tests run against an in-memory SQLite database injected through FastAPI's
dependency overrides, so they never touch ``./data``. Measurement results are
checked against an independent geodesic calculation (``pyproj.Geod``).
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from pyproj import CRS, Geod
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.models import Base, Feature, MeasurementRun, UploadedFile  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.crs import CRSResolutionError, select_utm_crs  # noqa: E402
from app.services.measurements import MeasurementStatus, compute_measurements  # noqa: E402

GEOD = Geod(ellps="WGS84")

# A ~1.09 km x ~1.11 km parcel near Coimbatore (UTM zone 43N).
PARCEL = Polygon([(77.0, 11.0), (77.01, 11.0), (77.01, 11.01), (77.0, 11.01)])
ROAD = LineString([(77.0, 11.0), (77.02, 11.01)])

KML_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark id="parcel-1">
      <name>Parcel A</name>
      <description>Sample plot</description>
      <ExtendedData><Data name="owner"><value>Asha</value></Data></ExtendedData>
      <Polygon><outerBoundaryIs><LinearRing><coordinates>
        77.0,11.0,0 77.01,11.0,0 77.01,11.01,0 77.0,11.01,0 77.0,11.0,0
      </coordinates></LinearRing></outerBoundaryIs></Polygon>
    </Placemark>
    <Placemark id="road-1">
      <name>Road</name>
      <LineString><coordinates>77.0,11.0 77.02,11.01</coordinates></LineString>
    </Placemark>
    <Placemark id="pin-1">
      <name>Survey pin</name>
      <Point><coordinates>77.005,11.005,250</coordinates></Point>
    </Placemark>
    <Placemark id="mixed-1">
      <name>Mixed</name>
      <MultiGeometry>
        <Point><coordinates>77.0,11.0</coordinates></Point>
        <LineString><coordinates>77.0,11.0 77.1,11.1</coordinates></LineString>
      </MultiGeometry>
    </Placemark>
  </Document>
</kml>
"""


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def env():
    """TestClient wired to an isolated in-memory database."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app), factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def shapefile_zip(gdf: gpd.GeoDataFrame, drop: tuple[str, ...] = ()) -> bytes:
    """Write ``gdf`` as a Shapefile and return it zipped (flat layout)."""
    with tempfile.TemporaryDirectory() as tmp:
        gdf.to_file(os.path.join(tmp, "layer.shp"), driver="ESRI Shapefile")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name in sorted(os.listdir(tmp)):
                if Path(name).suffix.lower() in drop:
                    continue
                archive.write(os.path.join(tmp, name), name)
        return buffer.getvalue()


def zip_of(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def upload(client: TestClient, filename: str, content: bytes, content_type: str = "application/octet-stream"):
    return client.post("/api/files/", files={"file": (filename, content, content_type)})


def geodesic_area(polygon: Polygon) -> float:
    return abs(GEOD.geometry_area_perimeter(polygon)[0])


def geodesic_length(line: LineString) -> float:
    return GEOD.geometry_length(line)


# --------------------------------------------------------------------------- #
# Meta endpoints
# --------------------------------------------------------------------------- #
def test_root_serves_interactive_dashboard(env):
    client, _ = env
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "IntersectionObserver" in html
    assert 'id="dropzone"' in html
    assert "Live metrics console" in html
    assert 'id="meet-the-developer"' in html
    assert "Yeshwanth J" in html
    assert 'href="https://yesh-portfolio-dev.vercel.app/"' in html
    assert 'href="https://github.com/Yesh6957"' in html
    assert "is-activated" in html


def test_health_endpoint(env):
    client, _ = env
    assert client.get("/health").json() == {"status": "ok"}


def test_openapi_lists_required_endpoints(env):
    client, _ = env
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/api/files/"]
    assert "get" in paths["/api/files/{file_id}/"]
    assert "get" in paths["/api/files/{file_id}/measurements/"]


# --------------------------------------------------------------------------- #
# KML upload (end to end)
# --------------------------------------------------------------------------- #
def test_kml_upload_end_to_end(env):
    client, factory = env
    response = upload(client, "survey.kml", KML_SAMPLE, "application/vnd.google-earth.kml+xml")
    assert response.status_code == 201, response.text
    info = response.json()
    assert info["filename"] == "survey.kml"
    assert info["file_format"] == "KML"
    assert info["feature_count"] == 4
    assert info["crs"] == "EPSG:4326"
    assert info["status"] == "COMPLETED"
    assert info["completed_at"].endswith("Z")

    file_id = info["id"]
    assert client.get(f"/api/files/{file_id}/").json() == info

    body = client.get(f"/api/files/{file_id}/measurements/").json()
    assert body["status"] == "COMPLETED"
    assert body["native_crs"] == "EPSG:4326"
    assert body["total_features"] == 4
    assert body["projection"]["projected_crs"] == "EPSG:32643"
    assert body["projection"]["utm_zone"] == 43
    assert body["projection"]["hemisphere"] == "N"

    parcel, road, pin, mixed = body["features"]
    assert [f["feature_index"] for f in body["features"]] == [0, 1, 2, 3]

    assert parcel["geometry_type"] == "Polygon"
    assert parcel["source_id"] == "parcel-1"
    assert parcel["crs"] == "EPSG:4326"
    assert parcel["properties"]["name"] == "Parcel A"
    assert parcel["properties"]["owner"] == "Asha"
    assert parcel["geometry"]["type"] == "Polygon"
    assert parcel["measurement"]["status"] == "MEASURED"
    assert parcel["measurement"]["area_m2"] == pytest.approx(geodesic_area(PARCEL), rel=0.005)
    assert parcel["measurement"]["length_m"] is None

    assert road["geometry_type"] == "LineString"
    assert road["measurement"]["length_m"] == pytest.approx(geodesic_length(ROAD), rel=0.005)
    assert road["measurement"]["area_m2"] is None

    assert pin["geometry_type"] == "Point"
    assert pin["measurement"]["status"] == "NOT_REQUIRED"
    assert pin["measurement"]["area_m2"] is None and pin["measurement"]["length_m"] is None

    assert mixed["geometry_type"] == "GeometryCollection"
    assert mixed["measurement"]["status"] == "UNSUPPORTED"

    summary = body["summary"]
    assert summary["polygon_count"] == 1
    assert summary["line_count"] == 1
    assert summary["point_count"] == 1
    assert summary["unsupported_count"] == 1
    assert summary["total_area_m2"] == parcel["measurement"]["area_m2"]

    # All three tables were populated.
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(UploadedFile)) == 1
        assert db.scalar(select(func.count()).select_from(Feature)) == 4
        run = db.scalar(select(MeasurementRun))
        assert run.projected_crs == "EPSG:32643"
        assert run.polygon_count == 1


def test_degree_based_math_is_not_used(env):
    """A 0.01° x 0.01° parcel is ~1.2e6 m², not ~1e-4 (the degree-based value)."""
    client, _ = env
    file_id = upload(client, "survey.kml", KML_SAMPLE).json()["id"]
    area = client.get(f"/api/files/{file_id}/measurements/").json()["features"][0]["measurement"]["area_m2"]
    assert area > 1_000_000


def test_kml_malformed_placemark_is_reported_not_fatal(env):
    client, _ = env
    kml = b"""<?xml version="1.0"?>
    <kml xmlns="http://www.opengis.net/kml/2.2"><Document>
      <Placemark><name>Good</name><Point><coordinates>77.0,11.0</coordinates></Point></Placemark>
      <Placemark><name>Broken</name><Point><coordinates>not,numbers</coordinates></Point></Placemark>
      <Placemark><name>No geometry</name></Placemark>
    </Document></kml>"""
    response = upload(client, "partial.kml", kml)
    assert response.status_code == 201
    info = response.json()
    assert info["feature_count"] == 3
    assert len(info["warnings"]) == 2

    features = client.get(f"/api/files/{info['id']}/measurements/").json()["features"]
    assert features[0]["measurement"]["status"] == "NOT_REQUIRED"
    assert features[1]["measurement"]["status"] == "INVALID"
    assert features[1]["geometry"] is None
    assert features[2]["measurement"]["status"] == "INVALID"


def test_kml_with_polygon_hole_and_multigeometry_polygons(env):
    client, _ = env
    kml = b"""<?xml version="1.0"?>
    <kml xmlns="http://www.opengis.net/kml/2.2"><Document>
      <Placemark><name>Donut</name><Polygon>
        <outerBoundaryIs><LinearRing><coordinates>77.0,11.0 77.02,11.0 77.02,11.02 77.0,11.02 77.0,11.0</coordinates></LinearRing></outerBoundaryIs>
        <innerBoundaryIs><LinearRing><coordinates>77.005,11.005 77.015,11.005 77.015,11.015 77.005,11.015 77.005,11.005</coordinates></LinearRing></innerBoundaryIs>
      </Polygon></Placemark>
      <Placemark><name>Two parcels</name><MultiGeometry>
        <Polygon><outerBoundaryIs><LinearRing><coordinates>77.0,11.0 77.01,11.0 77.01,11.01 77.0,11.0</coordinates></LinearRing></outerBoundaryIs></Polygon>
        <Polygon><outerBoundaryIs><LinearRing><coordinates>77.03,11.0 77.04,11.0 77.04,11.01 77.03,11.0</coordinates></LinearRing></outerBoundaryIs></Polygon>
      </MultiGeometry></Placemark>
    </Document></kml>"""
    info = upload(client, "shapes.kml", kml).json()
    features = client.get(f"/api/files/{info['id']}/measurements/").json()["features"]

    outer = Polygon([(77.0, 11.0), (77.02, 11.0), (77.02, 11.02), (77.0, 11.02)])
    inner = Polygon([(77.005, 11.005), (77.015, 11.005), (77.015, 11.015), (77.005, 11.015)])
    expected = geodesic_area(outer) - geodesic_area(inner)
    assert features[0]["measurement"]["area_m2"] == pytest.approx(expected, rel=0.005)
    assert features[1]["geometry_type"] == "MultiPolygon"
    assert features[1]["measurement"]["status"] == "MEASURED"


# --------------------------------------------------------------------------- #
# Shapefile upload and CRS handling
# --------------------------------------------------------------------------- #
def test_shapefile_geographic_polygons_including_multipolygon(env):
    client, _ = env
    far = Polygon([(77.05, 11.0), (77.06, 11.0), (77.06, 11.01), (77.05, 11.01)])
    gdf = gpd.GeoDataFrame(
        {"name": ["single", "multi"], "code": [1, 2]},
        geometry=[PARCEL, MultiPolygon([PARCEL, far])],
        crs="EPSG:4326",
    )
    response = upload(client, "parcels.zip", shapefile_zip(gdf), "application/zip")
    assert response.status_code == 201, response.text
    info = response.json()
    assert info["file_format"] == "SHAPEFILE"
    assert info["crs"] == "EPSG:4326"
    assert info["feature_count"] == 2

    features = client.get(f"/api/files/{info['id']}/measurements/").json()["features"]
    single, multi = features
    assert single["properties"] == {"name": "single", "code": 1}
    assert single["measurement"]["area_m2"] == pytest.approx(geodesic_area(PARCEL), rel=0.005)
    assert multi["geometry_type"] == "MultiPolygon"
    assert multi["measurement"]["area_m2"] == pytest.approx(
        geodesic_area(PARCEL) + geodesic_area(far), rel=0.005
    )


def test_shapefile_in_projected_crs_gives_same_area_and_keeps_native_crs(env):
    client, _ = env
    geographic = gpd.GeoDataFrame({"id": [1]}, geometry=[PARCEL], crs="EPSG:4326")
    projected = geographic.to_crs("EPSG:32643")

    a = upload(client, "geo.zip", shapefile_zip(geographic)).json()
    b = upload(client, "utm.zip", shapefile_zip(projected)).json()
    assert a["crs"] == "EPSG:4326"
    assert b["crs"] == "EPSG:32643"

    area_a = client.get(f"/api/files/{a['id']}/measurements/").json()["features"][0]["measurement"]["area_m2"]
    area_b = client.get(f"/api/files/{b['id']}/measurements/").json()["features"][0]["measurement"]["area_m2"]
    assert area_a == pytest.approx(area_b, rel=1e-4)


def test_shapefile_linestrings_are_measured_in_metres(env):
    client, _ = env
    gdf = gpd.GeoDataFrame({"road": ["NH47"]}, geometry=[ROAD], crs="EPSG:4326")
    info = upload(client, "roads.zip", shapefile_zip(gdf)).json()
    feature = client.get(f"/api/files/{info['id']}/measurements/").json()["features"][0]
    assert feature["geometry_type"] == "LineString"
    assert feature["measurement"]["length_m"] == pytest.approx(geodesic_length(ROAD), rel=0.005)


def test_shapefile_points_are_ingested_without_measurement(env):
    client, _ = env
    gdf = gpd.GeoDataFrame({"label": ["a", "b"]}, geometry=[Point(77.0, 11.0), Point(77.1, 11.1)], crs="EPSG:4326")
    info = upload(client, "points.zip", shapefile_zip(gdf)).json()
    body = client.get(f"/api/files/{info['id']}/measurements/").json()
    assert info["feature_count"] == 2
    assert all(f["measurement"]["status"] == "NOT_REQUIRED" for f in body["features"])
    assert body["summary"]["point_count"] == 2
    assert body["summary"]["total_area_m2"] == 0


def test_shapefile_without_prj_assumes_wgs84_when_coordinates_are_geographic(env):
    client, _ = env
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[PARCEL], crs="EPSG:4326")
    info = upload(client, "noprj.zip", shapefile_zip(gdf, drop=(".prj",))).json()
    assert info["status"] == "COMPLETED"
    assert info["crs"] == "EPSG:4326"
    assert any("EPSG:4326 was assumed" in w for w in info["warnings"])


def test_shapefile_without_prj_and_projected_coordinates_is_rejected(env):
    client, _ = env
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[PARCEL], crs="EPSG:4326").to_crs("EPSG:32643")
    response = upload(client, "ambiguous.zip", shapefile_zip(gdf, drop=(".prj",)))
    assert response.status_code == 422
    assert ".prj" in response.json()["detail"]["message"]


# --------------------------------------------------------------------------- #
# Validation, failures and security
# --------------------------------------------------------------------------- #
def test_unsupported_extension_returns_415(env):
    client, _ = env
    response = upload(client, "data.geojson", b"{}")
    assert response.status_code == 415
    assert "Unsupported file type" in response.json()["detail"]


def test_missing_upload_field_returns_actionable_validation_error(env):
    client, _ = env
    response = client.post("/api/files/", data={"upload": "survey.kml"})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "multipart field 'file'" in detail["message"]
    assert detail["errors"][0]["loc"] == ["body", "file"]


def test_empty_upload_returns_400(env):
    client, _ = env
    assert upload(client, "empty.kml", b"").status_code == 400


def test_oversized_upload_returns_413(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr("app.api.routes.files.MAX_UPLOAD_BYTES", 100)
    response = upload(client, "big.kml", KML_SAMPLE)
    assert response.status_code == 413


def test_failed_processing_is_recorded_and_blocks_measurements(env):
    client, _ = env
    response = upload(client, "broken.zip", b"this is not a zip")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["status"] == "FAILED"

    info = client.get(f"/api/files/{detail['file_id']}/").json()
    assert info["status"] == "FAILED"
    assert "not a valid ZIP" in info["error_message"]
    assert info["feature_count"] == 0

    blocked = client.get(f"/api/files/{detail['file_id']}/measurements/")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["status"] == "FAILED"


def test_zip_missing_required_component_is_rejected(env):
    client, _ = env
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[PARCEL], crs="EPSG:4326")
    response = upload(client, "partial.zip", shapefile_zip(gdf, drop=(".dbf",)))
    assert response.status_code == 422
    assert ".dbf" in response.json()["detail"]["message"]


def test_zip_without_shapefile_is_rejected(env):
    client, _ = env
    response = upload(client, "docs.zip", zip_of({"readme.txt": b"hello"}))
    assert response.status_code == 422
    assert "does not contain a .shp" in response.json()["detail"]["message"]


def test_zip_path_traversal_is_rejected(env):
    client, _ = env
    response = upload(client, "evil.zip", zip_of({"../../evil.shp": b"x", "layer.shx": b"x", "layer.dbf": b"x"}))
    assert response.status_code == 422
    assert "unsafe" in response.json()["detail"]["message"]


def test_zip_with_multiple_shapefiles_is_rejected(env):
    client, _ = env
    response = upload(
        client,
        "many.zip",
        zip_of({f"{n}.{e}": b"x" for n in ("a", "b") for e in ("shp", "shx", "dbf")}),
    )
    assert response.status_code == 422
    assert "multiple Shapefiles" in response.json()["detail"]["message"]


def test_corrupt_shapefile_content_is_reported_as_422(env):
    client, _ = env
    response = upload(client, "corrupt.zip", zip_of({"layer.shp": b"junk", "layer.shx": b"junk", "layer.dbf": b"junk"}))
    assert response.status_code == 422
    assert response.json()["detail"]["status"] == "FAILED"


def test_invalid_xml_is_rejected(env):
    client, _ = env
    response = upload(client, "bad.kml", b"<kml><Placemark>")
    assert response.status_code == 422
    assert "not valid KML" in response.json()["detail"]["message"]


def test_non_kml_xml_is_rejected(env):
    client, _ = env
    response = upload(client, "other.kml", b"<?xml version='1.0'?><html><body/></html>")
    assert response.status_code == 422


def test_kml_without_placemarks_is_rejected(env):
    client, _ = env
    response = upload(client, "empty.kml", b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document/></kml>')
    assert response.status_code == 422
    assert "no Placemark" in response.json()["detail"]["message"]


def test_xml_external_entities_are_not_resolved(env):
    client, _ = env
    kml = b"""<?xml version="1.0"?>
    <!DOCTYPE kml [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
    <kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>
      <name>&xxe;</name><Point><coordinates>77.0,11.0</coordinates></Point>
    </Placemark></Document></kml>"""
    response = upload(client, "xxe.kml", kml)
    assert "root:" not in response.text
    if response.status_code == 201:
        body = client.get(f"/api/files/{response.json()['id']}/measurements/").text
        assert "root:" not in body


def test_unknown_file_id_returns_404(env):
    client, _ = env
    assert client.get("/api/files/doesnotexist/").status_code == 404
    assert client.get("/api/files/doesnotexist/measurements/").status_code == 404


# --------------------------------------------------------------------------- #
# Measurements endpoint options
# --------------------------------------------------------------------------- #
def test_measurements_pagination_and_geometry_toggle(env):
    client, _ = env
    gdf = gpd.GeoDataFrame(
        {"n": list(range(5))},
        geometry=[Point(77.0 + i * 0.01, 11.0) for i in range(5)],
        crs="EPSG:4326",
    )
    file_id = upload(client, "pts.zip", shapefile_zip(gdf)).json()["id"]

    page = client.get(f"/api/files/{file_id}/measurements/?limit=2&offset=2&include_geometry=false").json()
    assert page["total_features"] == 5
    assert page["limit"] == 2 and page["offset"] == 2
    assert [f["feature_index"] for f in page["features"]] == [2, 3]
    assert all(f["geometry"] is None for f in page["features"])

    with_geometry = client.get(f"/api/files/{file_id}/measurements/?limit=1").json()
    assert with_geometry["features"][0]["geometry"]["type"] == "Point"

    assert client.get(f"/api/files/{file_id}/measurements/?limit=0").status_code == 422


# --------------------------------------------------------------------------- #
# Service-level unit tests
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("lon", "lat", "expected"),
    [
        (77.0, 11.0, "EPSG:32643"),
        (-58.4, -34.6, "EPSG:32721"),
        (-122.4, 37.8, "EPSG:32610"),
        (151.2, -33.9, "EPSG:32756"),
        (10.0, 87.0, "EPSG:32661"),
        (10.0, -85.0, "EPSG:32761"),
    ],
)
def test_utm_selection_from_layer_centre(lon, lat, expected):
    gdf = gpd.GeoDataFrame(geometry=[Point(lon, lat)], crs="EPSG:4326")
    assert select_utm_crs(gdf).label == expected


def test_utm_selection_uses_layer_centre_and_warns_when_spanning_zones():
    gdf = gpd.GeoDataFrame(geometry=[Point(70.0, 11.0), Point(86.0, 11.0)], crs="EPSG:4326")
    selection = select_utm_crs(gdf)
    # Centre longitude is exactly 78.0°E, where zone 44 begins.
    assert selection.label == "EPSG:32644"
    assert selection.zone == 44
    assert any("spans UTM zones 42-45" in w for w in selection.warnings)


def test_utm_selection_works_for_projected_input():
    gdf = gpd.GeoDataFrame(geometry=[PARCEL], crs="EPSG:4326").to_crs("EPSG:3857")
    assert select_utm_crs(gdf).label == "EPSG:32643"


def test_utm_selection_requires_valid_geometries():
    gdf = gpd.GeoDataFrame(geometry=[None], crs="EPSG:4326")
    with pytest.raises(CRSResolutionError):
        select_utm_crs(gdf)


def test_measurements_refuse_geographic_crs():
    gdf = gpd.GeoDataFrame(geometry=[PARCEL], crs="EPSG:4326")
    with pytest.raises(ValueError, match="projected"):
        compute_measurements(gdf, CRS.from_epsg(4326))


def test_measurements_handle_missing_and_empty_geometries():
    gdf = gpd.GeoDataFrame(geometry=[PARCEL, None, Polygon()], crs="EPSG:4326")
    report = compute_measurements(gdf, select_utm_crs(gdf).crs)
    statuses = [f.status for f in report.features]
    assert statuses == [MeasurementStatus.MEASURED, MeasurementStatus.INVALID, MeasurementStatus.INVALID]
    assert report.features[1].geometry_type is None
    assert report.totals.invalid_count == 2


def test_measurements_flag_invalid_polygons_but_still_measure():
    bowtie = Polygon([(77.0, 11.0), (77.01, 11.01), (77.01, 11.0), (77.0, 11.01)])
    gdf = gpd.GeoDataFrame(geometry=[bowtie], crs="EPSG:4326")
    report = compute_measurements(gdf, select_utm_crs(gdf).crs)
    assert report.features[0].status is MeasurementStatus.MEASURED
    assert "invalid" in (report.features[0].note or "")
