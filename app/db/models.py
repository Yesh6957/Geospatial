"""SQLAlchemy declarative models.

Three tables track an upload end to end:

* ``uploaded_files``     - one row per upload and its processing state.
* ``features``           - one row per feature: geometry (GeoJSON, native CRS),
                           properties and the computed measurement.
* ``measurement_runs``   - one row per file summarising the analytical run:
                           projected CRS used, layer centre and totals.

Timestamps are stored as naive UTC datetimes (SQLite has no timezone type).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (stored and read back consistently)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_file_id() -> str:
    return uuid.uuid4().hex


class FileStatus(str, Enum):
    """Lifecycle of an uploaded file."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Base(DeclarativeBase):
    pass


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_file_id)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_format: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FileStatus.PENDING.value, index=True
    )
    feature_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    crs: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    features: Mapped[list["Feature"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", passive_deletes=True
    )
    measurement_run: Mapped["MeasurementRun | None"] = relationship(
        back_populates="file", cascade="all, delete-orphan", passive_deletes=True, uselist=False
    )


class Feature(Base):
    __tablename__ = "features"
    __table_args__ = (
        UniqueConstraint("file_id", "feature_index", name="uq_features_file_index"),
        Index("ix_features_file_index", "file_id", "feature_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("uploaded_files.id", ondelete="CASCADE"), nullable=False
    )
    feature_index: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    geometry_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    geometry_geojson: Mapped[str | None] = mapped_column(Text, nullable=True)
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    measurement_status: Mapped[str] = mapped_column(String(16), nullable=False)
    area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    length_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    measurement_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    file: Mapped[UploadedFile] = relationship(back_populates="features")


class MeasurementRun(Base):
    __tablename__ = "measurement_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("uploaded_files.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    projected_crs: Mapped[str] = mapped_column(String(64), nullable=False)
    utm_zone: Mapped[int] = mapped_column(Integer, nullable=False)
    hemisphere: Mapped[str] = mapped_column(String(1), nullable=False)
    center_lon: Mapped[float] = mapped_column(Float, nullable=False)
    center_lat: Mapped[float] = mapped_column(Float, nullable=False)
    total_area_m2: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_length_m: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    polygon_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    point_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unsupported_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    file: Mapped[UploadedFile] = relationship(back_populates="measurement_run")
