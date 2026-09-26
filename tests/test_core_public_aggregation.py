"""公开聚合引擎的单元测试：确定性、最小样本、互补抑制、一致性。"""

from __future__ import annotations

import random

from app.core.public_aggregation import (
    CATEGORY_BELOW,
    CATEGORY_MEETS,
    PrivacyRules,
    Profile,
    apply_public_filters,
    build_public_summary,
    fingerprint_document,
)


def _student(sid: str, *, meets: bool, seconds: int) -> dict:
    return {
        "student_id": sid,
        "meets_requirement": meets,
        "total_seconds": seconds,
    }


def _snapshot(students: list[dict]) -> dict:
    return {
        "plan_version": "P-1",
        "freeze_id": "F-1",
        "generated_at": "2024-06-01T00:00:00Z",
        "students": students,
    }


def _dataset() -> tuple[dict, dict[str, Profile]]:
    students: list[dict] = []
    profiles: dict[str, Profile] = {}
    # Big: 7 meets + 5 below —— 两个单元格都达到 k=5，整体可发布。
    for i in range(7):
        sid = f"BIG-M-{i}"
        students.append(_student(sid, meets=True, seconds=14400))
        profiles[sid] = Profile("Big", {"track": "alpha" if i % 2 else "beta"})
    for i in range(5):
        sid = f"BIG-B-{i}"
        students.append(_student(sid, meets=False, seconds=3600))
        profiles[sid] = Profile("Big", {"track": "beta"})
    # Tiny: 4 below + 6 meets —— below 触发最小样本，meets 触发互补抑制。
    for i in range(6):
        sid = f"TINY-M-{i}"
        students.append(_student(sid, meets=True, seconds=12000))
        profiles[sid] = Profile("Tiny", {"track": "alpha"})
    for i in range(4):
        sid = f"TINY-B-{i}"
        students.append(_student(sid, meets=False, seconds=1800))
        profiles[sid] = Profile("Tiny", {"track": "alpha"})
    # 无组织画像的学生必须被排除并在隐私影响中列出。
    students.append(_student("GHOST-1", meets=True, seconds=20000))
    return _snapshot(students), profiles


def test_min_sample_and_complement_suppress_whole_organization():
    snapshot, profiles = _dataset()
    result = build_public_summary(
        snapshot, profiles, rules=PrivacyRules.from_values(min_cell_size=5)
    )
    doc, impact = result["document"], result["privacy_impact"]

    org_names = [o["organization"] for o in doc["organizations"]]
    assert org_names == ["Big"]

    big = doc["organizations"][0]
    cells = {c["category"]: c for c in big["cells"]}
    assert cells[CATEGORY_MEETS]["student_count"] == 7
    assert cells[CATEGORY_BELOW]["student_count"] == 5
    assert big["total_students"] == 12
    assert big["meets_rate"] == round(7 / 12, 4)

    # 隐私影响包含被抑制组织的真实计数，但公开文档中绝不能出现。
    suppressed = impact["organizations_suppressed"]
    assert len(suppressed) == 1
    tiny = suppressed[0]
    assert tiny["organization"] == "Tiny"
    assert tiny["total_students"] == 10
    reasons = {c["category"]: c["reason"] for c in tiny["suppressed_cells"]}
    assert reasons[CATEGORY_BELOW] == "min_sample"
    assert reasons[CATEGORY_MEETS] == "complement"
    assert impact["cells_primary_suppressed"] == 1
    assert impact["cells_complement_suppressed"] == 1
    assert impact["suppressed_students"] == 10
    assert impact["students_without_organization"] == ["GHOST-1"]
    assert "Tiny" not in str(doc)


def test_consistency_cells_sum_to_denominator_and_totals():
    snapshot, profiles = _dataset()
    result = build_public_summary(
        snapshot, profiles, rules=PrivacyRules.from_values(min_cell_size=5)
    )
    doc = result["document"]
    for org in doc["organizations"]:
        assert sum(c["student_count"] for c in org["cells"]) == org["total_students"]
        meets = next(
            c["student_count"] for c in org["cells"] if c["category"] == CATEGORY_MEETS
        )
        assert org["meets_rate"] == round(meets / org["total_students"], 4)
    # totals 只汇总完整发布的组织，且仍保持单元格求和一致。
    assert doc["totals"]["student_count"] == 12
    assert (
        sum(c["student_count"] for c in doc["totals"]["cells"])
        == doc["totals"]["student_count"]
    )
    assert doc["totals"]["meets_rate"] == round(7 / 12, 4)


def test_aggregation_is_deterministic_regardless_of_input_order():
    snapshot, profiles = _dataset()
    rules = PrivacyRules.from_values(min_cell_size=5)

    shuffled = dict(snapshot)
    students = list(snapshot["students"])
    random.Random(42).shuffle(students)
    shuffled["students"] = students
    profile_items = list(profiles.items())
    random.Random(7).shuffle(profile_items)
    shuffled_profiles = dict(profile_items)

    first = build_public_summary(snapshot, profiles, rules=rules)["document"]
    second = build_public_summary(shuffled, shuffled_profiles, rules=rules)["document"]
    assert first == second
    assert first["document_fingerprint"] == fingerprint_document(first)


def test_rule_change_produces_distinct_fingerprint():
    snapshot, profiles = _dataset()
    k5 = build_public_summary(
        snapshot, profiles, rules=PrivacyRules.from_values(min_cell_size=5)
    )["document"]
    k6 = build_public_summary(
        snapshot, profiles, rules=PrivacyRules.from_values(min_cell_size=6)
    )["document"]
    assert k5["document_fingerprint"] != k6["document_fingerprint"]
    # k=6 时 Big 的 below(5) 也被抑制，Big 整体消失。
    assert k6["organizations"] == []


def test_rounding_keeps_cell_sum_equal_to_denominator():
    snapshot, profiles = _dataset()
    result = build_public_summary(
        snapshot,
        profiles,
        rules=PrivacyRules.from_values(min_cell_size=5, round_increment=2),
    )
    doc = result["document"]
    big = doc["organizations"][0]
    counts = {c["category"]: c["student_count"] for c in big["cells"]}
    # 7 -> 8, 5 -> 6（半数向上舍入），分母由单元格之和导出。
    assert counts[CATEGORY_MEETS] == 8
    assert counts[CATEGORY_BELOW] == 6
    assert big["total_students"] == 14
    assert sum(counts.values()) == big["total_students"]
    assert big["meets_rate"] == round(8 / 14, 4)


def test_suppression_margin_raises_complement_floor():
    # 5 meets / 1 below：k=5 时 below 被最小样本抑制；meets 的互补仅 1 人。
    students = [_student(f"M-{i}", meets=True, seconds=9000) for i in range(5)]
    students.append(_student("B-0", meets=False, seconds=1000))
    profiles = {s["student_id"]: Profile("Only", {}) for s in students}
    strict = build_public_summary(
        _snapshot(students),
        profiles,
        rules=PrivacyRules.from_values(min_cell_size=5, suppression_margin=2),
    )
    # margin=2 -> 互补门槛 k-margin=3，互补 1 < 3，meets 因互补被抑制。
    assert strict["document"]["organizations"] == []
    strict_reasons = {
        c["category"]: c["reason"]
        for c in strict["privacy_impact"]["organizations_suppressed"][0][
            "suppressed_cells"
        ]
    }
    assert strict_reasons[CATEGORY_MEETS] == "complement"

    loose = build_public_summary(
        _snapshot(students),
        profiles,
        rules=PrivacyRules.from_values(min_cell_size=5, suppression_margin=4),
    )
    # margin=4 -> 门槛 1，meets 自身不再被互补抑制；但 below 仍触发最小样本，
    # 一致性规则仍抑制整个组织。
    assert loose["document"]["organizations"] == []
    loose_reasons = {
        c["category"]: c["reason"]
        for c in loose["privacy_impact"]["organizations_suppressed"][0][
            "suppressed_cells"
        ]
    }
    assert loose_reasons[CATEGORY_BELOW] == "min_sample"
    assert CATEGORY_MEETS not in loose_reasons


def test_custom_label_dimension():
    students = [
        _student("S1", meets=True, seconds=9000),
        _student("S2", meets=False, seconds=1000),
        _student("S3", meets=True, seconds=9000),
        _student("S4", meets=True, seconds=9000),
        _student("S5", meets=True, seconds=9000),
        _student("S6", meets=False, seconds=1000),
        _student("S7", meets=True, seconds=9000),
        # S8 没有 track 标签 -> 排除并在隐私影响中列出
        _student("S8", meets=True, seconds=9000),
    ]
    profiles = {
        "S1": Profile("O", {"track": "alpha"}),
        "S2": Profile("O", {"track": "alpha"}),
        "S3": Profile("O", {"track": "alpha"}),
        "S4": Profile("O", {"track": "alpha"}),
        "S5": Profile("O", {"track": "alpha"}),
        "S6": Profile("O", {"track": "beta"}),
        "S7": Profile("O", {"track": "beta"}),
        "S8": Profile("O", {}),
    }
    result = build_public_summary(
        _snapshot(students),
        profiles,
        rules=PrivacyRules.from_values(min_cell_size=2),
        category_dimension="track",
    )
    doc, impact = result["document"], result["privacy_impact"]
    org = doc["organizations"][0]
    assert {c["category"] for c in org["cells"]} == {"alpha", "beta"}
    assert sum(c["student_count"] for c in org["cells"]) == org["total_students"]
    assert "meets_rate" not in org
    assert "meets_rate" not in doc["totals"]
    assert impact["students_without_category"] == ["S8"]


def test_cross_filters_strip_denominator_for_category_slice():
    snapshot, profiles = _dataset()
    doc = build_public_summary(
        snapshot, profiles, rules=PrivacyRules.from_values(min_cell_size=5)
    )["document"]

    by_org = apply_public_filters(doc, organization="Big")
    assert len(by_org["organizations"]) == 1
    assert by_org["organizations"][0]["total_students"] == 12
    assert by_org["totals"] is None

    sliced = apply_public_filters(doc, organization="Big", category=CATEGORY_MEETS)
    org = sliced["organizations"][0]
    assert len(org["cells"]) == 1
    assert org["cells"][0]["category"] == CATEGORY_MEETS
    # 类别切片剥离分母与达标率，减法无法反推 below 单元格。
    assert org["total_students"] is None
    assert org["meets_rate"] is None
    assert sliced["totals"] is None

    # 筛选被抑制的组织什么都不返回，无法借此探测其存在。
    ghost = apply_public_filters(doc, organization="Tiny")
    assert ghost["organizations"] == []


def test_invalid_rules_rejected():
    import pytest

    with pytest.raises(ValueError):
        PrivacyRules.from_values(min_cell_size=0)
    with pytest.raises(ValueError):
        PrivacyRules.from_values(min_cell_size=5, suppression_margin=5)
