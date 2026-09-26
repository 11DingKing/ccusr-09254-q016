"""专业层面公开摘要的纯领域逻辑。

对冻结快照按 (组织, 类别) 聚合，应用最小样本抑制与一致性补充抑制，
并计算发布前的隐私影响。模块不触碰数据库，同样输入必然得到同样输出。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any, Iterable, Mapping

REASON_MIN_SAMPLE = "min_sample"
REASON_CONSISTENCY = "consistency"


class SummaryState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    PUBLISHED = "published"
    WITHDRAWN = "withdrawn"


ALLOWED_TRANSITIONS: Mapping[SummaryState, frozenset[SummaryState]] = {
    SummaryState.DRAFT: frozenset({SummaryState.APPROVED, SummaryState.WITHDRAWN}),
    SummaryState.APPROVED: frozenset({SummaryState.PUBLISHED, SummaryState.WITHDRAWN}),
    SummaryState.PUBLISHED: frozenset({SummaryState.WITHDRAWN}),
    SummaryState.WITHDRAWN: frozenset(),
}


class DomainError(ValueError):
    """封装领域状态与业务约束。"""


def check_transition(current: str, action: str) -> SummaryState:
    """校验生命周期动作并返回目标状态。"""
    targets = {
        "approve": SummaryState.APPROVED,
        "publish": SummaryState.PUBLISHED,
        "withdraw": SummaryState.WITHDRAWN,
    }
    target = targets.get(action)
    if target is None:
        raise DomainError(f"未知动作: {action}")
    try:
        state = SummaryState(current)
    except ValueError as exc:
        raise DomainError(f"未知状态: {current}") from exc
    if target not in ALLOWED_TRANSITIONS.get(state, frozenset()):
        raise DomainError(f"不允许从 {state} 执行 {action}")
    return target


@dataclass(frozen=True)
class StudentStat:
    """冻结快照中单个学生的达标指标。"""

    student_id: str
    total_seconds: int
    meets_requirement: bool


@dataclass(frozen=True)
class DirectoryEntry:
    """学生到 (组织, 类别) 的映射。"""

    student_id: str
    organization: str
    category: str


@dataclass
class _Group:
    """待聚合单元：单元格、组织合计或全校合计。"""

    key: tuple[str, str]
    student_count: int = 0
    compliant_count: int = 0
    total_seconds_sum: int = 0
    suppressed: bool = False
    suppression_reason: str | None = None

    def absorb(self, stat: StudentStat) -> None:
        self.student_count += 1
        self.compliant_count += 1 if stat.meets_requirement else 0
        self.total_seconds_sum += stat.total_seconds

    def merge(self, other: "_Group") -> None:
        self.student_count += other.student_count
        self.compliant_count += other.compliant_count
        self.total_seconds_sum += other.total_seconds_sum


def _suppress(group: _Group, reason: str) -> None:
    if group.suppressed:
        return
    group.suppressed = True
    group.suppression_reason = reason


def _apply_suppression(
    cells: list[_Group],
    org_totals: list[_Group],
    grand_total: _Group,
    min_group_size: int,
) -> None:
    """最小样本抑制 + 一致性补充抑制（确定性，单遍分层处理）。"""
    for group in [*cells, *org_totals, grand_total]:
        if group.student_count < min_group_size:
            _suppress(group, REASON_MIN_SAMPLE)

    def complement(members: list[_Group], total: _Group) -> None:
        suppressed = [m for m in members if m.suppressed]
        if not suppressed:
            return
        if len(members) == 1:
            # 唯一成员被抑制时，合计即该成员，必须一并抑制。
            _suppress(total, REASON_CONSISTENCY)
            return
        if len(suppressed) == 1 and not total.suppressed:
            # 仅抑制一格时可由合计反推，补充抑制最小的已发布格。
            published = [m for m in members if not m.suppressed]
            target = min(published, key=lambda m: (m.student_count, m.key))
            _suppress(target, REASON_CONSISTENCY)

    by_org: dict[str, list[_Group]] = {}
    for cell in cells:
        by_org.setdefault(cell.key[0], []).append(cell)
    totals_by_org = {t.key[0]: t for t in org_totals}
    for org in sorted(by_org):
        complement(by_org[org], totals_by_org[org])
    complement(list(org_totals), grand_total)


def _public_view(group: _Group, level: str) -> dict[str, Any]:
    """序列化聚合单元；被抑制的单元不携带任何数值。"""
    if level == "cell":
        out: dict[str, Any] = {"organization": group.key[0], "category": group.key[1]}
    elif level == "organization":
        out = {"organization": group.key[0]}
    else:
        out = {}
    out["suppressed"] = group.suppressed
    if group.suppressed:
        out["suppression_reason"] = group.suppression_reason
        return out
    out["student_count"] = group.student_count
    out["compliant_count"] = group.compliant_count
    out["total_seconds_sum"] = group.total_seconds_sum
    out["avg_total_seconds"] = round(group.total_seconds_sum / group.student_count, 2)
    out["compliance_rate"] = round(group.compliant_count / group.student_count, 4)
    return out


def build_public_summary(
    students: Iterable[StudentStat],
    directory: Iterable[DirectoryEntry],
    *,
    min_group_size: int,
) -> dict[str, Any]:
    """聚合冻结快照并应用抑制规则，返回可持久化的确定性载荷。"""
    if min_group_size < 1:
        raise DomainError("最小样本量必须大于零")
    directory_map = {d.student_id: d for d in directory}

    cells: dict[tuple[str, str], _Group] = {}
    unmapped = 0
    total = 0
    for stat in students:
        total += 1
        entry = directory_map.get(stat.student_id)
        if entry is None:
            unmapped += 1
            continue
        key = (entry.organization, entry.category)
        cells.setdefault(key, _Group(key)).absorb(stat)

    cell_list = [cells[k] for k in sorted(cells)]
    org_names = sorted({k[0] for k in cells})
    org_totals: list[_Group] = []
    for org in org_names:
        total_group = _Group((org, ""))
        for cell in cell_list:
            if cell.key[0] == org:
                total_group.merge(cell)
        org_totals.append(total_group)
    grand_total = _Group(("", ""))
    for org_total in org_totals:
        grand_total.merge(org_total)

    _apply_suppression(cell_list, org_totals, grand_total, min_group_size)

    suppressed_cells = [c for c in cell_list if c.suppressed]
    suppressed_students = sum(c.student_count for c in suppressed_cells)
    reasons = {REASON_MIN_SAMPLE: 0, REASON_CONSISTENCY: 0}
    for group in [*cell_list, *org_totals, grand_total]:
        if group.suppressed and group.suppression_reason in reasons:
            reasons[group.suppression_reason] += 1

    risk_flags: list[str] = []
    mapped = grand_total.student_count
    if mapped == 0:
        risk_flags.append("no_mapped_students")
    elif suppressed_students * 2 > mapped:
        risk_flags.append("majority_suppressed")
    if grand_total.suppressed:
        risk_flags.append("grand_total_suppressed")

    return {
        "min_group_size": min_group_size,
        "cells": [_public_view(c, "cell") for c in cell_list],
        "organization_totals": [_public_view(t, "organization") for t in org_totals],
        "grand_total": _public_view(grand_total, "grand"),
        "privacy_impact": {
            "total_students": total,
            "mapped_students": mapped,
            "unmapped_students": unmapped,
            "organizations": len(org_names),
            "categories": len({k[1] for k in cells}),
            "total_cells": len(cell_list),
            "suppressed_cells": len(suppressed_cells),
            "suppressed_students": suppressed_students,
            "published_students": mapped - suppressed_students,
            "suppression_reasons": reasons,
            "risk_flags": risk_flags,
        },
    }


def content_hash(payload: Mapping[str, Any]) -> str:
    """对载荷做规范化哈希，用于校验确定性聚合。"""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


def filter_public_view(
    payload: Mapping[str, Any],
    *,
    organization: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """按组织/类别交叉筛选公开视图；筛选后不附带会泄露边界的合计。"""
    cells = [
        c
        for c in payload["cells"]
        if (organization is None or c["organization"] == organization)
        and (category is None or c["category"] == category)
    ]
    out: dict[str, Any] = {
        "filters": {"organization": organization, "category": category},
        "cells": cells,
    }
    if organization is None and category is None:
        out["organization_totals"] = payload["organization_totals"]
        out["grand_total"] = payload["grand_total"]
    elif organization is not None and category is None:
        out["organization_totals"] = [
            t for t in payload["organization_totals"] if t["organization"] == organization
        ]
    return out


def summarize_for_public(record: Mapping[str, Any]) -> dict[str, Any]:
    """公开查询响应的元信息（不含隐私影响与审计）。"""
    return {
        "summary_id": record["summary_id"],
        "plan_version": record["plan_version"],
        "freeze_id": record["freeze_id"],
        "rule_version": record["rule_version"],
        "published_at": record["published_at"],
    }


def build_audit_entry(
    *,
    sequence: int,
    action: str,
    actor_id: str,
    from_state: str,
    to_state: str,
    reason: str,
) -> dict[str, Any]:
    """生成一条内部审计记录（撤回后仍保留）。"""
    fingerprint = sha256(
        f"{sequence}|{action}|{actor_id}|{from_state}|{to_state}|{reason}".encode("utf-8")
    ).hexdigest()
    return {
        "sequence": sequence,
        "action": action,
        "actor_id": actor_id,
        "from_state": from_state,
        "to_state": to_state,
        "reason": reason,
        "fingerprint": fingerprint,
    }
