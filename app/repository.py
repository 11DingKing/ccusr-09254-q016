"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import (
    Event as EventModel,
)
from .models import (
    Freeze,
    Plan,
    PrivacyRuleSet,
    PublicSummary,
    PublicSummaryAudit,
    StudentDirectoryEntry,
)


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


# --- 公开摘要：目录、隐私规则、摘要生命周期 -------------------------------


def upsert_directory_entries(
    db: Session,
    *,
    plan_version: str,
    entries: list[dict[str, str]],
) -> int:
    """批量写入学生目录映射，返回受影响行数。"""
    count = 0
    for e in entries:
        stmt = sqlite_insert(StudentDirectoryEntry).values(
            plan_version=plan_version,
            student_id=e["student_id"],
            organization=e["organization"],
            category=e["category"],
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["plan_version", "student_id"],
            set_={
                "organization": e["organization"],
                "category": e["category"],
                "updated_at": datetime.now(timezone.utc),
            },
        )
        db.execute(stmt)
        count += 1
    db.commit()
    return count


def list_directory(db: Session, plan_version: str) -> list[StudentDirectoryEntry]:
    stmt = (
        select(StudentDirectoryEntry)
        .where(StudentDirectoryEntry.plan_version == plan_version)
        .order_by(StudentDirectoryEntry.student_id)
    )
    return list(db.execute(stmt).scalars().all())


def insert_privacy_rule(
    db: Session,
    *,
    rule_version: str,
    min_group_size: int,
    note: str,
    created_by: str,
) -> PrivacyRuleSet | None:
    """插入隐私规则版本；已存在时返回 None（规则不可变，拒绝覆盖）。"""
    stmt = sqlite_insert(PrivacyRuleSet).values(
        rule_version=rule_version,
        min_group_size=min_group_size,
        note=note,
        created_by=created_by,
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["rule_version"]).returning(
        PrivacyRuleSet.rule_version
    )
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return db.get(PrivacyRuleSet, rule_version)


def get_privacy_rule(db: Session, rule_version: str) -> PrivacyRuleSet | None:
    return db.get(PrivacyRuleSet, rule_version)


def insert_public_summary(
    db: Session,
    *,
    summary_id: str,
    plan_version: str,
    freeze_id: str,
    rule_version: str,
    payload: dict[str, Any],
    privacy_impact: dict[str, Any],
    content_hash: str,
    created_by: str,
) -> PublicSummary | None:
    """创建草稿摘要；summary_id 冲突时返回 None。"""
    now = datetime.now(timezone.utc)
    stmt = sqlite_insert(PublicSummary).values(
        summary_id=summary_id,
        plan_version=plan_version,
        freeze_id=freeze_id,
        rule_version=rule_version,
        state="draft",
        payload=payload,
        privacy_impact=privacy_impact,
        content_hash=content_hash,
        version=1,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["summary_id"]).returning(
        PublicSummary.summary_id
    )
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return db.get(PublicSummary, summary_id)


def get_public_summary(db: Session, summary_id: str) -> PublicSummary | None:
    return db.get(PublicSummary, summary_id)


def transition_public_summary(
    db: Session,
    *,
    summary_id: str,
    from_state: str,
    to_state: str,
    stamp_field: str | None = None,
) -> PublicSummary | None:
    """基于当前状态的条件更新，保证并发下只有一个请求能完成迁移。"""
    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {
        "state": to_state,
        "updated_at": now,
        "version": PublicSummary.version + 1,
    }
    if stamp_field == "published_at":
        values["published_at"] = now
    elif stamp_field == "withdrawn_at":
        values["withdrawn_at"] = now
    stmt = (
        update(PublicSummary)
        .where(PublicSummary.summary_id == summary_id)
        .where(PublicSummary.state == from_state)
        .values(**values)
    )
    result = db.execute(stmt)
    if result.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    return db.get(PublicSummary, summary_id)


def insert_summary_audit(
    db: Session,
    *,
    summary_id: str,
    sequence: int,
    action: str,
    actor_id: str,
    from_state: str,
    to_state: str,
    reason: str,
    fingerprint: str,
) -> None:
    db.add(
        PublicSummaryAudit(
            summary_id=summary_id,
            sequence=sequence,
            action=action,
            actor_id=actor_id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            fingerprint=fingerprint,
        )
    )
    db.commit()


def list_summary_audit(db: Session, summary_id: str) -> list[PublicSummaryAudit]:
    stmt = (
        select(PublicSummaryAudit)
        .where(PublicSummaryAudit.summary_id == summary_id)
        .order_by(PublicSummaryAudit.sequence)
    )
    return list(db.execute(stmt).scalars().all())


def list_public_summaries(
    db: Session, *, plan_version: str | None = None, published_only: bool = False
) -> Sequence[PublicSummary]:
    stmt = select(PublicSummary).order_by(PublicSummary.summary_id)
    if plan_version is not None:
        stmt = stmt.where(PublicSummary.plan_version == plan_version)
    if published_only:
        stmt = stmt.where(PublicSummary.state == "published")
    return db.execute(stmt).scalars().all()
