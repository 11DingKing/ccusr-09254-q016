"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import Freeze, Plan, PublicSummary, StudentProfile

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


def upsert_student_profiles(
    db: Session,
    *,
    plan_version: str,
    profiles: list[dict[str, Any]],
) -> int:
    """批量写入学生组织画像（同 plan+student 覆盖更新）。"""

    count = 0
    for profile in profiles:
        stmt = sqlite_insert(StudentProfile).values(
            plan_version=plan_version,
            student_id=profile["student_id"],
            organization=profile["organization"],
            labels=dict(profile.get("labels", {})),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["plan_version", "student_id"],
            set_={
                "organization": profile["organization"],
                "labels": dict(profile.get("labels", {})),
            },
        )
        db.execute(stmt)
        count += 1
    db.commit()
    return count


def load_student_profiles(
    db: Session, plan_version: str
) -> dict[str, Any]:
    stmt = select(StudentProfile).where(
        StudentProfile.plan_version == plan_version
    )
    rows = db.execute(stmt).scalars().all()
    return {row.student_id: row for row in rows}


def get_public_summary(
    db: Session, plan_version: str, summary_id: str
) -> PublicSummary | None:
    stmt = select(PublicSummary).where(
        PublicSummary.plan_version == plan_version,
        PublicSummary.summary_id == summary_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def list_public_summaries(
    db: Session,
    plan_version: str,
    *,
    freeze_id: str | None = None,
    states: list[str] | None = None,
) -> list[PublicSummary]:
    stmt = select(PublicSummary).where(
        PublicSummary.plan_version == plan_version
    )
    if freeze_id is not None:
        stmt = stmt.where(PublicSummary.freeze_id == freeze_id)
    if states:
        stmt = stmt.where(PublicSummary.state.in_(states))
    stmt = stmt.order_by(PublicSummary.created_at.desc(), PublicSummary.summary_id)
    return list(db.execute(stmt).scalars().all())


def get_published_summary_for_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> PublicSummary | None:
    stmt = select(PublicSummary).where(
        PublicSummary.plan_version == plan_version,
        PublicSummary.freeze_id == freeze_id,
        PublicSummary.state == "published",
    )
    return db.execute(stmt).scalar_one_or_none()


def list_current_published(db: Session, plan_version: str) -> list[PublicSummary]:
    """返回某培养方案当前对外可见（已发布且未撤回）的摘要。"""

    stmt = (
        select(PublicSummary)
        .where(
            PublicSummary.plan_version == plan_version,
            PublicSummary.state == "published",
        )
        .order_by(PublicSummary.published_at.desc())
    )
    return list(db.execute(stmt).scalars().all())


def insert_public_summary(db: Session, row: PublicSummary) -> PublicSummary | None:
    """插入摘要，summary_id 冲突时返回 None。"""

    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(row)
    return row


def conditional_update_public_summary(
    db: Session,
    *,
    plan_version: str,
    summary_id: str,
    expected_state: str,
    changes: dict[str, Any],
) -> PublicSummary | None:
    """基于状态的比较并更新（CAS）；状态已被并发改动时返回 None。

    用单条条件 UPDATE 实现原子状态转移：SQLite 写锁会串行化并发事务，
    rowcount==0 即表示期望状态已被其他事务改变。
    """

    row = get_public_summary(db, plan_version, summary_id)
    if row is None or row.state != expected_state:
        return None
    stmt = (
        update(PublicSummary)
        .where(
            PublicSummary.plan_version == plan_version,
            PublicSummary.summary_id == summary_id,
            PublicSummary.state == expected_state,
        )
        .values(**changes)
    )
    try:
        result = db.execute(stmt)
        if result.rowcount != 1:
            db.rollback()
            return None
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return get_public_summary(db, plan_version, summary_id)
