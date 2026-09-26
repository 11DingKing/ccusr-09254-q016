"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .compliance import public_summaries as pub
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .repository import (
    get_freeze,
    get_plan,
    get_privacy_rule,
    get_public_summary,
    insert_events,
    insert_freeze,
    insert_privacy_rule,
    insert_public_summary,
    insert_summary_audit,
    list_directory,
    list_public_summaries,
    list_summary_audit,
    load_events,
    load_events_up_to,
    max_event_id,
    transition_public_summary,
    upsert_directory_entries,
    upsert_plan,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=events
    )
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
    }


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)


# --- 公开摘要：目录、隐私规则、预览/审批/发布/撤回/公开查询 -----------------


class RuleSetNotFoundError(Exception):
    pass


class RuleSetConflictError(Exception):
    pass


class SummaryNotFoundError(Exception):
    pass


class SummaryConflictError(Exception):
    pass


class SummaryStateError(Exception):
    pass


def upsert_directory(
    db: Session, *, plan_version: str, entries: list[dict[str, str]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    count = upsert_directory_entries(db, plan_version=plan_version, entries=entries)
    return {"plan_version": plan_version, "upserted": count}


def read_directory(db: Session, plan_version: str) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    return [
        {
            "student_id": row.student_id,
            "organization": row.organization,
            "category": row.category,
        }
        for row in list_directory(db, plan_version)
    ]


def create_privacy_rule(
    db: Session,
    *,
    rule_version: str,
    min_group_size: int,
    note: str,
    actor_id: str,
) -> dict[str, Any]:
    row = insert_privacy_rule(
        db,
        rule_version=rule_version,
        min_group_size=min_group_size,
        note=note,
        created_by=actor_id,
    )
    if row is None:
        raise RuleSetConflictError(
            f"privacy rule version '{rule_version}' already exists and is immutable"
        )
    return _rule_to_dict(row)


def read_privacy_rule(db: Session, rule_version: str) -> dict[str, Any]:
    row = get_privacy_rule(db, rule_version)
    if row is None:
        raise RuleSetNotFoundError(f"privacy rule version '{rule_version}' not found")
    return _rule_to_dict(row)


def _rule_to_dict(row: Any) -> dict[str, Any]:
    return {
        "rule_version": row.rule_version,
        "min_group_size": row.min_group_size,
        "note": row.note,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat(),
    }


def _compute_summary_payload(
    db: Session, plan_version: str, freeze_id: str, rule_version: str
) -> tuple[dict[str, Any], str]:
    """读取冻结快照与目录，按指定规则版本做确定性聚合。"""
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    rule = get_privacy_rule(db, rule_version)
    if rule is None:
        raise RuleSetNotFoundError(f"privacy rule version '{rule_version}' not found")
    students = [
        pub.StudentStat(
            student_id=s["student_id"],
            total_seconds=s["total_seconds"],
            meets_requirement=s["meets_requirement"],
        )
        for s in snap.students
    ]
    directory = [
        pub.DirectoryEntry(
            student_id=row.student_id,
            organization=row.organization,
            category=row.category,
        )
        for row in list_directory(db, plan_version)
    ]
    payload = pub.build_public_summary(
        students, directory, min_group_size=rule.min_group_size
    )
    return payload, pub.content_hash(payload)


def preview_public_summary(
    db: Session, *, plan_version: str, freeze_id: str, rule_version: str
) -> dict[str, Any]:
    """预览公开摘要与隐私影响，不落库。"""
    payload, digest = _compute_summary_payload(db, plan_version, freeze_id, rule_version)
    return {
        "plan_version": plan_version,
        "freeze_id": freeze_id,
        "rule_version": rule_version,
        "content_hash": digest,
        **payload,
    }


def create_public_summary(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    summary_id: str,
    rule_version: str,
    actor_id: str,
) -> dict[str, Any]:
    """以草稿状态固化公开摘要；载荷与规则版本在创建时锁定。"""
    payload, digest = _compute_summary_payload(db, plan_version, freeze_id, rule_version)
    row = insert_public_summary(
        db,
        summary_id=summary_id,
        plan_version=plan_version,
        freeze_id=freeze_id,
        rule_version=rule_version,
        payload=payload,
        privacy_impact=payload["privacy_impact"],
        content_hash=digest,
        created_by=actor_id,
    )
    if row is None:
        raise SummaryConflictError(f"public summary '{summary_id}' already exists")
    audit = pub.build_audit_entry(
        sequence=1,
        action="create",
        actor_id=actor_id,
        from_state="-",
        to_state="draft",
        reason="create draft from frozen snapshot",
    )
    insert_summary_audit(db, summary_id=summary_id, **audit)
    return _summary_to_dict(db, row)


def get_public_summary_internal(db: Session, summary_id: str) -> dict[str, Any]:
    row = get_public_summary(db, summary_id)
    if row is None:
        raise SummaryNotFoundError(f"public summary '{summary_id}' not found")
    return _summary_to_dict(db, row)


def transition_summary(
    db: Session, *, summary_id: str, action: str, actor_id: str, reason: str
) -> dict[str, Any]:
    """审批/发布/撤回：状态机校验 + 条件更新，并发下仅一个请求成功。"""
    row = get_public_summary(db, summary_id)
    if row is None:
        raise SummaryNotFoundError(f"public summary '{summary_id}' not found")
    try:
        target = pub.check_transition(row.state, action)
    except pub.DomainError as exc:
        raise SummaryStateError(str(exc)) from exc
    stamp = {"publish": "published_at", "withdraw": "withdrawn_at"}.get(action)
    updated = transition_public_summary(
        db,
        summary_id=summary_id,
        from_state=row.state,
        to_state=target.value,
        stamp_field=stamp,
    )
    if updated is None:
        raise SummaryConflictError(
            f"public summary '{summary_id}' changed concurrently; retry '{action}'"
        )
    sequence = len(list_summary_audit(db, summary_id)) + 1
    audit = pub.build_audit_entry(
        sequence=sequence,
        action=action,
        actor_id=actor_id,
        from_state=row.state,
        to_state=target.value,
        reason=reason,
    )
    insert_summary_audit(db, summary_id=summary_id, **audit)
    return _summary_to_dict(db, updated)


def get_public_summary_public(
    db: Session,
    summary_id: str,
    *,
    organization: str | None = None,
    category: str | None = None,
) -> dict[str, Any] | None:
    """公开查询：仅发布中可见，且只返回抑制后的公开边界。"""
    row = get_public_summary(db, summary_id)
    if row is None or row.state != "published":
        return None
    view = pub.filter_public_view(
        row.payload, organization=organization, category=category
    )
    return {
        **pub.summarize_for_public(
            {
                "summary_id": row.summary_id,
                "plan_version": row.plan_version,
                "freeze_id": row.freeze_id,
                "rule_version": row.rule_version,
                "published_at": row.published_at.isoformat() if row.published_at else None,
            }
        ),
        **view,
    }


def list_public_summaries_public(
    db: Session, *, plan_version: str | None = None
) -> list[dict[str, Any]]:
    rows = list_public_summaries(db, plan_version=plan_version, published_only=True)
    return [
        {
            "summary_id": row.summary_id,
            "plan_version": row.plan_version,
            "freeze_id": row.freeze_id,
            "rule_version": row.rule_version,
            "published_at": row.published_at.isoformat() if row.published_at else None,
        }
        for row in rows
    ]


def _summary_to_dict(db: Session, row: Any) -> dict[str, Any]:
    return {
        "summary_id": row.summary_id,
        "plan_version": row.plan_version,
        "freeze_id": row.freeze_id,
        "rule_version": row.rule_version,
        "state": row.state,
        "version": row.version,
        "content_hash": row.content_hash,
        "cells": row.payload["cells"],
        "organization_totals": row.payload["organization_totals"],
        "grand_total": row.payload["grand_total"],
        "privacy_impact": row.privacy_impact,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "withdrawn_at": row.withdrawn_at.isoformat() if row.withdrawn_at else None,
        "audit": [
            {
                "sequence": a.sequence,
                "action": a.action,
                "actor_id": a.actor_id,
                "from_state": a.from_state,
                "to_state": a.to_state,
                "reason": a.reason,
                "fingerprint": a.fingerprint,
                "created_at": a.created_at.isoformat(),
            }
            for a in list_summary_audit(db, row.summary_id)
        ],
    }
