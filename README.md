# Geospatial File Measurement API

A FastAPI backend that accepts a **zipped Shapefile** or a **KML** file, extracts every feature, and returns
**true metric measurements**: polygon area in square metres and line length in metres.

Geometries are never measured in degrees. Each layer is reprojected into the **best-fit local UTM zone**
(chosen dynamically from the layer's geographic centre) before any calculation.

- **Stack:** Python 3.12, FastAPI, GeoPandas, Shapely, PyProj, Fiona, lxml, SQLAlchemy 2 + SQLite
- **Frontend:** a single-page dashboard served at `GET /` (drag-and-drop upload, scroll-to-reveal sections, live JSON console)
- **Docs:** interactive OpenAPI at `/docs`

---

## Setup

### Run locally

```bash
python3.12 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/> for the dashboard or <http://127.0.0.1:8000/docs> for the OpenAPI UI.
The SQLite database is created automatically at `./data/geospatial.db`.

### Run with Docker

```bash
docker build -t geospatial-measurement-api .
docker run --rm -p 8000:8000 -v geo-data:/app/data geospatial-measurement-api
```

### Run the tests

```bash
python -m pytest -q
```

The suite uses an in-memory database (it never touches `./data`) and checks results against an independent
geodesic calculation (`pyproj.Geod`).

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/geospatial.db` | SQLAlchemy database URL |
| `MAX_UPLOAD_MB` | `50` | Maximum upload size (HTTP 413 above this) |
| `MAX_UNCOMPRESSED_MB` | `250` | Maximum total size a ZIP may expand to (zip-bomb guard) |
| `MAX_ZIP_MEMBERS` | `500` | Maximum number of files inside a ZIP |

---

## API

All endpoints use trailing slashes, as in the specification. Requests without one receive a `307` redirect.

### `POST /api/files/`: upload and process

Multipart form field `file`: a `.zip` containing a Shapefile (`.shp`, `.shx`, `.dbf`, optional `.prj` / `.cpg`) or a `.kml`.
Processing is synchronous, so the response already reflects the final state.

```bash
curl -X POST http://127.0.0.1:8000/api/files/ -F "file=@survey.kml"
```

```json
{
  "id": "51eec35fa851449cb18763deaec790a7",
  "filename": "survey.kml",
  "file_format": "KML",
  "size_bytes": 616,
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "error_message": null,
  "warnings": [],
  "created_at": "2026-10-08T05:07:25.007281Z",
  "completed_at": "2026-10-08T05:07:25.100659Z"
}
```

| Status | Meaning |
|---|---|
| `201` | File processed (`status: COMPLETED`) |
| `400` | Empty upload |
| `413` | File larger than `MAX_UPLOAD_MB` |
| `415` | Extension is not `.zip` or `.kml` |
| `422` | Missing multipart field `file`, or content is not valid geospatial data. For invalid geospatial data, a `FAILED` record is stored; the body contains `file_id`, `status` and `message`. For a missing field, the response explains the expected multipart request. |

### `GET /api/files/{id}/`: file information

```bash
curl http://127.0.0.1:8000/api/files/51eec35fa851449cb18763deaec790a7/
```

Returns the same object as the upload response. `status` is one of `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`.
Unknown ids return `404`.

### `GET /api/files/{id}/measurements/`: per-feature measurements

| Query parameter | Default | Description |
|---|---|---|
| `limit` | `1000` | Features per page (1 to 10000) |
| `offset` | `0` | Features to skip |
| `include_geometry` | `true` | Include each feature's GeoJSON geometry (native CRS) |

```bash
curl "http://127.0.0.1:8000/api/files/51eec35fa851449cb18763deaec790a7/measurements/?include_geometry=false"
```

```json
{
  "file_id": "51eec35fa851449cb18763deaec790a7",
  "filename": "survey.kml",
  "status": "COMPLETED",
  "native_crs": "EPSG:4326",
  "projection": {
    "projected_crs": "EPSG:32643",
    "utm_zone": 43,
    "hemisphere": "N",
    "center_lon": 77.01,
    "center_lat": 11.005,
    "warnings": []
  },
  "summary": {
    "total_area_m2": 1209329.9307,
    "total_length_m": 2450.1575,
    "polygon_count": 1,
    "line_count": 1,
    "point_count": 1,
    "unsupported_count": 0,
    "invalid_count": 0
  },
  "total_features": 3,
  "limit": 1000,
  "offset": 0,
  "features": [
    {
      "feature_index": 0,
      "source_id": "parcel-1",
      "geometry_type": "Polygon",
      "crs": "EPSG:4326",
      "properties": { "name": "Parcel A" },
      "geometry": null,
      "measurement": { "status": "MEASURED", "area_m2": 1209329.9307, "length_m": null, "note": null }
    },
    {
      "feature_index": 1,
      "source_id": "road-1",
      "geometry_type": "LineString",
      "crs": "EPSG:4326",
      "properties": { "name": "Access road" },
      "geometry": null,
      "measurement": { "status": "MEASURED", "area_m2": null, "length_m": 2450.1575, "note": null }
    },
    {
      "feature_index": 2,
      "source_id": "pin-1",
      "geometry_type": "Point",
      "crs": "EPSG:4326",
      "properties": { "name": "Survey pin" },
      "geometry": null,
      "measurement": { "status": "NOT_REQUIRED", "area_m2": null, "length_m": null, "note": "Points have no area or length." }
    }
  ]
}
```

(`geometry` is `null` here only because `include_geometry=false` was passed.)

If the file did not finish successfully this endpoint returns `409` with the stored failure reason.

**Measurement status values**

| Status | When |
|---|---|
| `MEASURED` | Polygon / MultiPolygon (area) or LineString / MultiLineString (length) |
| `NOT_REQUIRED` | Point / MultiPoint: ingested, nothing measured |
| `UNSUPPORTED` | Any other type (for example `GeometryCollection`): reported, not crashed |
| `INVALID` | Missing, empty or unparseable geometry |

---

## Architecture

### Application structure

```
app/
  main.py                  FastAPI app, lifespan (DB init), embedded single-page dashboard
  api/routes/files.py      Controllers + upload orchestration + persistence queries
  db/models.py             SQLAlchemy models: uploaded_files, features, measurement_runs
  db/session.py            Engine, session factory, get_db dependency, init_db
  schemas/schemas.py       Pydantic response models
  services/file_reader.py  ZIP/Shapefile (fiona) and KML (lxml) ingestion
  services/crs.py          CRS labelling, missing-CRS inference, dynamic UTM matcher
  services/measurements.py Vectorized area/length calculation in the projected CRS
tests/test_api.py          Endpoint, CRS, measurement, failure-path and security tests
```

The services are pure functions with no knowledge of HTTP or the database, so they are unit-testable in isolation.
The route module is thin glue between the two.

### File-processing flow

1. **Validate:** the extension decides the format (`.zip` -> Shapefile, `.kml` -> KML, otherwise `415`).
   The body is streamed in 1 MB chunks and aborted at the size limit (`413`).
2. **Record:** an `uploaded_files` row is created (`PENDING`, then `PROCESSING`).
3. **Parse** (in a worker thread so the event loop stays free):
   - *Shapefile:* the ZIP is inspected, then exactly one `.shp` with its `.shx` and `.dbf` is extracted to a private
     temp directory under fixed names and opened with `fiona`.
   - *KML:* parsed with a hardened `lxml` parser into Shapely geometries, with `<ExtendedData>` mapped to properties.
4. **Project and measure** (next section).
5. **Persist atomically:** features, the measurement run and the `COMPLETED` status are committed in a **single
   transaction**. On any failure the transaction is rolled back and only a `FAILED` row with the reason remains.

### Measurement calculation flow

1. `select_utm_crs()` finds the layer centre and the matching UTM CRS (see below).
2. `compute_measurements()` classifies every geometry with vectorized GeoPandas operations, transforms the whole
   geometry column to the UTM CRS in one call, and reads `.area` / `.length` as NumPy arrays.
3. Results are assigned per feature: area for polygons, length for lines, nothing for points. Unsupported, empty or
   non-projectable geometries get an explicit status instead of an exception. Topologically invalid polygons are
   still measured but carry a note.
4. Totals are accumulated with `math.fsum`, and all values are rounded to 4 decimals.

`compute_measurements()` refuses to run with a geographic CRS, so degree-based measurements cannot happen by accident.

### CRS handling

- **Native CRS is preserved and reported.** KML is always EPSG:4326 by specification. Shapefiles take their CRS from
  the `.prj`; registry matches are reported as `EPSG:xxxx`.
- **Dynamic UTM matcher:** the layer extent is transformed to EPSG:4326, its centre is computed (antimeridian-aware),
  and `pyproj.database.query_utm_crs_info` returns the WGS 84 / UTM zone covering that point (`EPSG:326xx` north,
  `EPSG:327xx` south). Beyond 84°N / 80°S the UPS polar CRS is used.
- **Works for any input CRS:** a layer already in a projected CRS is still measured in the UTM zone of its centre, so the
  same parcel gives the same answer whatever CRS it was delivered in (covered by a test).
- **Missing `.prj`:** EPSG:4326 is assumed *only if* every coordinate is a valid longitude/latitude, and a warning is
  attached. Otherwise (for example projected metres) the upload is rejected with `422` rather than measured wrongly.
- **Multi-zone layers:** one CRS is used for the whole layer so totals are consistent, and a warning is returned in
  `projection.warnings` when the layer spans several UTM zones.

---

## Design Decisions

| Decision | Why | Alternatives considered |
|---|---|---|
| **FastAPI** | Small, focused API; Pydantic models and OpenAPI for free; a plain async upload path. | Django + DRF: its ORM/admin/migrations would mostly sit unused for three endpoints, and GeoDjango adds GDAL/PostGIS setup. |
| **Project to UTM, then planar math** | Matches the requirement to use a projected CRS, and is fast and vectorizable. Accuracy is typically well under 0.1% inside a zone. | `pyproj.Geod` geodesic area/length: more accurate over large extents with no projection choice. Used as the independent reference in the tests; a good future "high accuracy" mode. Per-feature UTM zones: more accurate for huge or multi-zone layers, but totals become harder to explain. |
| **One UTM zone per layer (from the centre)** | Simple, deterministic and explainable; warns when the layer is too wide. | Per-feature zone selection. |
| **`fiona` for Shapefile, `lxml` for KML** | `fiona` exposes the CRS and feature iteration cleanly. A direct KML parser gives full control over `<ExtendedData>`, `MultiGeometry` and per-placemark error handling. | Reading KML through the GDAL KML driver: its availability varies by build and it loses `ExtendedData` detail. |
| **Synchronous processing in a threadpool** | The CPU-bound work does not block the event loop, and the `POST` returns the final state, which suits the spec's `status` field. | Background tasks / Celery with polling: more moving parts than needed now (see Future Scope). The `PENDING/PROCESSING/FAILED` states are already modelled for it. |
| **SQLite + SQLAlchemy, three tables** | Zero-setup persistence for files, features and a per-file measurement run. One transaction per upload. | In-memory dict (lost on restart); PostGIS (overkill here). |
| **Geometry stored as GeoJSON text in the native CRS** | Portable, directly returnable, no spatial extension required. | WKB/WKT columns; PostGIS geometry columns. |
| **Per-feature failure tolerance for KML** | One bad `<coordinates>` should not discard an otherwise good file. The feature is kept as `INVALID` with a warning. | Rejecting the whole file. |
| **Strict ZIP handling** | Zip-slip, zip-bomb and ambiguity are real upload risks. Only whitelisted component extensions are extracted, under fixed names, with a bounded byte budget. | Extracting everything with `extractall`. |
| **Hardened XML parser** | `resolve_entities=False`, no DTD loading, no network, which blocks XXE and entity-expansion attacks. | Default parser settings. |

---

## Notes on accuracy and scope

- Measurements are **planar in UTM and ignore elevation (Z)**. KML altitude values are dropped on ingestion.
- On the sample parcel above, the UTM area is within about 0.04% of the geodesic area; tests assert agreement within 0.5%.
- Shapefile geometry types `MultiPoint`, `MultiLineString` and `MultiPolygon` are supported. A `GeometryCollection`
  (possible from KML `MultiGeometry` with mixed members) is reported as `UNSUPPORTED`.
- Only one Shapefile per ZIP is accepted. `.kmz` is not supported.
- The Dockerfile follows the standard wheels-only approach for this stack. If your platform has no wheels for
  fiona / pyogrio / shapely, install GDAL, GEOS and PROJ system packages in the image first.

---

## Learnings

- **CRS bugs are silent.** A degree-based area looks like a plausible small number. The test that a ~1 km parcel yields
  ~1.2 million m² (not ~0.0001) guards against the most common geospatial mistake, and the geodesic cross-check catches
  subtler projection errors.
- **Zone boundaries are real.** A layer centred at exactly 78.0°E belongs to UTM zone 44, not 43. Wide layers need an
  explicit warning rather than a silent accuracy loss.
- **`geom_type` of a missing geometry is `NaN`, not `None`.** Without handling it, "nan" leaked into API output.
  Real-world files contain null and empty geometries, so each case needs an explicit status.
- **Treat uploads as hostile.** Archive paths, decompressed size and XML entities each needed an explicit defence.
- **Keep services framework-free.** Separating parsing, CRS selection and measurement from FastAPI made them
  straightforward to test against a reference implementation.

---

## Future Scope

- **Asynchronous processing:** move parsing to a task queue (Celery/RQ/ARQ + Redis) and return `202` with polling,
  using the existing `PENDING/PROCESSING` states. Needed for very large files.
- **PostGIS:** store native geometry columns for spatial queries (bounding-box search, intersections) and use
  `ST_Area(geography)` as a built-in cross-check.
- **Geodesic "high accuracy" mode:** a query flag to compute with `pyproj.Geod`, and per-feature UTM zones for layers
  spanning many zones.
- **More formats:** GeoJSON, KMZ, GeoPackage, multi-layer ZIPs.
- **More measurements:** polygon perimeter, centroid, bounding box, unit selection (hectares, acres, km, miles).
- **Production hardening:** authentication, rate limiting, upload virus scanning, object storage for originals,
  Alembic migrations, structured logging and metrics, retention/cleanup jobs.
- **Dashboard:** a map preview (for example MapLibre) of the uploaded geometries next to the measurements table.
- **Streaming ingestion** for files too large to hold in memory.
