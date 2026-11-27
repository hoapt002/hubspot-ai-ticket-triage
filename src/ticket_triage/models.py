"""Database schema."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class TicketStatus(enum.StrEnum):
    RECEIVED = "received"  # stored, waiting for the triage job
    AWAITING_REVIEW = "awaiting_review"  # AI proposal needs a person
    AWAITING_MANUAL = "awaiting_manual"  # manual triage from scratch
    TRIAGED = "triaged"  # final classification known, CRM sync pending
    SYNCED = "synced"  # HubSpot ticket created
    CRM_DISABLED = "crm_disabled"  # triaged; no CRM configured
    FAILED = "failed"  # CRM sync gave up after retries


class ProcessingPath(enum.StrEnum):
    AI_AUTO = "ai_auto"  # AI result accepted without a person
    AI_REVIEWED = "ai_reviewed"  # AI proposal verified by a person
    MANUAL = "manual"  # triaged by a person without seeing AI output


class ReviewKind(enum.StrEnum):
    VERIFY_AI = "verify_ai"
    MANUAL_TRIAGE = "manual_triage"


class ReviewStatus(enum.StrEnum):
    OPEN = "open"
    DONE = "done"


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_tickets_source_external_id"),
        Index("ix_tickets_status", "status"),
        Index("ix_tickets_received_at", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(255))
    channel: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    sender_email: Mapped[str | None] = mapped_column(String(320))
    sender_name: Mapped[str | None] = mapped_column(String(255))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    status: Mapped[TicketStatus] = mapped_column(
        _enum(TicketStatus, "ticket_status"), default=TicketStatus.RECEIVED
    )
    processing_path: Mapped[ProcessingPath | None] = mapped_column(
        _enum(ProcessingPath, "processing_path")
    )

    # Final triage result (from AI or from the reviewer).
    category: Mapped[str | None] = mapped_column(String(64))
    priority: Mapped[str | None] = mapped_column(String(8))
    team: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(16))
    sentiment: Mapped[str | None] = mapped_column(String(16))
    customer: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ai_confidence: Mapped[float | None]
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    crm_contact_id: Mapped[str | None] = mapped_column(String(64))
    crm_ticket_id: Mapped[str | None] = mapped_column(String(64))
    crm_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    crm_error: Mapped[str | None] = mapped_column(Text)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    triage_runs: Mapped[list[TriageRun]] = relationship(
        back_populates="ticket", order_by="TriageRun.created_at"
    )
    review_tasks: Mapped[list[ReviewTask]] = relationship(
        back_populates="ticket", order_by="ReviewTask.created_at"
    )


class TriageRun(Base):
    """One LLM call. Kept for audit, cost and accuracy measurement."""

    __tablename__ = "triage_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    shadow: Mapped[bool] = mapped_column(default=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticket: Mapped[Ticket] = relationship(back_populates="triage_runs")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewTask(Base):
    __tablename__ = "review_tasks"
    __table_args__ = (Index("ix_review_tasks_status_created", "status", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    kind: Mapped[ReviewKind] = mapped_column(_enum(ReviewKind, "review_kind"))
    status: Mapped[ReviewStatus] = mapped_column(
        _enum(ReviewStatus, "review_status"), default=ReviewStatus.OPEN
    )
    reasons: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    # The AI proposal shown to the reviewer (empty for manual triage).
    proposal: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    changed_fields: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticket: Mapped[Ticket] = relationship(back_populates="review_tasks")
    sessions: Mapped[list[ReviewSession]] = relationship(back_populates="task")


class ReviewSession(Base):
    """A reviewer opening a task. Submitted sessions give the human touch time."""

    __tablename__ = "review_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task: Mapped[ReviewTask] = relationship(back_populates="sessions")


class Job(Base):
    """Postgres-backed work queue (claimed with FOR UPDATE SKIP LOCKED)."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_ready", "run_after", postgresql_where="done_at IS NULL"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(Text)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
