"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


class StudentProfileIn(BaseModel):
    student_id: str = Field(..., min_length=1, max_length=128)
    organization: str = Field(..., min_length=1, max_length=128)
    labels: dict[str, str] = Field(default_factory=dict)


class StudentProfileBatchIn(BaseModel):
    profiles: list[StudentProfileIn]


class ProfileUpsertResult(BaseModel):
    accepted: int


class SummaryPreviewIn(BaseModel):
    freeze_id: str = Field(..., min_length=1, max_length=128)
    summary_id: str = Field(..., min_length=1, max_length=128)
    category_dimension: str = Field("compliance", min_length=1, max_length=64)
    min_cell_size: int = Field(5, ge=1, le=10_000)
    suppression_margin: int = Field(0, ge=0, le=10_000)
    round_increment: int = Field(1, ge=1, le=1000)


class SummarySubmitIn(BaseModel):
    acknowledged: bool
    document_fingerprint: str = Field(..., min_length=1, max_length=96)


class SummaryApprovalIn(BaseModel):
    note: str = Field("", max_length=512)


class SummaryWithdrawIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=512)


class AuditEntryOut(BaseModel):
    sequence: int
    action: str
    actor_id: str
    occurred_at: str
    detail: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str


class PublicSummaryOut(BaseModel):
    """内部视图：含状态、隐私影响评估与审计链。"""

    plan_version: str
    freeze_id: str
    summary_id: str
    state: str
    category_dimension: str
    ruleset_version: str
    privacy_rules: dict[str, Any]
    document: dict[str, Any]
    privacy_impact: dict[str, Any]
    document_fingerprint: str
    created_by: str
    created_at: str
    submitted_by: str | None
    submitted_at: str | None
    approved_by: str | None
    approved_at: str | None
    published_by: str | None
    published_at: str | None
    withdrawn_by: str | None
    withdrawn_at: str | None
    withdrawal_reason: str | None
    audit_log: list[AuditEntryOut]


class PublicSummaryListItem(BaseModel):
    plan_version: str
    freeze_id: str
    summary_id: str
    state: str
    category_dimension: str
    ruleset_version: str
    document_fingerprint: str
    created_at: str
    published_at: str | None
    withdrawn_at: str | None


class PublicDocumentOut(BaseModel):
    """对外视图：只含已发布文档本身，无任何内部字段。"""

    plan_version: str
    freeze_id: str
    ruleset_version: str
    rules: dict[str, Any]
    category_dimension: str
    coverage: str
    suppression_applied: bool
    organizations: list[dict[str, Any]]
    totals: dict[str, Any] | None
    filters: dict[str, Any] | None
    document_fingerprint: str
    published_at: str | None = None
    summary_id: str | None = None
