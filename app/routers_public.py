"""公开摘要相关 API。

内部工作流接口（预览/审批/发布/撤回）要求 ``X-Actor-Id`` 与 ``X-Actor-Roles``
请求头；``/api/public/...`` 下的公开查询接口不鉴权，且只暴露已发布文档。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from . import services_public as sp
from .core.public_aggregation import PrivacyRules
from .db import get_db
from .schemas import (
    ProfileUpsertResult,
    PublicDocumentOut,
    PublicSummaryListItem,
    PublicSummaryOut,
    StudentProfileBatchIn,
    SummaryApprovalIn,
    SummaryPreviewIn,
    SummarySubmitIn,
    SummaryWithdrawIn,
)
from .security import Actor, AuthorizationError, resolve_actor

internal_router = APIRouter(prefix="/api")
public_router = APIRouter(prefix="/api/public")


def _guard(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, sp.SummaryNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, sp.SummaryStateConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, sp.PublishConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    message = str(exc)
    if "not registered" in message or "does not exist" in message:
        return HTTPException(status_code=404, detail=message)
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=message)
    return HTTPException(status_code=400, detail=message)


@internal_router.put(
    "/plans/{plan_version}/student-profiles",
    response_model=ProfileUpsertResult,
)
def put_student_profiles(
    plan_version: str,
    body: StudentProfileBatchIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(resolve_actor),
) -> Any:
    try:
        count = sp.replace_profiles(
            db,
            actor=actor,
            plan_version=plan_version,
            profiles=[p.model_dump() for p in body.profiles],
        )
    except Exception as exc:  # noqa: BLE001 - mapped into HTTP status codes
        raise _guard(exc) from exc
    return {"accepted": count}


@internal_router.post(
    "/plans/{plan_version}/public-summaries/{summary_id}/preview",
    response_model=PublicSummaryOut,
)
def preview_summary(
    plan_version: str,
    summary_id: str,
    body: SummaryPreviewIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(resolve_actor),
) -> Any:
    try:
        rules = PrivacyRules.from_values(
            min_cell_size=body.min_cell_size,
            suppression_margin=body.suppression_margin,
            round_increment=body.round_increment,
        )
        row = sp.create_or_refresh_preview(
            db,
            actor=actor,
            plan_version=plan_version,
            freeze_id=body.freeze_id,
            summary_id=summary_id,
            category_dimension=body.category_dimension,
            rules=rules,
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    return sp.serialize_internal(row)


@internal_router.get(
    "/plans/{plan_version}/public-summaries",
    response_model=list[PublicSummaryListItem],
)
def list_summaries(
    plan_version: str,
    freeze_id: str | None = Query(default=None),
    state: list[str] | None = Query(default=None),
    db: Session = Depends(get_db),
    actor: Actor = Depends(resolve_actor),
) -> Any:
    try:
        return sp.list_internal(
            db,
            actor=actor,
            plan_version=plan_version,
            freeze_id=freeze_id,
            states_filter=state,
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@internal_router.get(
    "/plans/{plan_version}/public-summaries/{summary_id}",
    response_model=PublicSummaryOut,
)
def get_summary(
    plan_version: str,
    summary_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(resolve_actor),
) -> Any:
    try:
        return sp.get_internal(
            db, actor=actor, plan_version=plan_version, summary_id=summary_id
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@internal_router.post(
    "/plans/{plan_version}/public-summaries/{summary_id}/submit",
    response_model=PublicSummaryOut,
)
def submit_summary(
    plan_version: str,
    summary_id: str,
    body: SummarySubmitIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(resolve_actor),
) -> Any:
    try:
        row = sp.submit_for_approval(
            db,
            actor=actor,
            plan_version=plan_version,
            summary_id=summary_id,
            acknowledged=body.acknowledged,
            document_fingerprint=body.document_fingerprint,
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    return sp.serialize_internal(row)


@internal_router.post(
    "/plans/{plan_version}/public-summaries/{summary_id}/approve",
    response_model=PublicSummaryOut,
)
def approve_summary(
    plan_version: str,
    summary_id: str,
    body: SummaryApprovalIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(resolve_actor),
) -> Any:
    try:
        row = sp.approve(
            db,
            actor=actor,
            plan_version=plan_version,
            summary_id=summary_id,
            note=body.note,
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    return sp.serialize_internal(row)


@internal_router.post(
    "/plans/{plan_version}/public-summaries/{summary_id}/reject",
    response_model=PublicSummaryOut,
)
def reject_summary(
    plan_version: str,
    summary_id: str,
    body: SummaryApprovalIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(resolve_actor),
) -> Any:
    try:
        row = sp.reject_to_preview(
            db,
            actor=actor,
            plan_version=plan_version,
            summary_id=summary_id,
            note=body.note,
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    return sp.serialize_internal(row)


@internal_router.post(
    "/plans/{plan_version}/public-summaries/{summary_id}/publish",
    response_model=PublicSummaryOut,
)
def publish_summary(
    plan_version: str,
    summary_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(resolve_actor),
) -> Any:
    try:
        row = sp.publish(
            db, actor=actor, plan_version=plan_version, summary_id=summary_id
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    return sp.serialize_internal(row)


@internal_router.post(
    "/plans/{plan_version}/public-summaries/{summary_id}/withdraw",
    response_model=PublicSummaryOut,
)
def withdraw_summary(
    plan_version: str,
    summary_id: str,
    body: SummaryWithdrawIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(resolve_actor),
) -> Any:
    try:
        row = sp.withdraw(
            db,
            actor=actor,
            plan_version=plan_version,
            summary_id=summary_id,
            reason=body.reason,
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
    return sp.serialize_internal(row)


@public_router.get(
    "/plans/{plan_version}/summaries",
    response_model=list[PublicDocumentOut],
)
def public_list_summaries(
    plan_version: str,
    organization: str | None = Query(default=None),
    category: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return sp.public_list(
            db,
            plan_version,
            organization=organization,
            category=category,
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc


@public_router.get(
    "/plans/{plan_version}/summaries/{summary_id}",
    response_model=PublicDocumentOut,
)
def public_get_summary(
    plan_version: str,
    summary_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return sp.public_get(db, plan_version, summary_id)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc) from exc
