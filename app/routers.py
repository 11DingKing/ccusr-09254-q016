"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .schemas import (
    DiffOut,
    DirectoryEntryOut,
    DirectoryUpsertIn,
    DirectoryUpsertOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    PrivacyRuleIn,
    PrivacyRuleOut,
    PublicSummaryMetaOut,
    PublicSummaryViewOut,
    SnapshotOut,
    StudentProgressOut,
    SummaryActionIn,
    SummaryCreateIn,
    SummaryOut,
    SummaryPreviewIn,
    SummaryPreviewOut,
)

router = APIRouter(prefix="/api")


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- 公开摘要：目录、隐私规则、预览/审批/发布/撤回/公开查询 -----------------

ROLE_ANALYST = "analyst"
ROLE_PRIVACY_OFFICER = "privacy_officer"
ROLE_AUDITOR = "auditor"


@dataclass(frozen=True)
class ActorContext:
    actor_id: str
    role: str


def _require_actor(
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> ActorContext:
    if not x_actor_id or not x_actor_id.strip():
        raise HTTPException(status_code=401, detail="missing X-Actor-Id header")
    if not x_actor_role or not x_actor_role.strip():
        raise HTTPException(status_code=401, detail="missing X-Actor-Role header")
    return ActorContext(actor_id=x_actor_id.strip(), role=x_actor_role.strip())


def _require_roles(*roles: str):
    allowed = frozenset(roles)

    def dependency(actor: ActorContext = Depends(_require_actor)) -> ActorContext:
        if actor.role not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"role '{actor.role}' is not allowed; requires one of {sorted(allowed)}",
            )
        return actor

    return dependency


@router.put(
    "/plans/{plan_version}/directory",
    response_model=DirectoryUpsertOut,
)
def put_directory(
    plan_version: str,
    body: DirectoryUpsertIn,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_roles(ROLE_ANALYST, ROLE_PRIVACY_OFFICER)),
) -> Any:
    try:
        return services.upsert_directory(
            db,
            plan_version=plan_version,
            entries=[e.model_dump() for e in body.entries],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/directory",
    response_model=list[DirectoryEntryOut],
)
def get_directory(
    plan_version: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(
        _require_roles(ROLE_ANALYST, ROLE_PRIVACY_OFFICER, ROLE_AUDITOR)
    ),
) -> Any:
    try:
        return services.read_directory(db, plan_version)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/privacy-rules",
    response_model=PrivacyRuleOut,
    status_code=status.HTTP_201_CREATED,
)
def post_privacy_rule(
    body: PrivacyRuleIn,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_roles(ROLE_PRIVACY_OFFICER)),
) -> Any:
    try:
        return services.create_privacy_rule(
            db,
            rule_version=body.rule_version,
            min_group_size=body.min_group_size,
            note=body.note,
            actor_id=actor.actor_id,
        )
    except services.RuleSetConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/privacy-rules/{rule_version}", response_model=PrivacyRuleOut)
def get_privacy_rule(
    rule_version: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(
        _require_roles(ROLE_ANALYST, ROLE_PRIVACY_OFFICER, ROLE_AUDITOR)
    ),
) -> Any:
    try:
        return services.read_privacy_rule(db, rule_version)
    except services.RuleSetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}/public-summaries/preview",
    response_model=SummaryPreviewOut,
)
def preview_public_summary(
    plan_version: str,
    freeze_id: str,
    body: SummaryPreviewIn,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_roles(ROLE_ANALYST, ROLE_PRIVACY_OFFICER)),
) -> Any:
    """预览公开摘要与隐私影响（不落库），发布前评估抑制范围。"""
    try:
        return services.preview_public_summary(
            db,
            plan_version=plan_version,
            freeze_id=freeze_id,
            rule_version=body.rule_version,
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.RuleSetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}/public-summaries",
    response_model=SummaryOut,
    status_code=status.HTTP_201_CREATED,
)
def create_public_summary(
    plan_version: str,
    freeze_id: str,
    body: SummaryCreateIn,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_roles(ROLE_ANALYST, ROLE_PRIVACY_OFFICER)),
) -> Any:
    try:
        return services.create_public_summary(
            db,
            plan_version=plan_version,
            freeze_id=freeze_id,
            summary_id=body.summary_id,
            rule_version=body.rule_version,
            actor_id=actor.actor_id,
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.RuleSetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.SummaryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/public-summaries/{summary_id}", response_model=SummaryOut)
def get_public_summary_internal(
    summary_id: str,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(
        _require_roles(ROLE_ANALYST, ROLE_PRIVACY_OFFICER, ROLE_AUDITOR)
    ),
) -> Any:
    """内部视图：含隐私影响与完整审计轨迹，撤回后仍可查。"""
    try:
        return services.get_public_summary_internal(db, summary_id)
    except services.SummaryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _do_transition(
    summary_id: str,
    action: str,
    body: SummaryActionIn,
    db: Session,
    actor: ActorContext,
) -> Any:
    try:
        return services.transition_summary(
            db,
            summary_id=summary_id,
            action=action,
            actor_id=actor.actor_id,
            reason=body.reason,
        )
    except services.SummaryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.SummaryStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except services.SummaryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/public-summaries/{summary_id}/approve", response_model=SummaryOut)
def approve_public_summary(
    summary_id: str,
    body: SummaryActionIn,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_roles(ROLE_PRIVACY_OFFICER)),
) -> Any:
    return _do_transition(summary_id, "approve", body, db, actor)


@router.post("/public-summaries/{summary_id}/publish", response_model=SummaryOut)
def publish_public_summary(
    summary_id: str,
    body: SummaryActionIn,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_roles(ROLE_PRIVACY_OFFICER)),
) -> Any:
    return _do_transition(summary_id, "publish", body, db, actor)


@router.post("/public-summaries/{summary_id}/withdraw", response_model=SummaryOut)
def withdraw_public_summary(
    summary_id: str,
    body: SummaryActionIn,
    db: Session = Depends(get_db),
    actor: ActorContext = Depends(_require_roles(ROLE_PRIVACY_OFFICER)),
) -> Any:
    return _do_transition(summary_id, "withdraw", body, db, actor)


@router.get("/public/summaries", response_model=list[PublicSummaryMetaOut])
def list_public_summaries(
    plan_version: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    """公开列表：仅已发布的摘要，无需身份。"""
    return services.list_public_summaries_public(db, plan_version=plan_version)


@router.get("/public/summaries/{summary_id}", response_model=PublicSummaryViewOut)
def get_public_summary_public(
    summary_id: str,
    organization: str | None = Query(default=None),
    category: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    """公开查询：按组织/类别交叉筛选，仅返回抑制后的公开边界。"""
    view = services.get_public_summary_public(
        db, summary_id, organization=organization, category=category
    )
    if view is None:
        raise HTTPException(status_code=404, detail="public summary not found")
    return view
