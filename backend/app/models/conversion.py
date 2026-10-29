from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.user import utcnow


class ConversionJob(Base):
    __tablename__ = "conversion_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    guest_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    tool: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    input_format: Mapped[str] = mapped_column(String(16), nullable=False)
    output_format: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    # Fine-grained progress stage, e.g. queued -> converting -> packaging.
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True, default="queued")
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    output_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    output_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    output_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Batch conversion fields
    zip_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    zip_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    batch_results_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    options_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pages_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    user = relationship("User", back_populates="jobs")
    logs = relationship(
        "ConversionLog", back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_conversion_jobs_user_created", "user_id", "created_at"),
        Index("ix_conversion_jobs_guest_created", "guest_id", "created_at"),
    )


class ConversionLog(Base):
    __tablename__ = "conversion_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversion_job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversion_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job = relationship("ConversionJob", back_populates="logs")