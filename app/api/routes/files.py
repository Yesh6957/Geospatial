"""File upload, status and measurement endpoints.

``POST /api/files/`` reads the upload asynchronously (with a hard size cap),
then runs the CPU-bound parsing/measuring work in a worker thread so the event
loop stays responsive. Everything produced by a successful run (file row,
features, measurement run) is committed in **one transaction**; a failed run
leaves only a ``FAILED`` file row with the reason.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated, Any

import shapely
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from app.db.models import Feature, FileStatus, MeasurementRun, UploadedFile, utcnow
from app.db.session import get_db
from app.schemas.schemas import (
    FeatureMeasurementOut,
    FileOut,
    MeasurementResult,
    MeasurementsResponse,
    MeasurementSummary,
    ProjectionInfo,
)
from app.services.crs import CRSResolutionError, UTMSelection, select_utm_crs
from app.services.file_reader import (
    MAX_UPLOAD_BYTES,
    FileReadError,
    ParsedLayer,
    UnsupportedFormatError,
    detect_format,
    read_geospatial_file,
)
from app.services.measurements import MeasurementReport, MeasurementStatus, compute_measurements

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/files", tags=["files"])

READ_CHUNK_BYTES = 1024 * 1024
INSERT_BATCH_SIZE = 1000

DbSession = Annotated[Session, Depends(get_db)]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clean_filename(raw: str | None) -> str:
    """Keep only the base name of the client-supplied file name."""
    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    return name[:255] if name else "upload"


async def _read_upload(upload: UploadFile) -> bytes:
    """Read the upload in chunks, aborting once the size limit is exceeded."""
    buffer = bytearray()
    while True:
        chunk = await upload.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. The limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            )
    if not buffer:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    return bytes(buffer)


def _mark_failed(db: Session, file_id: str, message: str) -> UploadedFile:
    record = db.get(UploadedFile, file_id)
    if record is None:  # pragma: no cover - the row was created moments ago
        raise RuntimeError("Upload record disappeared while marking it as failed.")
    record.status = FileStatus.FAILED.value
    record.error_message = message[:2000]
    record.completed_at = utcnow()
    db.commit()
    return record


def _persist_results(
    db: Session,
    record: UploadedFile,
    layer: ParsedLayer,
    selection: UTMSelection,
    report: MeasurementReport,
) -> None:
    """Stage features and the measurement run on the open transaction."""
    geometries = list(layer.gdf.geometry)
    source_ids = layer.gdf["source_id"].tolist()
    attributes = layer.gdf["attributes"].tolist()

    rows: list[dict[str, Any]] = []
    for measured in report.features:
        index = measured.feature_index
        geometry = geometries[index]
        rows.append(
            {
                "file_id": record.id,
                "feature_index": index,
                "source_id": source_ids[index],
                "geometry_type": measured.geometry_type,
                "geometry_geojson": None if geometry is None else shapely.to_geojson(geometry),
                "properties": attributes[index],
                "measurement_status": measured.status.value,
                "area_m2": measured.area_m2,
                "length_m": measured.length_m,
                "measurement_note": measured.note,
            }
        )

    for start in range(0, len(rows), INSERT_BATCH_SIZE):
        db.execute(insert(Feature), rows[start : start + INSERT_BATCH_SIZE])

    totals = report.totals
    db.add(
        MeasurementRun(
            file_id=record.id,
            projected_crs=selection.label,
            utm_zone=selection.zone,
            hemisphere=selection.hemisphere,
            center_lon=selection.center_lon,
            center_lat=selection.center_lat,
            total_area_m2=totals.total_area_m2,
            total_length_m=totals.total_length_m,
            polygon_count=totals.polygon_count,
            line_count=totals.line_count,
            point_count=totals.point_count,
            unsupported_count=totals.unsupported_count,
            invalid_count=totals.invalid_count,
            warnings=list(selection.warnings),
        )
    )


def _process_upload(db: Session, filename: str, file_format: str, content: bytes) -> UploadedFile:
    """Synchronous pipeline: parse -> choose UTM -> measure -> persist."""
    record = UploadedFile(
        filename=filename,
        file_format=file_format,
        size_bytes=len(content),
        status=FileStatus.PENDING.value,
        warnings=[],
    )
    db.add(record)
    db.commit()

    try:
        record.status = FileStatus.PROCESSING.value
        db.commit()

        layer = read_geospatial_file(file_format, content)
        selection = select_utm_crs(layer.gdf)
        report = compute_measurements(layer.gdf, selection.crs)

        record.feature_count = len(report.features)
        record.crs = layer.crs_label
        record.warnings = list(layer.warnings)
        _persist_results(db, record, layer, selection, report)
        record.status = FileStatus.COMPLETED.value
        record.completed_at = utcnow()
        db.commit()
        return record

    except (FileReadError, CRSResolutionError) as exc:
        db.rollback()
        failed = _mark_failed(db, record.id, str(exc))
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "file_id": failed.id, "status": FileStatus.FAILED.value},
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error while processing upload %s", record.id)
        db.rollback()
        failed = _mark_failed(db, record.id, "Internal error while processing the file.")
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Internal error while processing the file.",
                "file_id": failed.id,
                "status": FileStatus.FAILED.value,
            },
        ) from exc


def _get_file_or_404(db: Session, file_id: str) -> UploadedFile:
    record = db.get(UploadedFile, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"File '{file_id}' was not found.")
    return record


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.post(
    "/",
    response_model=FileOut,
    status_code=201,
    summary="Upload and process a Shapefile (.zip) or KML (.kml)",
    responses={
        400: {"description": "Empty upload."},
        413: {"description": "File exceeds the size limit."},
        415: {"description": "Unsupported file type."},
        422: {"description": "The file could not be read as valid geospatial data."},
    },
)
async def upload_file(
    db: DbSession,
    file: Annotated[UploadFile, File(description="A .zip containing a Shapefile, or a .kml file.")],
) -> UploadedFile:
    filename = _clean_filename(file.filename)
    try:
        file_format = detect_format(filename)
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    content = await _read_upload(file)
    return await run_in_threadpool(_process_upload, db, filename, file_format, content)


@router.get(
    "/{file_id}/",
    response_model=FileOut,
    summary="Information about an uploaded file",
    responses={404: {"description": "Unknown file id."}},
)
def get_file(file_id: str, db: DbSession) -> UploadedFile:
    return _get_file_or_404(db, file_id)


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    summary="Per-feature measurements for an uploaded file",
    responses={
        404: {"description": "Unknown file id."},
        409: {"description": "The file has not been processed successfully."},
    },
)
def get_measurements(
    file_id: str,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=10000, description="Maximum features to return.")] = 1000,
    offset: Annotated[int, Query(ge=0, description="Number of features to skip.")] = 0,
    include_geometry: Annotated[
        bool, Query(description="Include each feature's GeoJSON geometry.")
    ] = True,
) -> MeasurementsResponse:
    record = _get_file_or_404(db, file_id)
    if record.status != FileStatus.COMPLETED.value:
        raise HTTPException(
            status_code=409,
            detail={
                "message": record.error_message
                or f"File is not ready for measurements (status {record.status}).",
                "file_id": record.id,
                "status": record.status,
            },
        )

    run = db.scalar(select(MeasurementRun).where(MeasurementRun.file_id == record.id))
    total = db.scalar(
        select(func.count()).select_from(Feature).where(Feature.file_id == record.id)
    )
    rows = db.scalars(
        select(Feature)
        .where(Feature.file_id == record.id)
        .order_by(Feature.feature_index)
        .offset(offset)
        .limit(limit)
    ).all()

    features = [
        FeatureMeasurementOut(
            feature_index=row.feature_index,
            source_id=row.source_id,
            geometry_type=row.geometry_type,
            crs=record.crs,
            properties=row.properties or {},
            geometry=(
                json.loads(row.geometry_geojson)
                if include_geometry and row.geometry_geojson
                else None
            ),
            measurement=MeasurementResult(
                status=MeasurementStatus(row.measurement_status),
                area_m2=row.area_m2,
                length_m=row.length_m,
                note=row.measurement_note,
            ),
        )
        for row in rows
    ]

    projection = summary = None
    if run is not None:
        projection = ProjectionInfo(
            projected_crs=run.projected_crs,
            utm_zone=run.utm_zone,
            hemisphere=run.hemisphere,
            center_lon=run.center_lon,
            center_lat=run.center_lat,
            warnings=run.warnings or [],
        )
        summary = MeasurementSummary(
            total_area_m2=run.total_area_m2,
            total_length_m=run.total_length_m,
            polygon_count=run.polygon_count,
            line_count=run.line_count,
            point_count=run.point_count,
            unsupported_count=run.unsupported_count,
            invalid_count=run.invalid_count,
        )

    return MeasurementsResponse(
        file_id=record.id,
        filename=record.filename,
        status=FileStatus(record.status),
        native_crs=record.crs,
        projection=projection,
        summary=summary,
        total_features=total or 0,
        limit=limit,
        offset=offset,
        features=features,
    )
