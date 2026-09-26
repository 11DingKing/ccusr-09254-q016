"""公开摘要 API：生命周期、抑制规则、权限与确定性测试。"""

from __future__ import annotations

import threading

from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

OFFICER = {"X-Actor-Id": "officer-1", "X-Actor-Role": "privacy_officer"}
OFFICER_2 = {"X-Actor-Id": "officer-2", "X-Actor-Role": "privacy_officer"}
ANALYST = {"X-Actor-Id": "analyst-1", "X-Actor-Role": "analyst"}
AUDITOR = {"X-Actor-Id": "auditor-1", "X-Actor-Role": "auditor"}

# 目录映射：S01-S05 CS/regular，S06-S07 CS/internship，S08-S11 MATH/regular，
# S12-S14 MATH/internship，S15-S16 PHY/internship；S99 故意不入目录。
DIRECTORY = [
    *[(f"S{i:02d}", "CS", "regular") for i in range(1, 6)],
    *[(f"S{i:02d}", "CS", "internship") for i in range(6, 8)],
    *[(f"S{i:02d}", "MATH", "regular") for i in range(8, 12)],
    *[(f"S{i:02d}", "MATH", "internship") for i in range(12, 15)],
    *[(f"S{i:02d}", "PHY", "internship") for i in range(15, 17)],
]


def _checkin(eid, student, minutes=120):
    end_total = 8 * 60 + minutes
    end = f"{end_total // 60:02d}:{end_total % 60:02d}"
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": "regular",
            "check_in_at": "2024-03-15T08:00:00+08:00",
            "check_out_at": f"2024-03-15T{end}:00+08:00",
        },
    }


def _seed(client):
    """建计划、事件、目录、规则并冻结，返回 (plan_version, freeze_id)。"""
    plan = dict(SHANGHAI_PLAN)
    plan["required_seconds"] = 3600
    assert client.post("/api/plans", json=plan).status_code == 201
    pv = plan["plan_version"]

    events = []
    for i in range(1, 18):
        sid = f"S{i:02d}"
        minutes = 30 if sid == "S10" else 120  # S10 不达标
        events.append(_checkin(f"E-{i:02d}", sid, minutes))
    resp = client.post(f"/api/plans/{pv}/events", json={"events": events})
    assert resp.status_code == 201, resp.text

    resp = client.put(
        f"/api/plans/{pv}/directory",
        json={
            "entries": [
                {"student_id": s, "organization": o, "category": c}
                for s, o, c in DIRECTORY
            ]
        },
        headers=ANALYST,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["upserted"] == len(DIRECTORY)

    resp = client.post(
        "/api/privacy-rules",
        json={"rule_version": "R-1", "min_group_size": 3, "note": "v1"},
        headers=OFFICER,
    )
    assert resp.status_code == 201, resp.text

    assert client.post(f"/api/plans/{pv}/freezes/F-1", json={}).status_code == 201
    return pv, "F-1"


def _create_draft(client, pv, freeze_id="F-1", summary_id="SUM-1", rule="R-1"):
    resp = client.post(
        f"/api/plans/{pv}/freezes/{freeze_id}/public-summaries",
        json={"summary_id": summary_id, "rule_version": rule},
        headers=ANALYST,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _publish(client, summary_id="SUM-1"):
    resp = client.post(
        f"/api/public-summaries/{summary_id}/approve",
        json={"reason": "impact reviewed"},
        headers=OFFICER,
    )
    assert resp.status_code == 200, resp.text
    resp = client.post(
        f"/api/public-summaries/{summary_id}/publish",
        json={"reason": "release approved"},
        headers=OFFICER_2,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _cell(body, org, category):
    for cell in body["cells"]:
        if cell["organization"] == org and cell["category"] == category:
            return cell
    raise AssertionError(f"cell {org}/{category} not found")


# --- 生命周期与隐私影响 -----------------------------------------------------


def test_full_lifecycle_with_privacy_impact(client):
    pv, freeze_id = _seed(client)

    preview = client.post(
        f"/api/plans/{pv}/freezes/{freeze_id}/public-summaries/preview",
        json={"rule_version": "R-1"},
        headers=ANALYST,
    )
    assert preview.status_code == 200, preview.text
    impact = preview.json()["privacy_impact"]
    assert impact["total_students"] == 17
    assert impact["unmapped_students"] == 1
    assert impact["mapped_students"] == 16
    assert impact["total_cells"] == 5
    assert impact["suppressed_cells"] == 3
    assert impact["suppressed_students"] == 9
    assert impact["published_students"] == 7
    assert impact["suppression_reasons"] == {"min_sample": 3, "consistency": 2}
    assert "majority_suppressed" in impact["risk_flags"]

    # 预览不落库。
    assert client.get("/api/public/summaries").json() == []

    draft = _create_draft(client, pv, freeze_id)
    assert draft["state"] == "draft"
    assert draft["rule_version"] == "R-1"
    assert draft["privacy_impact"] == impact
    assert [a["action"] for a in draft["audit"]] == ["create"]

    published = _publish(client)
    assert published["state"] == "published"
    assert published["published_at"] is not None
    actions = [a["action"] for a in published["audit"]]
    assert actions == ["create", "approve", "publish"]

    public = client.get("/api/public/summaries/SUM-1")
    assert public.status_code == 200, public.text
    body = public.json()
    assert body["summary_id"] == "SUM-1"
    assert body["grand_total"]["student_count"] == 16
    assert body["grand_total"]["compliant_count"] == 15
    assert body["grand_total"]["total_seconds_sum"] == 109800
    # 公开视图不携带隐私影响与审计。
    assert "privacy_impact" not in body
    assert "audit" not in body


def test_min_sample_and_consistency_suppression(client):
    pv, freeze_id = _seed(client)
    _create_draft(client, pv, freeze_id)
    _publish(client)
    body = client.get("/api/public/summaries/SUM-1").json()

    # 小样本单元格被抑制，且不携带任何数值。
    small = _cell(body, "CS", "internship")
    assert small["suppressed"] is True
    assert small["suppression_reason"] == "min_sample"
    assert "student_count" not in small
    assert "compliant_count" not in small

    # CS 仅剩一个被抑制格，regular 格被一致性补充抑制，防止由合计反推。
    comp = _cell(body, "CS", "regular")
    assert comp["suppressed"] is True
    assert comp["suppression_reason"] == "consistency"

    # 大样本单元格正常发布。
    math_reg = _cell(body, "MATH", "regular")
    assert math_reg["suppressed"] is False
    assert math_reg["student_count"] == 4
    assert math_reg["compliant_count"] == 3
    assert math_reg["compliance_rate"] == 0.75
    assert math_reg["total_seconds_sum"] == 23400

    # 组织合计层面同样抑制：CS 合计被一致性抑制，PHY 合计样本不足。
    totals = {t["organization"]: t for t in body["organization_totals"]}
    assert totals["CS"]["suppressed"] is True
    assert totals["CS"]["suppression_reason"] == "consistency"
    assert totals["PHY"]["suppressed"] is True
    assert totals["PHY"]["suppression_reason"] == "min_sample"
    assert totals["MATH"]["student_count"] == 7


def test_cross_filtering_public_query(client):
    pv, freeze_id = _seed(client)
    _create_draft(client, pv, freeze_id)
    _publish(client)

    # 按组织筛选：仅该组织单元格与该组织合计，无全校合计。
    resp = client.get("/api/public/summaries/SUM-1", params={"organization": "MATH"})
    assert resp.status_code == 200
    body = resp.json()
    assert {c["category"] for c in body["cells"]} == {"regular", "internship"}
    assert all(c["organization"] == "MATH" for c in body["cells"])
    assert [t["organization"] for t in body["organization_totals"]] == ["MATH"]
    assert body["grand_total"] is None

    # 按类别筛选：跨组织的同类单元格，不带任何合计。
    body = client.get("/api/public/summaries/SUM-1", params={"category": "internship"}).json()
    assert len(body["cells"]) == 3
    assert all(c["category"] == "internship" for c in body["cells"])
    assert body["organization_totals"] is None
    assert body["grand_total"] is None

    # 交叉筛选定位到被抑制格时，依然不泄露数值。
    body = client.get(
        "/api/public/summaries/SUM-1",
        params={"organization": "CS", "category": "internship"},
    ).json()
    assert len(body["cells"]) == 1
    cell = body["cells"][0]
    assert cell["suppressed"] is True
    assert "student_count" not in cell

    # 筛选不到任何单元格时返回空列表而非 404。
    body = client.get("/api/public/summaries/SUM-1", params={"organization": "NONE"}).json()
    assert body["cells"] == []
    assert body["organization_totals"] == []


# --- 确定性聚合与规则升级 ---------------------------------------------------


def test_deterministic_aggregation(client):
    pv, freeze_id = _seed(client)
    hashes = set()
    for _ in range(2):
        preview = client.post(
            f"/api/plans/{pv}/freezes/{freeze_id}/public-summaries/preview",
            json={"rule_version": "R-1"},
            headers=ANALYST,
        ).json()
        hashes.add(preview["content_hash"])
    assert len(hashes) == 1

    draft1 = _create_draft(client, pv, freeze_id, summary_id="SUM-1")
    draft2 = _create_draft(client, pv, freeze_id, summary_id="SUM-2")
    assert draft1["content_hash"] == hashes.pop()
    assert draft1["content_hash"] == draft2["content_hash"]
    # 单元格顺序确定（按组织、类别排序）。
    keys = [(c["organization"], c["category"]) for c in draft1["cells"]]
    assert keys == sorted(keys)


def test_rule_upgrade_does_not_rewrite_old_summaries(client):
    pv, freeze_id = _seed(client)
    before = _create_draft(client, pv, freeze_id, summary_id="SUM-1")
    _publish(client)

    # 规则版本不可变：同版本重复创建被拒绝。
    resp = client.post(
        "/api/privacy-rules",
        json={"rule_version": "R-1", "min_group_size": 9},
        headers=OFFICER,
    )
    assert resp.status_code == 409

    # 升级规则：新建版本，旧摘要保持原规则与原载荷。
    resp = client.post(
        "/api/privacy-rules",
        json={"rule_version": "R-2", "min_group_size": 6, "note": "stricter"},
        headers=OFFICER,
    )
    assert resp.status_code == 201, resp.text

    after = client.get("/api/public-summaries/SUM-1", headers=AUDITOR).json()
    assert after["rule_version"] == "R-1"
    assert after["content_hash"] == before["content_hash"]
    assert after["cells"] == before["cells"]

    # 新规则生成的新摘要应用更严格的抑制。
    stricter = _create_draft(client, pv, freeze_id, summary_id="SUM-2", rule="R-2")
    assert stricter["rule_version"] == "R-2"
    assert stricter["content_hash"] != before["content_hash"]
    assert stricter["privacy_impact"]["suppressed_cells"] > before["privacy_impact"]["suppressed_cells"]

    # 旧摘要的公开视图仍按旧规则发布。
    public = client.get("/api/public/summaries/SUM-1").json()
    assert public["rule_version"] == "R-1"
    assert _cell(public, "MATH", "regular")["student_count"] == 4


# --- 撤回与内部审计 ---------------------------------------------------------


def test_withdraw_keeps_internal_audit(client):
    pv, freeze_id = _seed(client)
    _create_draft(client, pv, freeze_id)
    _publish(client)

    resp = client.post(
        "/api/public-summaries/SUM-1/withdraw",
        json={"reason": "data quality issue found"},
        headers=OFFICER,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "withdrawn"
    assert resp.json()["withdrawn_at"] is not None

    # 公开边界立即失效。
    assert client.get("/api/public/summaries/SUM-1").status_code == 404
    assert client.get("/api/public/summaries").json() == []

    # 内部审计完整保留。
    internal = client.get("/api/public-summaries/SUM-1", headers=AUDITOR)
    assert internal.status_code == 200
    body = internal.json()
    assert body["state"] == "withdrawn"
    actions = [(a["action"], a["actor_id"]) for a in body["audit"]]
    assert actions == [
        ("create", "analyst-1"),
        ("approve", "officer-1"),
        ("publish", "officer-2"),
        ("withdraw", "officer-1"),
    ]
    assert all(a["reason"] for a in body["audit"])
    assert all(a["fingerprint"] for a in body["audit"])
    assert body["privacy_impact"]["total_students"] == 17

    # 撤回是终态。
    resp = client.post(
        "/api/public-summaries/SUM-1/approve",
        json={"reason": "try again"},
        headers=OFFICER,
    )
    assert resp.status_code == 409


def test_state_machine_rejects_illegal_transitions(client):
    pv, freeze_id = _seed(client)
    _create_draft(client, pv, freeze_id)

    # 未审批不能直接发布。
    resp = client.post(
        "/api/public-summaries/SUM-1/publish",
        json={"reason": "skip approval"},
        headers=OFFICER,
    )
    assert resp.status_code == 409

    # 重复审批被拒绝。
    client.post(
        "/api/public-summaries/SUM-1/approve",
        json={"reason": "ok"},
        headers=OFFICER,
    )
    resp = client.post(
        "/api/public-summaries/SUM-1/approve",
        json={"reason": "again"},
        headers=OFFICER,
    )
    assert resp.status_code == 409

    # 重复 summary_id 被拒绝。
    resp = client.post(
        f"/api/plans/{pv}/freezes/{freeze_id}/public-summaries",
        json={"summary_id": "SUM-1", "rule_version": "R-1"},
        headers=ANALYST,
    )
    assert resp.status_code == 409


# --- 权限隔离 ---------------------------------------------------------------


def test_permission_isolation(client):
    pv, freeze_id = _seed(client)

    # 缺少身份头 → 401。
    assert client.get("/api/public-summaries/SUM-1").status_code == 401
    assert client.post(
        f"/api/plans/{pv}/freezes/{freeze_id}/public-summaries/preview",
        json={"rule_version": "R-1"},
    ).status_code == 401
    assert client.post(
        "/api/privacy-rules",
        json={"rule_version": "R-9", "min_group_size": 2},
    ).status_code == 401

    # 角色不足 → 403：auditor 只读，analyst 不能审批/发布/建规则。
    assert client.post(
        f"/api/plans/{pv}/freezes/{freeze_id}/public-summaries/preview",
        json={"rule_version": "R-1"},
        headers=AUDITOR,
    ).status_code == 403
    assert client.post(
        "/api/privacy-rules",
        json={"rule_version": "R-9", "min_group_size": 2},
        headers=ANALYST,
    ).status_code == 403

    _create_draft(client, pv, freeze_id)
    assert client.post(
        "/api/public-summaries/SUM-1/approve",
        json={"reason": "no"},
        headers=ANALYST,
    ).status_code == 403
    assert client.post(
        "/api/public-summaries/SUM-1/withdraw",
        json={"reason": "no"},
        headers=AUDITOR,
    ).status_code == 403

    # auditor 可读内部视图；公开端点无需身份。
    assert client.get("/api/public-summaries/SUM-1", headers=AUDITOR).status_code == 200
    _publish(client)
    assert client.get("/api/public/summaries/SUM-1").status_code == 200


# --- 并发发布 ---------------------------------------------------------------


def test_concurrent_publish_only_one_wins(client):
    pv, freeze_id = _seed(client)
    _create_draft(client, pv, freeze_id)
    client.post(
        "/api/public-summaries/SUM-1/approve",
        json={"reason": "ok"},
        headers=OFFICER,
    )

    from app import services

    outcomes: list[str] = []
    lock = threading.Lock()

    def _publish(i):
        session = TestSessionLocal()
        try:
            services.transition_summary(
                session,
                summary_id="SUM-1",
                action="publish",
                actor_id=f"officer-{i}",
                reason="race",
            )
            with lock:
                outcomes.append("ok")
        except (services.SummaryConflictError, services.SummaryStateError):
            with lock:
                outcomes.append("conflict")
        finally:
            session.close()

    threads = [threading.Thread(target=_publish, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 3

    internal = client.get("/api/public-summaries/SUM-1", headers=AUDITOR).json()
    assert internal["state"] == "published"
    publishes = [a for a in internal["audit"] if a["action"] == "publish"]
    assert len(publishes) == 1


# --- 错误路径 ---------------------------------------------------------------


def test_not_found_paths(client):
    pv, freeze_id = _seed(client)
    assert client.post(
        f"/api/plans/{pv}/freezes/NOPE/public-summaries/preview",
        json={"rule_version": "R-1"},
        headers=ANALYST,
    ).status_code == 404
    assert client.post(
        f"/api/plans/{pv}/freezes/{freeze_id}/public-summaries/preview",
        json={"rule_version": "NOPE"},
        headers=ANALYST,
    ).status_code == 404
    assert client.get("/api/public-summaries/NOPE", headers=AUDITOR).status_code == 404
    assert client.get("/api/public/summaries/NOPE").status_code == 404
    assert client.get("/api/privacy-rules/NOPE", headers=AUDITOR).status_code == 404


def test_unmapped_students_excluded_from_cells(client):
    pv, freeze_id = _seed(client)
    preview = client.post(
        f"/api/plans/{pv}/freezes/{freeze_id}/public-summaries/preview",
        json={"rule_version": "R-1"},
        headers=ANALYST,
    ).json()
    # S99 不在目录中：不计入任何单元格，仅体现在隐私影响里。
    assert preview["grand_total"]["student_count"] == 16
    assert preview["privacy_impact"]["unmapped_students"] == 1
