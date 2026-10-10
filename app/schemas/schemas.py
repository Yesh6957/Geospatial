"""Pydantic models describing the API responses."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.db.models import FileStatus
from app.services.measurements import MeasurementStatus


def _iso_utc(value: datetime | None) -> str | None:
    """Serialise naive-UTC datetimes as ISO 8601 with a trailing ``Z``."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class FileOut(_APIModel):
    """Top-level information about an uploaded file."""

    id: str = Field(examples=["3f2b9c1e8a7d4c5e9b1a2d3c4e5f6a7b"])
    filename: str = Field(examples=["survey.kml"])
    file_format: str = Field(description="SHAPEFILE or KML.", examples=["KML"])
    size_bytes: int
    feature_count: int = Field(examples=[120])
    crs: str | None = Field(default=None, description="Native CRS of the file.", examples=["EPSG:4326"])
    status: FileStatus = Field(examples=["COMPLETED"])
    error_message: str | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    completed_at: datetime | None = None

    @field_validator("warnings", mode="before")
    @classmethod
    def _none_to_empty(cls, value: Any) -> Any:
        return [] if value is None else value

    @field_serializer("created_at", "completed_at")
    def _serialize_datetime(self, value: datetime | None) -> str | None:
        return _iso_utc(value)


class MeasurementResult(BaseModel):
    """Computed metrics for a single feature (metres / square metres)."""

    status: MeasurementStatus
    area_m2: float | None = Field(default=None, description="Polygon area in square metres.")
    length_m: float | None = Field(default=None, description="LineString length in metres.")
    note: str | None = None


class FeatureMeasurementOut(BaseModel):
    """One feature with its core parameters and measurements."""

    feature_index: int
    source_id: str | None = None
    geometry_type: str | None = None
    crs: str | None = Field(default=None, description="Native CRS of the feature geometry.")
    properties: dict[str, Any] = Field(default_factory=dict)
    geometry: dict[str, Any] | None = Field(
        default=None, description="GeoJSON geometry in the native CRS (omit with include_geometry=false)."
    )
    measurement: MeasurementResult


class ProjectionInfo(BaseModel):
    """How the layer was projected before measuring."""

    projected_crs: str = Field(examples=["EPSG:32643"])
    utm_zone: int
    hemisphere: str
    center_lon: float
    center_lat: float
    warnings: list[str] = Field(default_factory=list)


class MeasurementSummary(BaseModel):
    """Aggregate figures for the whole file."""

    total_area_m2: float
    total_length_m: float
    polygon_count: int
    line_count: int
    point_count: int
    unsupported_count: int
    invalid_count: int


class MeasurementsResponse(BaseModel):
    """Response of ``GET /api/files/{id}/measurements/``."""

    file_id: str
    filename: str
    status: FileStatus
    native_crs: str | None = None
    projection: ProjectionInfo | None = None
    summary: MeasurementSummary | None = None
    total_features: int
    limit: int
    offset: int
    features: list[FeatureMeasurementOut]
