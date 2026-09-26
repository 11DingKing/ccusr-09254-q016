"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    __tablename__ = "plans"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    required_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("required_seconds >= 0", name="ck_plans_required_nonneg"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "plan_version", name="uq_events_event_id_plan"),
        Index("ix_events_plan_student", "plan_version", "student_id"),
    )


class Freeze(Base):
    __tablename__ = "freezes"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    freeze_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class StudentProfile(Base):
    """学生的组织归属与公开类别标签（内部数据，不对外公开）。"""

    __tablename__ = "student_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False)
    organization: Mapped[str] = mapped_column(String(128), nullable=False)
    labels: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "plan_version", "student_id", name="uq_student_profiles_plan_student"
        ),
        Index("ix_student_profiles_plan_org", "plan_version", "organization"),
    )


class PublicSummary(Base):
    """冻结快照的公开聚合摘要：预览 → 审批 → 发布 → 撤回。"""

    __tablename__ = "public_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False)
    freeze_id: Mapped[str] = mapped_column(String(128), nullable=False)
    summary_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="preview")
    category_dimension: Mapped[str] = mapped_column(String(64), nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(32), nullable=False)
    privacy_rules: Mapped[dict] = mapped_column(JSON, nullable=False)
    document: Mapped[dict] = mapped_column(JSON, nullable=False)
    privacy_impact: Mapped[dict] = mapped_column(JSON, nullable=False)
    document_fingerprint: Mapped[str] = mapped_column(String(96), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approval_note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    withdrawn_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    withdrawal_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    audit_log: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    __table_args__ = (
        UniqueConstraint(
            "plan_version", "summary_id", name="uq_public_summaries_plan_summary"
        ),
        # 同一冻结快照至多存在一个已发布摘要（历史版本以 withdrawn 留存审计）。
        Index(
            "ux_public_summaries_one_published",
            "plan_version",
            "freeze_id",
            unique=True,
            sqlite_where=text("state = 'published'"),
        ),
        Index(
            "ix_public_summaries_plan_freeze",
            "plan_version",
            "freeze_id",
            "state",
        ),
    )
