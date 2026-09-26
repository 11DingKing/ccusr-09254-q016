"""公开摘要的应用服务：生命周期状态机、职责分离与审计链。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from sqlalchemy.orm import Session

from .core.public_aggregation import (
    PrivacyRules,
    Profile,
    apply_public_filters,
    build_public_summary,
    fingerprint_document,
)
from .core.snapshot import Snapshot
from .models import PublicSummary
from .repository import (
    conditional_update_public_summary,
    get_freeze,
    get_plan,
    get_public_summary,
    get_published_summary_for_freeze,
    insert_public_summary,
    list_current_published,
    list_public_summaries,
    load_student_profiles,
    upsert_student_profiles,
)
from .security import (
    ROLE_APPROVER,
    ROLE_AUDITOR,
    ROLE_PRIVACY_OFFICER,
    ROLE_PUBLISHER,
    Actor,
    require_roles,
)

STATE_PREVIEW = "preview"
STATE_PENDING = "pending_approval"
STATE_APPROVED = "approved"
STATE_PUBLISHED = "published"
STATE_WITHDRAWN = "withdrawn"

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_PREVIEW: frozenset({STATE_PENDING}),
    STATE_PENDING: frozenset({STATE_APPROVED, STATE_PREVIEW}),
    STATE_APPROVED: frozenset({STATE_PUBLISHED}),
    STATE_PUBLISHED: frozenset({STATE_WITHDRAWN}),
    STATE_WITHDRAWN: frozenset(),
}

_INTERNAL_ROLES = (
    ROLE_PRIVACY_OFFICER,
    ROLE_APPROVER,
    ROLE_PUBLISHER,
    ROLE_AUDITOR,
)


class PublicSummaryError(Exception):
    pass


class SummaryNotFoundError(PublicSummaryError):
    pass


class SummaryStateConflict(PublicSummaryError):
    pass


class PublishConflictError(PublicSummaryError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _next_audit(
    log: list[dict[str, Any]],
    *,
    action: str,
    actor_id: str,
    detail: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """追加一条带哈希链的审计条目（前一条指纹参与本条哈希）。"""

    moment = (occurred_at or _utcnow()).astimezone(timezone.utc)
    sequence = len(log) + 1
    previous = log[-1]["fingerprint"] if log else "GENESIS"
    detail_json = json.dumps(
        detail or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    raw = f"{previous}|{sequence}|{action}|{actor_id}|{detail_json}".encode("utf-8")
    return {
        "sequence": sequence,
        "action": action,
        "actor_id": actor_id,
        "occurred_at": _iso(moment),
        "detail": detail or {},
        "fingerprint": "sha256:" + sha256(raw).hexdigest(),
    }


def verify_audit_chain(log: list[dict[str, Any]]) -> bool:
    """校验审计链序号连续、哈希链接完整且无重复指纹。"""

    previous = "GENESIS"
    seen: set[str] = set()
    for index, entry in enumerate(log, start=1):
        if entry.get("sequence") != index:
            return False
        detail_json = json.dumps(
            entry.get("detail", {}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        raw = (
            f"{previous}|{index}|{entry['action']}|{entry['actor_id']}|"
            f"{detail_json}"
        ).encode("utf-8")
        expected = "sha256:" + sha256(raw).hexdigest()
        if entry.get("fingerprint") != expected or entry["fingerprint"] in seen:
            return False
        seen.add(entry["fingerprint"])
        previous = entry["fingerprint"]
    return True


def _require_plan_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PublicSummaryError(f"plan version '{plan_version}' is not registered")
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise PublicSummaryError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def replace_profiles(
    db: Session,
    *,
    actor: Actor,
    plan_version: str,
    profiles: list[dict[str, Any]],
) -> int:
    require_roles(actor, ROLE_PRIVACY_OFFICER)
    if get_plan(db, plan_version) is None:
        raise PublicSummaryError(f"plan version '{plan_version}' is not registered")
    return upsert_student_profiles(
        db, plan_version=plan_version, profiles=profiles
    )


def _load_profile_map(db: Session, plan_version: str) -> dict[str, Profile]:
    rows = load_student_profiles(db, plan_version)
    return {
        sid: Profile(organization=row.organization, labels=dict(row.labels or {}))
        for sid, row in rows.items()
    }


def create_or_refresh_preview(
    db: Session,
    *,
    actor: Actor,
    plan_version: str,
    freeze_id: str,
    summary_id: str,
    category_dimension: str,
    rules: PrivacyRules,
) -> PublicSummary:
    require_roles(actor, ROLE_PRIVACY_OFFICER)
    snapshot = _require_plan_freeze(db, plan_version, freeze_id)
    profiles = _load_profile_map(db, plan_version)
    result = build_public_summary(
        snapshot.to_dict(),
        profiles,
        rules=rules,
        category_dimension=category_dimension,
    )
    document = result["document"]
    impact = result["privacy_impact"]
    now = _utcnow()

    existing = get_public_summary(db, plan_version, summary_id)
    if existing is None:
        row = PublicSummary(
            plan_version=plan_version,
            freeze_id=freeze_id,
            summary_id=summary_id,
            state=STATE_PREVIEW,
            category_dimension=category_dimension,
            ruleset_version=rules.ruleset_version,
            privacy_rules=rules.to_dict(),
            document=document,
            privacy_impact=impact,
            document_fingerprint=document["document_fingerprint"],
            created_by=actor.actor_id,
            created_at=now,
            audit_log=[],
        )
        row.audit_log = [
            _next_audit(
                [],
                action="preview_created",
                actor_id=actor.actor_id,
                detail={
                    "freeze_id": freeze_id,
                    "ruleset_version": rules.ruleset_version,
                    "category_dimension": category_dimension,
                    "min_cell_size": rules.min_cell_size,
                    "suppression_margin": rules.suppression_margin,
                    "round_increment": rules.round_increment,
                    "organizations_published": impact["organizations_published"],
                    "organizations_suppressed_count": len(
                        impact["organizations_suppressed"]
                    ),
                    "cells_primary_suppressed": impact[
                        "cells_primary_suppressed"
                    ],
                    "cells_complement_suppressed": impact[
                        "cells_complement_suppressed"
                    ],
                },
                occurred_at=now,
            )
        ]
        stored = insert_public_summary(db, row)
        if stored is None:
            raise SummaryStateConflict(
                f"summary '{summary_id}' already exists for plan '{plan_version}'"
            )
        return stored

    # 规则升级不改写旧摘要：仅允许刷新仍处于 preview 的草稿。
    if existing.state != STATE_PREVIEW:
        raise SummaryStateConflict(
            f"summary '{summary_id}' is {existing.state}; existing summaries are "
            "immutable once submitted — create a new summary id for rule changes"
        )
    if existing.freeze_id != freeze_id:
        raise SummaryStateConflict(
            f"summary '{summary_id}' is bound to freeze '{existing.freeze_id}'"
        )
    existing.category_dimension = category_dimension
    existing.ruleset_version = rules.ruleset_version
    existing.privacy_rules = rules.to_dict()
    existing.document = document
    existing.privacy_impact = impact
    existing.document_fingerprint = document["document_fingerprint"]
    log = list(existing.audit_log or [])
    log.append(
        _next_audit(
            log,
            action="preview_refreshed",
            actor_id=actor.actor_id,
            detail={
                "ruleset_version": rules.ruleset_version,
                "min_cell_size": rules.min_cell_size,
                "suppression_margin": rules.suppression_margin,
                "document_fingerprint": document["document_fingerprint"],
            },
            occurred_at=now,
        )
    )
    existing.audit_log = log
    db.commit()
    db.refresh(existing)
    return existing


def _load_managed(
    db: Session, plan_version: str, summary_id: str
) -> PublicSummary:
    row = get_public_summary(db, plan_version, summary_id)
    if row is None:
        raise SummaryNotFoundError(
            f"summary '{summary_id}' for plan '{plan_version}' does not exist"
        )
    return row


def _assert_transition(row: PublicSummary, target: str) -> None:
    if target not in ALLOWED_TRANSITIONS.get(row.state, frozenset()):
        raise SummaryStateConflict(
            f"cannot transition summary from {row.state} to {target}"
        )


def _verify_document_integrity(row: PublicSummary) -> None:
    recomputed = fingerprint_document(row.document)
    if recomputed != row.document_fingerprint:
        raise PublicSummaryError(
            "stored document fails fingerprint verification"
        )


def submit_for_approval(
    db: Session,
    *,
    actor: Actor,
    plan_version: str,
    summary_id: str,
    acknowledged: bool,
    document_fingerprint: str,
) -> PublicSummary:
    require_roles(actor, ROLE_PRIVACY_OFFICER)
    row = _load_managed(db, plan_version, summary_id)
    _assert_transition(row, STATE_PENDING)
    if not acknowledged:
        raise PublicSummaryError(
            "submitter must acknowledge the privacy impact assessment"
        )
    if document_fingerprint != row.document_fingerprint:
        raise PublicSummaryError(
            "document_fingerprint does not match the generated preview"
        )
    _verify_document_integrity(row)
    now = _utcnow()
    log = list(row.audit_log or [])
    log.append(
        _next_audit(
            log,
            action="submitted",
            actor_id=actor.actor_id,
            detail={
                "acknowledged_privacy_impact": True,
                "document_fingerprint": row.document_fingerprint,
            },
            occurred_at=now,
        )
    )
    updated = conditional_update_public_summary(
        db,
        plan_version=plan_version,
        summary_id=summary_id,
        expected_state=STATE_PREVIEW,
        changes={
            "state": STATE_PENDING,
            "submitted_at": now,
            "submitted_by": actor.actor_id,
            "audit_log": log,
        },
    )
    if updated is None:
        raise SummaryStateConflict("summary state changed concurrently")
    return updated


def reject_to_preview(
    db: Session,
    *,
    actor: Actor,
    plan_version: str,
    summary_id: str,
    note: str,
) -> PublicSummary:
    """驳回：审批人将待批摘要退回预览，审计链保留。"""

    require_roles(actor, ROLE_APPROVER)
    row = _load_managed(db, plan_version, summary_id)
    _assert_transition(row, STATE_PREVIEW)
    now = _utcnow()
    log = list(row.audit_log or [])
    log.append(
        _next_audit(
            log,
            action="rejected",
            actor_id=actor.actor_id,
            detail={"note": note},
            occurred_at=now,
        )
    )
    updated = conditional_update_public_summary(
        db,
        plan_version=plan_version,
        summary_id=summary_id,
        expected_state=STATE_PENDING,
        changes={"state": STATE_PREVIEW, "audit_log": log},
    )
    if updated is None:
        raise SummaryStateConflict("summary state changed concurrently")
    return updated


def approve(
    db: Session,
    *,
    actor: Actor,
    plan_version: str,
    summary_id: str,
    note: str,
) -> PublicSummary:
    require_roles(actor, ROLE_APPROVER)
    row = _load_managed(db, plan_version, summary_id)
    _assert_transition(row, STATE_APPROVED)
    if row.submitted_by == actor.actor_id:
        raise PublicSummaryError(
            "approver must not be the same actor who submitted the summary"
        )
    _verify_document_integrity(row)
    now = _utcnow()
    log = list(row.audit_log or [])
    log.append(
        _next_audit(
            log,
            action="approved",
            actor_id=actor.actor_id,
            detail={
                "note": note,
                "document_fingerprint": row.document_fingerprint,
                "ruleset_version": row.ruleset_version,
            },
            occurred_at=now,
        )
    )
    updated = conditional_update_public_summary(
        db,
        plan_version=plan_version,
        summary_id=summary_id,
        expected_state=STATE_PENDING,
        changes={
            "state": STATE_APPROVED,
            "approved_at": now,
            "approved_by": actor.actor_id,
            "approval_note": note,
            "audit_log": log,
        },
    )
    if updated is None:
        raise SummaryStateConflict("summary state changed concurrently")
    return updated


def publish(
    db: Session,
    *,
    actor: Actor,
    plan_version: str,
    summary_id: str,
) -> PublicSummary:
    require_roles(actor, ROLE_PUBLISHER)
    row = _load_managed(db, plan_version, summary_id)
    _assert_transition(row, STATE_PUBLISHED)
    _verify_document_integrity(row)

    other = get_published_summary_for_freeze(db, plan_version, row.freeze_id)
    if other is not None and other.summary_id != summary_id:
        raise PublishConflictError(
            f"freeze '{row.freeze_id}' already has published summary "
            f"'{other.summary_id}'; withdraw it first"
        )

    now = _utcnow()
    log = list(row.audit_log or [])
    log.append(
        _next_audit(
            log,
            action="published",
            actor_id=actor.actor_id,
            detail={
                "document_fingerprint": row.document_fingerprint,
                "ruleset_version": row.ruleset_version,
            },
            occurred_at=now,
        )
    )
    # 条件 UPDATE 保证状态机；部分唯一索引保证同一冻结至多一个已发布摘要。
    updated = conditional_update_public_summary(
        db,
        plan_version=plan_version,
        summary_id=summary_id,
        expected_state=STATE_APPROVED,
        changes={
            "state": STATE_PUBLISHED,
            "published_at": now,
            "published_by": actor.actor_id,
            "audit_log": log,
        },
    )
    if updated is None:
        raise PublishConflictError(
            f"freeze '{row.freeze_id}' already has a published summary or the "
            "summary state changed concurrently"
        )
    return updated


def withdraw(
    db: Session,
    *,
    actor: Actor,
    plan_version: str,
    summary_id: str,
    reason: str,
) -> PublicSummary:
    """撤回公开摘要。撤回后记录与完整审计链仍保留供内部审计。"""

    require_roles(actor, ROLE_PUBLISHER, ROLE_PRIVACY_OFFICER)
    row = _load_managed(db, plan_version, summary_id)
    _assert_transition(row, STATE_WITHDRAWN)
    if not reason.strip():
        raise PublicSummaryError("withdrawal reason is required")
    now = _utcnow()
    log = list(row.audit_log or [])
    log.append(
        _next_audit(
            log,
            action="withdrawn",
            actor_id=actor.actor_id,
            detail={"reason": reason.strip()},
            occurred_at=now,
        )
    )
    updated = conditional_update_public_summary(
        db,
        plan_version=plan_version,
        summary_id=summary_id,
        expected_state=STATE_PUBLISHED,
        changes={
            "state": STATE_WITHDRAWN,
            "withdrawn_at": now,
            "withdrawn_by": actor.actor_id,
            "withdrawal_reason": reason.strip(),
            "audit_log": log,
        },
    )
    if updated is None:
        raise SummaryStateConflict("summary state changed concurrently")
    return updated


def serialize_internal(row: PublicSummary) -> dict[str, Any]:
    return {
        "plan_version": row.plan_version,
        "freeze_id": row.freeze_id,
        "summary_id": row.summary_id,
        "state": row.state,
        "category_dimension": row.category_dimension,
        "ruleset_version": row.ruleset_version,
        "privacy_rules": dict(row.privacy_rules or {}),
        "document": dict(row.document),
        "privacy_impact": dict(row.privacy_impact or {}),
        "document_fingerprint": row.document_fingerprint,
        "created_by": row.created_by,
        "created_at": _iso(row.created_at),
        "submitted_by": row.submitted_by,
        "submitted_at": _iso(row.submitted_at),
        "approved_by": row.approved_by,
        "approved_at": _iso(row.approved_at),
        "published_by": row.published_by,
        "published_at": _iso(row.published_at),
        "withdrawn_by": row.withdrawn_by,
        "withdrawn_at": _iso(row.withdrawn_at),
        "withdrawal_reason": row.withdrawal_reason,
        "audit_log": list(row.audit_log or []),
    }


def serialize_list_item(row: PublicSummary) -> dict[str, Any]:
    return {
        "plan_version": row.plan_version,
        "freeze_id": row.freeze_id,
        "summary_id": row.summary_id,
        "state": row.state,
        "category_dimension": row.category_dimension,
        "ruleset_version": row.ruleset_version,
        "document_fingerprint": row.document_fingerprint,
        "created_at": _iso(row.created_at),
        "published_at": _iso(row.published_at),
        "withdrawn_at": _iso(row.withdrawn_at),
    }


def get_internal(
    db: Session, *, actor: Actor, plan_version: str, summary_id: str
) -> dict[str, Any]:
    require_roles(actor, *_INTERNAL_ROLES)
    return serialize_internal(_load_managed(db, plan_version, summary_id))


def list_internal(
    db: Session,
    *,
    actor: Actor,
    plan_version: str,
    freeze_id: str | None,
    states_filter: list[str] | None,
) -> list[dict[str, Any]]:
    require_roles(actor, *_INTERNAL_ROLES)
    rows = list_public_summaries(
        db, plan_version, freeze_id=freeze_id, states=states_filter
    )
    return [serialize_list_item(r) for r in rows]


def _serialize_public_document(row: PublicSummary) -> dict[str, Any]:
    doc = dict(row.document)
    doc["summary_id"] = row.summary_id
    doc["published_at"] = _iso(row.published_at)
    return doc


def public_get(
    db: Session, plan_version: str, summary_id: str
) -> dict[str, Any]:
    row = _load_managed(db, plan_version, summary_id)
    if row.state != STATE_PUBLISHED:
        raise SummaryNotFoundError("summary is not publicly available")
    return _serialize_public_document(row)


def public_list(
    db: Session,
    plan_version: str,
    *,
    organization: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    rows = list_current_published(db, plan_version)
    documents: list[dict[str, Any]] = []
    for row in rows:
        doc = _serialize_public_document(row)
        if organization is not None or category is not None:
            doc = apply_public_filters(
                doc, organization=organization, category=category
            )
            doc["summary_id"] = row.summary_id
            doc["published_at"] = _iso(row.published_at)
            if not doc["organizations"]:
                continue
        documents.append(doc)
    return documents
