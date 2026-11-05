"""Durable job state. Workflow transitions are implemented by later stories."""
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, FetchedValue, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Job(Base):
    __tablename__ = 'jobs'
    __mapper_args__ = {'eager_defaults': True}
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'running', 'completed', 'failed', 'cancelled')", name='ck_jobs_status'),
        CheckConstraint("length(btrim(team_id)) > 0 AND length(btrim(created_by)) > 0", name='ck_jobs_owner'),
        CheckConstraint("length(btrim(scan_type)) > 0", name='ck_jobs_scan_type'),
        CheckConstraint("jsonb_typeof(parameters) = 'object'", name='ck_jobs_parameters'),
        CheckConstraint('attempt_count >= 0', name='ck_jobs_attempt_count'),
        CheckConstraint('(claim_token IS NULL) = (lease_expires_at IS NULL)', name='ck_jobs_lease'),
        CheckConstraint("(status IN ('completed', 'failed', 'cancelled')) = (finished_at IS NOT NULL)", name='ck_jobs_finished'),
        CheckConstraint("status <> 'running' OR started_at IS NOT NULL", name='ck_jobs_started'),
        CheckConstraint(
            "(status = 'completed' AND result IS NOT NULL AND jsonb_typeof(result) = 'object' AND error IS NULL) OR "
            "(status = 'failed' AND result IS NULL AND error IS NOT NULL AND jsonb_typeof(error) = 'object') OR "
            "(status IN ('pending', 'running', 'cancelled') AND result IS NULL AND error IS NULL)",
            name='ck_jobs_outcome',
        ),
        Index('ix_jobs_team_created', 'team_id', text('created_at DESC'), text('job_id DESC')),
        Index('ix_jobs_recovery', 'status', 'lease_expires_at'),
    )

    job_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    team_id: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text)
    scan_type: Mapped[str] = mapped_column(Text)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default='pending')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), server_onupdate=FetchedValue(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    attempt_count: Mapped[int] = mapped_column(Integer, server_default='0')
    worker_id: Mapped[str | None] = mapped_column(Text)
    claim_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Reserved storage for US-011.1; cancellation decisions are not implemented here.
    cancellation_requested_by: Mapped[str | None] = mapped_column(Text)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
