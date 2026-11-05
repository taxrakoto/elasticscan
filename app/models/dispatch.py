"""One durable dispatch intent per job; payload comes from immutable job fields."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DispatchIntent(Base):
    __tablename__ = 'dispatch_intents'
    __table_args__ = (
        CheckConstraint('schema_version > 0', name='ck_dispatch_schema_version'),
        CheckConstraint('attempt_count >= 0', name='ck_dispatch_attempt_count'),
        CheckConstraint('(claim_token IS NULL) = (lease_expires_at IS NULL)', name='ck_dispatch_lease'),
        Index('ix_dispatch_due', 'next_attempt_at', postgresql_where=text('published_at IS NULL AND suppressed_at IS NULL')),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey('jobs.job_id', ondelete='CASCADE'), unique=True)
    schema_version: Mapped[int] = mapped_column(Integer, server_default='1')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, server_default='0')
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claim_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(Text)
