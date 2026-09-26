"""公开摘要 API：完整工作流、隐私影响、权限隔离、并发发布与确定性。"""

from __future__ import annotations

import threading

import pytest

from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

OFFICER = {"X-Actor-Id": "officer-1", "X-Actor-Roles": "privacy_officer"}
OFFICER_2 = {"X-Actor-Id": "officer-2", "X-Actor-Roles": "privacy_officer"}
APPROVER = {"X-Actor-Id": "approver-1", "X-Actor-Roles": "approver"}
PUBLISHER = {"X-Actor-Id": "publisher-1", "X-Actor-Roles": "publisher"}
AUDITOR = {"X-Actor-Id": "auditor-1", "X-Actor-Roles": "auditor"}


def _checkin(eid, student, start, end, activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


@pytest.fixture
def frozen_plan(client):
    """12 名达标 + 5 名未达标学生，全部属于 ORG-A；冻结 F-1。"""

    client.post("/api/plans", json=SHANGHAI_PLAN)
    pv = SHANGHAI_PLAN["plan_version"]
    events = []
    for i in range(12):
        events.append(
            _checkin(
                f"E-M-{i:02d}",
                f"S-M-{i:02d}",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T11:00:00+08:00",
            )
        )
    for i in range(5):
        events.append(
            _checkin(
                f"E-B-{i:02d}",
                f"S-B-{i:02d}",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T08:30:00+08:00",
            )
        )
    client.post(f"/api/plans/{pv}/events", json={"events": events})
    resp = client.post(f"/api/plans/{pv}/freezes/F-1", json={})
    assert resp.status_code == 201, resp.text

    profiles = [
        {"student_id": f"S-M-{i:02d}", "organization": "ORG-A"}
        for i in range(12)
    ] + [
        {"student_id": f"S-B-{i:02d}", "organization": "ORG-A"}
        for i in range(5)
    ]
    resp = client.put(
        f"/api/plans/{pv}/student-profiles",
        json={"profiles": profiles},
        headers=OFFICER,
    )
    assert resp.status_code == 200, resp.text
    return pv


def _preview(client, pv, summary_id="PUB-1", headers=OFFICER, **overrides):
    body = {"freeze_id": "F-1", "summary_id": summary_id}
    body.update(overrides)
    return client.post(
        f"/api/plans/{pv}/public-summaries/{summary_id}/preview",
        json=body,
        headers=headers,
    )


def test_full_lifecycle_preview_to_withdraw_keeps_audit(client, frozen_plan):
    pv = frozen_plan

    # 1. 预览：隐私官看到公开文档与隐私影响。
    resp = _preview(client, pv, min_cell_size=5)
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["state"] == "preview"
    assert preview["ruleset_version"] == "v1"
    doc = preview["document"]
    org = doc["organizations"][0]
    assert org["organization"] == "ORG-A"
    assert org["total_students"] == 17
    cells = {c["category"]: c for c in org["cells"]}
    assert cells["meets"]["student_count"] == 12
    assert cells["below"]["student_count"] == 5
    # 隐私影响在内部视图中完整呈现，发布前必须可见。
    impact = preview["privacy_impact"]
    assert impact["students_in_snapshot"] == 17
    assert impact["organizations_published"] == 1
    assert impact["smallest_published_cell"] == 5
    fingerprint = preview["document_fingerprint"]
    assert len(fingerprint) == 71

    # 2. 提交：必须确认隐私影响且指纹匹配。
    resp = client.post(
        f"/api/plans/{pv}/public-summaries/PUB-1/submit",
        json={"acknowledged": False, "document_fingerprint": fingerprint},
        headers=OFFICER,
    )
    assert resp.status_code == 400
    resp = client.post(
        f"/api/plans/{pv}/public-summaries/PUB-1/submit",
        json={"acknowledged": True, "document_fingerprint": "sha256:deadbeef"},
        headers=OFFICER,
    )
    assert resp.status_code == 400
    resp = client.post(
        f"/api/plans/{pv}/public-summaries/PUB-1/submit",
        json={"acknowledged": True, "document_fingerprint": fingerprint},
        headers=OFFICER,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "pending_approval"

    # 3. 审批：提交人不得自审。
    resp = client.post(
        f"/api/plans/{pv}/public-summaries/PUB-1/approve",
        json={"note": "self approval"},
        headers={
            "X-Actor-Id": "officer-1",
            "X-Actor-Roles": "approver,privacy_officer",
        },
    )
    assert resp.status_code == 400
    resp = client.post(
        f"/api/plans/{pv}/public-summaries/PUB-1/approve",
        json={"note": "PIA reviewed"},
        headers=APPROVER,
    )
    assert resp.status_code == 200, resp.text
    approved = resp.json()
    assert approved["state"] == "approved"
    assert approved["approved_by"] == "approver-1"

    # 4. 发布。
    resp = client.post(
        f"/api/plans/{pv}/public-summaries/PUB-1/publish",
        headers=PUBLISHER,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "published"

    # 5. 公开查询无需任何身份头。
    public = client.get(f"/api/public/plans/{pv}/summaries/PUB-1")
    assert public.status_code == 200, public.text
    public_doc = public.json()
    assert "privacy_impact" not in public_doc
    assert "audit_log" not in public_doc
    assert "state" not in public_doc
    assert public_doc["document_fingerprint"] == fingerprint
    assert public_doc["published_at"] is not None
    # 公开文档不含任何学生标识。
    serialized = str(public_doc)
    for i in range(12):
        assert f"S-M-{i:02d}" not in serialized
    for i in range(5):
        assert f"S-B-{i:02d}" not in serialized

    listing = client.get(f"/api/public/plans/{pv}/summaries").json()
    assert [d["summary_id"] for d in listing] == ["PUB-1"]

    # 6. 撤回。
    resp = client.post(
        f"/api/plans/{pv}/public-summaries/PUB-1/withdraw",
        json={"reason": "rule update required"},
        headers=PUBLISHER,
    )
    assert resp.status_code == 200, resp.text
    withdrawn = resp.json()
    assert withdrawn["state"] == "withdrawn"
    assert withdrawn["withdrawn_by"] == "publisher-1"

    # 撤回后公开侧不可见，但内部审计链完整保留。
    assert client.get(f"/api/public/plans/{pv}/summaries/PUB-1").status_code == 404
    assert client.get(f"/api/public/plans/{pv}/summaries").json() == []
    internal = client.get(
        f"/api/plans/{pv}/public-summaries/PUB-1", headers=AUDITOR
    )
    assert internal.status_code == 200
    body = internal.json()
    actions = [entry["action"] for entry in body["audit_log"]]
    assert actions == [
        "preview_created",
        "submitted",
        "approved",
        "published",
        "withdrawn",
    ]
    # 哈希审计链：每条指纹包含前一条，篡改即断链。
    from app.services_public import verify_audit_chain

    assert verify_audit_chain(body["audit_log"]) is True
    tampered = [dict(e) for e in body["audit_log"]]
    tampered[2]["action"] = "forged"
    assert verify_audit_chain(tampered) is False
    # 撤回原因与各阶段操作人均留存。
    assert body["withdrawal_reason"] == "rule update required"
    assert body["created_by"] == "officer-1"
    assert body["submitted_by"] == "officer-1"


def test_privacy_impact_preview_shows_suppression(client, frozen_plan):
    pv = frozen_plan
    # 提高 k 到 6：below(5) 触发最小样本，meets(12) 因互补 5 < 6 被互补抑制，
    # 整个组织被抑制 —— 隐私影响必须在发布前明确告知。
    resp = _preview(client, pv, summary_id="PUB-STRICT", min_cell_size=6)
    assert resp.status_code == 200
    body = resp.json()
    assert body["document"]["organizations"] == []
    suppressed = body["privacy_impact"]["organizations_suppressed"]
    assert suppressed[0]["organization"] == "ORG-A"
    assert suppressed[0]["total_students"] == 17
    reasons = {c["category"]: c["reason"] for c in suppressed[0]["suppressed_cells"]}
    assert reasons == {"below": "min_sample", "meets": "complement"}
    assert body["privacy_impact"]["suppressed_students"] == 17


def test_rule_upgrade_does_not_rewrite_old_summary(client, frozen_plan):
    pv = frozen_plan
    first = _preview(client, pv, summary_id="PUB-V1", min_cell_size=5).json()
    fingerprint_v1 = first["document_fingerprint"]
    client.post(
        f"/api/plans/{pv}/public-summaries/PUB-V1/submit",
        json={
            "acknowledged": True,
            "document_fingerprint": fingerprint_v1,
        },
        headers=OFFICER,
    )
    client.post(
        f"/api/plans/{pv}/public-summaries/PUB-V1/approve",
        json={"note": "ok"},
        headers=APPROVER,
    )
    client.post(
        f"/api/plans/{pv}/public-summaries/PUB-V1/publish", headers=PUBLISHER
    )

    # 已提交的摘要不能被新规则覆盖刷新。
    resp = _preview(client, pv, summary_id="PUB-V1", min_cell_size=8)
    assert resp.status_code == 409
    # 已发布旧摘要的文档与规则版本保持原样。
    old = client.get(
        f"/api/plans/{pv}/public-summaries/PUB-V1", headers=AUDITOR
    ).json()
    assert old["ruleset_version"] == "v1"
    assert old["privacy_rules"]["min_cell_size"] == 5
    assert old["document_fingerprint"] == fingerprint_v1

    # 规则升级必须用新 summary_id 重新走完整流程。
    new = _preview(client, pv, summary_id="PUB-V2", min_cell_size=8).json()
    assert new["document"]["organizations"] == []
    assert new["document_fingerprint"] != fingerprint_v1


def test_preview_is_refreshable_while_in_preview(client, frozen_plan):
    pv = frozen_plan
    k5 = _preview(client, pv, summary_id="PUB-DRAFT", min_cell_size=5).json()
    k7 = _preview(client, pv, summary_id="PUB-DRAFT", min_cell_size=7).json()
    assert k7["document_fingerprint"] != k5["document_fingerprint"]
    actions = [e["action"] for e in k7["audit_log"]]
    assert actions == ["preview_created", "preview_refreshed"]
    # 刷新只允许在 preview 状态。
    client.post(
        f"/api/plans/{pv}/public-summaries/PUB-DRAFT/submit",
        json={
            "acknowledged": True,
            "document_fingerprint": k7["document_fingerprint"],
        },
        headers=OFFICER,
    )
    assert _preview(client, pv, summary_id="PUB-DRAFT", min_cell_size=5).status_code == 409


def test_reject_returns_to_preview_and_history_remains(client, frozen_plan):
    pv = frozen_plan
    preview = _preview(client, pv, summary_id="PUB-R").json()
    client.post(
        f"/api/plans/{pv}/public-summaries/PUB-R/submit",
        json={
            "acknowledged": True,
            "document_fingerprint": preview["document_fingerprint"],
        },
        headers=OFFICER,
    )
    resp = client.post(
        f"/api/plans/{pv}/public-summaries/PUB-R/reject",
        json={"note": "please raise k"},
        headers=APPROVER,
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "preview"
    actions = [e["action"] for e in resp.json()["audit_log"]]
    assert actions == ["preview_created", "submitted", "rejected"]


def test_permission_isolation_between_roles(client, frozen_plan):
    pv = frozen_plan

    # 无身份头一律拒绝内部接口。
    resp = client.post(
        f"/api/plans/{pv}/public-summaries/PUB-X/preview",
        json={"freeze_id": "F-1", "summary_id": "PUB-X"},
    )
    assert resp.status_code == 403

    # 错误角色不能预览。
    assert _preview(client, pv, summary_id="PUB-X", headers=APPROVER).status_code == 403
    assert _preview(client, pv, summary_id="PUB-X", headers=PUBLISHER).status_code == 403

    preview = _preview(client, pv, summary_id="PUB-X").json()
    fingerprint = preview["document_fingerprint"]

    # 隐私官不能审批；审批人不能提交。
    resp = client.post(
        f"/api/plans/{pv}/public-summaries/PUB-X/submit",
        json={"acknowledged": True, "document_fingerprint": fingerprint},
        headers=APPROVER,
    )
    assert resp.status_code == 403
    client.post(
        f"/api/plans/{pv}/public-summaries/PUB-X/submit",
        json={"acknowledged": True, "document_fingerprint": fingerprint},
        headers=OFFICER,
    )
    resp = client.post(
        f"/api/plans/{pv}/public-summaries/PUB-X/approve",
        json={"note": ""},
        headers=PUBLISHER,
    )
    assert resp.status_code == 403
    client.post(
        f"/api/plans/{pv}/public-summaries/PUB-X/approve",
        json={"note": "ok"},
        headers=APPROVER,
    )

    # 审批人/隐私官不能发布。
    resp = client.post(
        f"/api/plans/{pv}/public-summaries/PUB-X/publish", headers=APPROVER
    )
    assert resp.status_code == 403

    # 审计员只读：不能驱动任何状态变更。
    for path, body in [
        ("preview", {"freeze_id": "F-1", "summary_id": "PUB-AUD"}),
        ("submit", {"acknowledged": True, "document_fingerprint": fingerprint}),
        ("withdraw", {"reason": "x"}),
    ]:
        resp = client.post(
            f"/api/plans/{pv}/public-summaries/PUB-X/{path}",
            json=body,
            headers=AUDITOR,
        )
        assert resp.status_code == 403
    # 但审计员可读任意状态摘要（含隐私影响）。
    assert (
        client.get(
            f"/api/plans/{pv}/public-summaries/PUB-X", headers=AUDITOR
        ).status_code
        == 200
    )

    # 公开接口始终无需身份。
    client.post(
        f"/api/plans/{pv}/public-summaries/PUB-X/publish", headers=PUBLISHER
    )
    assert client.get(f"/api/public/plans/{pv}/summaries").status_code == 200


def test_public_cross_filters(client, frozen_plan):
    pv = frozen_plan
    preview = _preview(client, pv, summary_id="PUB-F").json()
    client.post(
        f"/api/plans/{pv}/public-summaries/PUB-F/submit",
        json={
            "acknowledged": True,
            "document_fingerprint": preview["document_fingerprint"],
        },
        headers=OFFICER,
    )
    client.post(
        f"/api/plans/{pv}/public-summaries/PUB-F/approve",
        json={"note": "ok"},
        headers=APPROVER,
    )
    client.post(
        f"/api/plans/{pv}/public-summaries/PUB-F/publish", headers=PUBLISHER
    )

    # 类别交叉筛选：分母被剥离，无法用减法反推互补格。
    resp = client.get(
        f"/api/public/plans/{pv}/summaries",
        params={"organization": "ORG-A", "category": "meets"},
    )
    assert resp.status_code == 200
    doc = resp.json()[0]
    org = doc["organizations"][0]
    assert org["cells"][0]["category"] == "meets"
    assert org["total_students"] is None
    assert org["meets_rate"] is None
    assert doc["totals"] is None
    assert doc["filters"] == {"organization": "ORG-A", "category": "meets"}

    # 无筛选时返回分母与总体。
    full = client.get(f"/api/public/plans/{pv}/summaries").json()[0]
    assert full["organizations"][0]["total_students"] == 17
    assert full["totals"]["student_count"] == 17

    # 筛选不存在的组织返回空列表（不泄露组织是否存在）。
    assert (
        client.get(
            f"/api/public/plans/{pv}/summaries",
            params={"organization": "SECRET-ORG"},
        ).json()
        == []
    )


def test_concurrent_publish_only_one_wins(client, frozen_plan):
    """两个不同摘要竞争同一冻结的发布位，只有一个成功。"""

    pv = frozen_plan
    for sid in ("PUB-C1", "PUB-C2"):
        preview = _preview(client, pv, summary_id=sid).json()
        client.post(
            f"/api/plans/{pv}/public-summaries/{sid}/submit",
            json={
                "acknowledged": True,
                "document_fingerprint": preview["document_fingerprint"],
            },
            headers=OFFICER,
        )
        client.post(
            f"/api/plans/{pv}/public-summaries/{sid}/approve",
            json={"note": "ok"},
            headers=APPROVER,
        )

    from app import services_public

    outcomes: list[str] = []
    lock = threading.Lock()

    def _publish(sid: str) -> None:
        session = TestSessionLocal()
        try:
            row = services_public.publish(
                session,
                actor=__import__("app.security", fromlist=["Actor"]).Actor(
                    "publisher-1", frozenset({"publisher"})
                ),
                plan_version=pv,
                summary_id=sid,
            )
            with lock:
                outcomes.append(f"ok:{row.summary_id}")
        except services_public.PublishConflictError:
            with lock:
                outcomes.append(f"conflict:{sid}")
        finally:
            session.close()

    threads = [
        threading.Thread(target=_publish, args=("PUB-C1",)),
        threading.Thread(target=_publish, args=("PUB-C2",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes).count("ok:PUB-C1") + sorted(outcomes).count(
        "ok:PUB-C2"
    ) == 1
    assert any(o.startswith("conflict") for o in outcomes)

    # 同一冻结只有一个公开可见摘要。
    public = client.get(f"/api/public/plans/{pv}/summaries").json()
    assert len(public) == 1

    # 撤回赢家后，败者仍处于 approved，可以接替发布（无需重新预览）。
    winner = public[0]["summary_id"]
    client.post(
        f"/api/plans/{pv}/public-summaries/{winner}/withdraw",
        json={"reason": "cycle"},
        headers=PUBLISHER,
    )
    loser = "PUB-C2" if winner == "PUB-C1" else "PUB-C1"
    resp = client.post(
        f"/api/plans/{pv}/public-summaries/{loser}/publish", headers=PUBLISHER
    )
    assert resp.status_code == 200, resp.text
    public_after = client.get(f"/api/public/plans/{pv}/summaries").json()
    assert [d["summary_id"] for d in public_after] == [loser]
    # 赢家的撤回记录与其完整审计链仍保留在内部视图中。
    internal = client.get(
        f"/api/plans/{pv}/public-summaries/{winner}", headers=AUDITOR
    ).json()
    assert internal["state"] == "withdrawn"
    assert len(internal["audit_log"]) == 5


def test_deterministic_aggregation_across_requests(client, frozen_plan):
    pv = frozen_plan
    first = _preview(client, pv, summary_id="PUB-D1").json()
    second = _preview(client, pv, summary_id="PUB-D2").json()
    # 同一冻结、同一规则、不同 summary_id：文档指纹必须一致（summary_id
    # 不参与文档指纹，只固化为元数据）。
    assert (
        first["document"]["document_fingerprint"]
        == second["document"]["document_fingerprint"]
    )
    assert first["document"]["organizations"] == second["document"]["organizations"]
    assert first["privacy_impact"]["organizations_suppressed"] == []


def test_preview_requires_existing_freeze(client, frozen_plan):
    pv = frozen_plan
    resp = _preview(client, pv, summary_id="PUB-MISSING", freeze_id="F-404")
    assert resp.status_code == 404


def test_illegal_transitions_are_rejected(client, frozen_plan):
    pv = frozen_plan
    _preview(client, pv, summary_id="PUB-T")
    # preview 不能直接审批或发布。
    assert (
        client.post(
            f"/api/plans/{pv}/public-summaries/PUB-T/approve",
            json={"note": "x"},
            headers=APPROVER,
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/plans/{pv}/public-summaries/PUB-T/publish",
            headers=PUBLISHER,
        ).status_code
        == 409
    )
    # 撤回不存在或未发布的摘要都失败。
    assert (
        client.post(
            f"/api/plans/{pv}/public-summaries/PUB-T/withdraw",
            json={"reason": "x"},
            headers=PUBLISHER,
        ).status_code
        == 409
    )


def test_concurrent_approval_only_one_wins(client, frozen_plan):
    """并发审批同一待批摘要：CAS 保证只有一个审批人成功。"""

    pv = frozen_plan
    preview = _preview(client, pv, summary_id="PUB-A").json()
    client.post(
        f"/api/plans/{pv}/public-summaries/PUB-A/submit",
        json={
            "acknowledged": True,
            "document_fingerprint": preview["document_fingerprint"],
        },
        headers=OFFICER,
    )

    from app.security import Actor
    from app import services_public

    results: list[str] = []
    lock = threading.Lock()

    def _approve(actor_id: str) -> None:
        session = TestSessionLocal()
        try:
            sp_actor = Actor(actor_id, frozenset({"approver"}))
            services_public.approve(
                session,
                actor=sp_actor,
                plan_version=pv,
                summary_id="PUB-A",
                note=f"approved by {actor_id}",
            )
            with lock:
                results.append(f"ok:{actor_id}")
        except services_public.SummaryStateConflict:
            with lock:
                results.append(f"conflict:{actor_id}")
        finally:
            session.close()

    threads = [
        threading.Thread(target=_approve, args=("approver-1",)),
        threading.Thread(target=_approve, args=("approver-2",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["conflict:approver-2", "ok:approver-1"] or sorted(
        results
    ) == ["conflict:approver-1", "ok:approver-2"]
    approved_by = client.get(
        f"/api/plans/{pv}/public-summaries/PUB-A", headers=AUDITOR
    ).json()["approved_by"]
    assert approved_by in {"approver-1", "approver-2"}
    assert len(results) == 2


def test_internal_listing_filters_and_auditor_visibility(client, frozen_plan):
    pv = frozen_plan
    preview = _preview(client, pv, summary_id="PUB-L").json()
    client.post(
        f"/api/plans/{pv}/public-summaries/PUB-L/submit",
        json={
            "acknowledged": True,
            "document_fingerprint": preview["document_fingerprint"],
        },
        headers=OFFICER,
    )

    resp = client.get(
        f"/api/plans/{pv}/public-summaries",
        params={"state": "preview", "state": "pending_approval"},
        headers=AUDITOR,
    )
    assert resp.status_code == 200
    assert resp.json()[0]["summary_id"] == "PUB-L"
    assert resp.json()[0]["state"] == "pending_approval"

    resp = client.get(
        f"/api/plans/{pv}/public-summaries",
        params={"state": "published"},
        headers=AUDITOR,
    )
    assert resp.json() == []

    # 无角色不能访问内部列表。
    assert client.get(f"/api/plans/{pv}/public-summaries").status_code == 403
