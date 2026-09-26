"""冻结快照的公开聚合引擎。

该模块是纯函数、确定性的：相同的冻结快照、组织画像与隐私规则必然产生
字节一致的公开文档与指纹。引擎实现三类隐私保护：

1. 最小样本（k-匿名）：任一 (组织, 类别) 单元格人数 ``count < k`` 时整体抑制；
2. 互补抑制：单元格的互补人群（同组织内其他类别）人数低于
   ``k - suppression_margin`` 时同样抑制，防止用总数减去大格反推小格；
3. 一致性：只有组织内全部单元格均发布时，才发布组织总人数与达标率，
   保证「单元格之和 = 总数」「达标率 = 达标人数 / 总人数」恒成立。

规则集版本（``ruleset_version``）固化在每份摘要中；规则升级后旧摘要不会
被重新计算，调用方必须以新的 ``summary_id`` 重新预览与发布。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping, Sequence

RULESET_VERSION = "v1"
COMPLIANCE_DIMENSION = "compliance"
CATEGORY_MEETS = "meets"
CATEGORY_BELOW = "below"
COVERAGE_NOTE = "complete_organizations_only"


@dataclass(frozen=True)
class PrivacyRules:
    """公开聚合的隐私规则（随摘要固化、不可变）。"""

    min_cell_size: int = 5
    suppression_margin: int = 0
    round_increment: int = 1
    ruleset_version: str = RULESET_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleset_version": self.ruleset_version,
            "min_cell_size": self.min_cell_size,
            "suppression_margin": self.suppression_margin,
            "round_increment": self.round_increment,
            "require_consistency": True,
        }

    @classmethod
    def from_values(
        cls,
        *,
        min_cell_size: int = 5,
        suppression_margin: int = 0,
        round_increment: int = 1,
    ) -> "PrivacyRules":
        if min_cell_size < 1:
            raise ValueError("min_cell_size must be >= 1")
        if suppression_margin < 0:
            raise ValueError("suppression_margin must be >= 0")
        if suppression_margin >= min_cell_size:
            raise ValueError(
                "suppression_margin must be smaller than min_cell_size"
            )
        if round_increment < 1:
            raise ValueError("round_increment must be >= 1")
        if round_increment > min_cell_size:
            raise ValueError(
                "round_increment must not exceed min_cell_size"
            )
        return cls(
            min_cell_size=int(min_cell_size),
            suppression_margin=int(suppression_margin),
            round_increment=int(round_increment),
            ruleset_version=RULESET_VERSION,
        )


class AggregationError(ValueError):
    """聚合输入不满足要求。"""


@dataclass(frozen=True)
class Profile:
    organization: str
    labels: Mapping[str, str]


def _category_value(
    student: Mapping[str, Any], dimension: str, labels: Mapping[str, str]
) -> str | None:
    if dimension == COMPLIANCE_DIMENSION:
        return CATEGORY_MEETS if student.get("meets_requirement") else CATEGORY_BELOW
    value = labels.get(dimension)
    if value is None or value == "":
        return None
    return value


def _round_count(raw: int, increment: int) -> int:
    """确定性四舍五入到 increment 的倍数（0.5 向上）。"""

    if increment <= 1:
        return raw
    quotient = (2 * raw + increment) // (2 * increment)
    return quotient * increment


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _seconds_block(seconds: Sequence[int]) -> dict[str, int]:
    # 不输出精确人数：精确人数可能绕过舍入规则或低于最小样本阈值；
    # 人数统一由单元格的 student_count 字段表达。
    total = sum(seconds)
    n = len(seconds)
    return {
        "sum": total,
        "min": min(seconds),
        "max": max(seconds),
        "mean": (2 * total + n) // (2 * n) if n else 0,
    }


def build_public_summary(
    snapshot: Mapping[str, Any],
    profiles: Mapping[str, Profile],
    *,
    rules: PrivacyRules | None = None,
    category_dimension: str = COMPLIANCE_DIMENSION,
) -> dict[str, Any]:
    """对冻结快照执行确定性的公开聚合。

    返回 ``{"document": 公开文档, "privacy_impact": 内部隐私影响评估}``。
    ``privacy_impact`` 含被抑制单元格的真实人数，仅供内部预览/审计，
    绝不能进入公开文档或公开接口。
    """

    rules = rules or PrivacyRules()
    dimension = category_dimension.strip()
    if not dimension:
        raise AggregationError("category_dimension must not be empty")

    students = sorted(snapshot.get("students", []), key=lambda s: s["student_id"])

    without_org: list[str] = []
    without_category: list[str] = []
    # org -> category -> [student]
    grouped: dict[str, dict[str, list[Mapping[str, Any]]]] = {}

    for student in students:
        sid = student["student_id"]
        profile = profiles.get(sid)
        if profile is None or not profile.organization:
            without_org.append(sid)
            continue
        value = _category_value(student, dimension, profile.labels)
        if value is None:
            without_category.append(sid)
            continue
        grouped.setdefault(profile.organization, {}).setdefault(value, []).append(
            student
        )

    k = rules.min_cell_size
    complement_floor = max(1, k - rules.suppression_margin)

    published_orgs: list[dict[str, Any]] = []
    suppressed_orgs: list[dict[str, Any]] = []
    cells_evaluated = 0
    primary_suppressed = 0
    complement_suppressed = 0
    suppressed_students = 0
    smallest_published: int | None = None

    for org in sorted(grouped):
        buckets = grouped[org]
        org_total_raw = sum(len(members) for members in buckets.values())
        suppressed_cells: list[dict[str, Any]] = []
        visible: dict[str, list[Mapping[str, Any]]] = {}

        for value in sorted(buckets):
            members = buckets[value]
            count = len(members)
            complement = org_total_raw - count
            cells_evaluated += 1
            if count < k:
                suppressed_cells.append(
                    {
                        "category": value,
                        "student_count": count,
                        "complement_count": complement,
                        "reason": "min_sample",
                    }
                )
                primary_suppressed += 1
                continue
            if complement < complement_floor:
                suppressed_cells.append(
                    {
                        "category": value,
                        "student_count": count,
                        "complement_count": complement,
                        "reason": "complement",
                    }
                )
                complement_suppressed += 1
                continue
            visible[value] = members

        if suppressed_cells:
            # 一致性规则：只要任一类别被抑制，整个组织（含分母）都不发布。
            suppressed_students += org_total_raw
            suppressed_orgs.append(
                {
                    "organization": org,
                    "total_students": org_total_raw,
                    "suppressed_cells": suppressed_cells,
                }
            )
            continue

        cells: list[dict[str, Any]] = []
        for value in sorted(visible):
            members = visible[value]
            raw_count = len(members)
            published_count = _round_count(raw_count, rules.round_increment)
            smallest_published = (
                published_count
                if smallest_published is None
                else min(smallest_published, published_count)
            )
            cell: dict[str, Any] = {
                "category": value,
                "student_count": published_count,
                "seconds": _seconds_block(
                    [int(m.get("total_seconds", 0)) for m in members]
                ),
            }
            cells.append(cell)

        # 组织分母由已发布单元格之和导出（而非独立舍入），保证
        # 「单元格之和 = 总数」在任何舍入增量下都成立。
        published_total = sum(c["student_count"] for c in cells)
        org_doc: dict[str, Any] = {
            "organization": org,
            "total_students": published_total,
            "cells": cells,
        }
        if dimension == COMPLIANCE_DIMENSION:
            meets_published = next(
                (
                    c["student_count"]
                    for c in cells
                    if c["category"] == CATEGORY_MEETS
                ),
                0,
            )
            org_doc["meets_rate"] = _rate(meets_published, published_total)
        published_orgs.append(org_doc)

    # 总体合计只汇总完整发布的组织，因此减法无法还原被抑制的小格。
    totals = _build_totals(published_orgs, dimension)

    document: dict[str, Any] = {
        "plan_version": snapshot.get("plan_version"),
        "freeze_id": snapshot.get("freeze_id"),
        "snapshot_generated_at": snapshot.get("generated_at"),
        "ruleset_version": rules.ruleset_version,
        "rules": rules.to_dict(),
        "category_dimension": dimension,
        "coverage": COVERAGE_NOTE,
        "suppression_applied": True,
        "organizations": published_orgs,
        "totals": totals,
        "filters": None,
    }
    document["document_fingerprint"] = fingerprint_document(document)

    notes: list[str] = []
    if without_org:
        notes.append(
            "students without an organization profile are excluded from the "
            "public document"
        )
    if without_category:
        notes.append(
            "students without a value for the selected category dimension "
            "are excluded from the public document"
        )
    if not published_orgs and grouped:
        notes.append(
            "every organization was suppressed; the public document contains "
            "no organization-level data"
        )

    impact = {
        "ruleset_version": rules.ruleset_version,
        "rules": rules.to_dict(),
        "category_dimension": dimension,
        "students_in_snapshot": len(students),
        "analyzed_students": sum(
            len(members)
            for buckets in grouped.values()
            for members in buckets.values()
        ),
        "students_without_organization": sorted(without_org),
        "students_without_category": sorted(without_category),
        "organizations_total": len(grouped),
        "organizations_published": len(published_orgs),
        "organizations_suppressed": suppressed_orgs,
        "cells_evaluated": cells_evaluated,
        "cells_primary_suppressed": primary_suppressed,
        "cells_complement_suppressed": complement_suppressed,
        "suppressed_students": suppressed_students,
        "smallest_published_cell": smallest_published,
        "document_fingerprint": document["document_fingerprint"],
        "notes": notes,
    }
    return {"document": document, "privacy_impact": impact}


def _build_totals(
    published_orgs: Sequence[dict[str, Any]], dimension: str
) -> dict[str, Any]:
    by_category: dict[str, dict[str, int]] = {}
    total_students = 0
    for org in published_orgs:
        total_students += org["total_students"]
        for cell in org["cells"]:
            agg = by_category.setdefault(
                cell["category"], {"student_count": 0, "seconds_sum": 0}
            )
            agg["student_count"] += cell["student_count"]
            agg["seconds_sum"] += cell["seconds"]["sum"]
    cells = [
        {
            "category": value,
            "student_count": by_category[value]["student_count"],
            "seconds_sum": by_category[value]["seconds_sum"],
        }
        for value in sorted(by_category)
    ]
    totals: dict[str, Any] = {
        "student_count": total_students,
        "cells": cells,
    }
    if dimension == COMPLIANCE_DIMENSION:
        meets = next(
            (c["student_count"] for c in cells if c["category"] == CATEGORY_MEETS),
            0,
        )
        totals["meets_rate"] = _rate(meets, total_students)
    return totals


def fingerprint_document(document: Mapping[str, Any]) -> str:
    """对公开文档（排除指纹字段本身）计算确定性的 SHA-256 指纹。"""

    canonical = json.dumps(
        {k: v for k, v in document.items() if k != "document_fingerprint"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + sha256(canonical.encode("utf-8")).hexdigest()


def apply_public_filters(
    document: Mapping[str, Any],
    *,
    organization: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """对已发布文档做公开交叉筛选。

    被抑制的组织/单元格本就不在文档中，因此筛选无法绕过抑制；只要启用
    类别筛选就同时剥离组织分母、达标率与总体合计，防止用减法反推互补格。
    """

    filtered: dict[str, Any] = {
        k: v
        for k, v in dict(document).items()
        if k not in ("organizations", "totals", "filters")
    }
    orgs: list[dict[str, Any]] = []
    for org in document["organizations"]:
        if organization is not None and org["organization"] != organization:
            continue
        if category is None:
            orgs.append(dict(org))
            continue
        cells = [c for c in org["cells"] if c["category"] == category]
        if not cells:
            continue
        # 不暴露 total_students / meets_rate：配合单格可反推互补类别。
        orgs.append(
            {
                "organization": org["organization"],
                "cells": [dict(c) for c in cells],
                "total_students": None,
                "meets_rate": None,
            }
        )
    filtered["organizations"] = orgs
    filtered["totals"] = None if (organization or category) else document["totals"]
    filtered["filters"] = {
        "organization": organization,
        "category": category,
    }
    return filtered
