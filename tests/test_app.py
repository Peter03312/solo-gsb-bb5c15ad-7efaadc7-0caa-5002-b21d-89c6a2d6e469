import threading

import pytest

from app import create_app

ADMIN = {"username": "admin", "password": "admin123"}
OP1 = {"username": "operator1", "password": "op123456"}
OP2 = {"username": "operator2", "password": "op234567"}
VIEWER = {"username": "viewer", "password": "view1234"}
REASON = "受试者发生严重不良事件，需紧急揭盲以指导救治"


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.db"),
            "SECRET_KEY": "test",
            "MIN_REASON_LENGTH": 10,
        }
    )
    return app.test_client()


def login(client, creds):
    r = client.post("/api/login", json=creds)
    assert r.status_code == 200, r.get_json()


def make_study(client, name="研究A", **over):
    payload = {
        "name": name,
        "seed": 42,
        "groups": ["试验组", "安慰剂组"],
        "strata": [
            {"name": "中心1", "count": 10},
            {"name": "中心2", "count": 10},
        ],
    }
    payload.update(over)
    r = client.post("/api/studies", json=payload)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["study_id"]


def study_status(client, study_id):
    return client.get(f"/api/studies/{study_id}").get_json()["study"]["status"]


def first_code(client, study_id):
    return client.get(f"/api/studies/{study_id}/codes").get_json()["codes"][0]["code"]


def replace(client, study_id, code, reason="样品运输途中损毁", request_id="req-1", **extra):
    payload = {
        "original_code": code,
        "reason": reason,
        "request_id": request_id,
    }
    payload.update(extra)
    return client.post(
        f"/api/studies/{study_id}/replacements", json=payload
    )


def test_health(client):
    assert client.get("/api/health").get_json()["ok"] is True


def test_generate_success_and_blinding(client):
    login(client, ADMIN)
    sid = make_study(client)
    r = client.post(f"/api/studies/{sid}/generate")
    assert r.status_code == 201
    assert r.get_json()["count"] == 20

    # 管理员可见映射，且每层每组数量平衡
    mapping = client.get(f"/api/studies/{sid}/mapping").get_json()["mapping"]
    assert len(mapping) == 20
    balance = {}
    for m in mapping:
        key = (m["stratum"], m["group_name"])
        balance[key] = balance.get(key, 0) + 1
    assert set(balance.values()) == {5}

    # 样品码不含组别含义
    assert all(m["code"].startswith("SMP-") for m in mapping)

    # 未授权用户看不到组别
    login(client, VIEWER)
    r = client.get(f"/api/studies/{sid}/mapping")
    assert r.status_code == 403
    codes = client.get(f"/api/studies/{sid}/codes").get_json()
    assert codes["blinded"] is True
    assert all(c["group_name"] is None for c in codes["codes"])


def test_unbalanced_rejected(client):
    login(client, ADMIN)
    sid = make_study(client, strata=[{"name": "中心1", "count": 9}])  # 9 不能被 2 整除
    r = client.post(f"/api/studies/{sid}/generate")
    assert r.status_code == 400
    assert any("平衡" in e for e in r.get_json()["errors"])
    assert client.get(f"/api/studies/{sid}/codes").get_json()["codes"] == []


def test_missing_count_rejected(client):
    login(client, ADMIN)
    sid = make_study(client, strata=[{"name": "中心1", "count": None}])
    r = client.post(f"/api/studies/{sid}/generate")
    assert r.status_code == 400
    assert any("缺少" in e for e in r.get_json()["errors"])
    assert client.get(f"/api/studies/{sid}/codes").get_json()["codes"] == []


def test_regeneration_conflict_rejects_whole_batch(client):
    login(client, ADMIN)
    sid = make_study(client)
    assert client.post(f"/api/studies/{sid}/generate").status_code == 201

    # 固定种子下再次生成会得到相同编码 -> 整批拒绝并列出冲突
    r = client.post(f"/api/studies/{sid}/generate")
    body = r.get_json()
    assert r.status_code == 409
    assert body["ok"] is False
    assert len(body["conflicts"]) == 20

    # 未新增任何编码
    assert len(client.get(f"/api/studies/{sid}/codes").get_json()["codes"]) == 20


def test_unblind_requires_two_distinct_operators(client):
    login(client, ADMIN)
    sid = make_study(client)
    client.post(f"/api/studies/{sid}/generate")

    r = client.post(
        f"/api/studies/{sid}/unblind",
        json={
            "operator1": "operator1", "password1": "op123456",
            "operator2": "operator1", "password2": "op123456",
            "reason": REASON,
        },
    )
    assert r.status_code == 400
    assert any("不同" in e for e in r.get_json()["errors"])
    assert study_status(client, sid) == "active"


def test_unblind_rejects_bad_password_and_short_reason(client):
    login(client, ADMIN)
    sid = make_study(client)
    client.post(f"/api/studies/{sid}/generate")

    r = client.post(
        f"/api/studies/{sid}/unblind",
        json={
            "operator1": "operator1", "password1": "wrong",
            "operator2": "operator2", "password2": "op234567",
            "reason": REASON,
        },
    )
    assert r.status_code == 400
    assert any("验证失败" in e for e in r.get_json()["errors"])

    r = client.post(
        f"/api/studies/{sid}/unblind",
        json={
            "operator1": "operator1", "password1": "op123456",
            "operator2": "operator2", "password2": "op234567",
            "reason": "太短",
        },
    )
    assert r.status_code == 400
    assert any("理由" in e for e in r.get_json()["errors"])

    # 失败不改变任何状态
    assert study_status(client, sid) == "active"
    login(client, VIEWER)
    assert client.get(f"/api/studies/{sid}/mapping").status_code == 403


def test_unblind_rejects_operator_without_permission(client):
    login(client, ADMIN)
    sid = make_study(client)
    client.post(f"/api/studies/{sid}/generate")
    r = client.post(
        f"/api/studies/{sid}/unblind",
        json={
            "operator1": "operator1", "password1": "op123456",
            "operator2": "viewer", "password2": "view1234",
            "reason": REASON,
        },
    )
    assert r.status_code == 400
    assert any("权限" in e for e in r.get_json()["errors"])
    assert study_status(client, sid) == "active"


def test_unblind_success_records_snapshot(client):
    login(client, ADMIN)
    sid = make_study(client)
    client.post(f"/api/studies/{sid}/generate")

    r = client.post(
        f"/api/studies/{sid}/unblind",
        json={
            "operator1": "operator1", "password1": "op123456",
            "operator2": "operator2", "password2": "op234567",
            "reason": REASON,
        },
    )
    assert r.status_code == 200, r.get_json()
    assert study_status(client, sid) == "unblinded"

    events = client.get(f"/api/studies/{sid}/unblinding-events").get_json()["events"]
    assert len(events) == 1
    event = events[0]
    assert {event["operator1"], event["operator2"]} == {"operator1", "operator2"}
    assert event["reason"] == REASON
    assert event["created_at"]
    assert len(event["snapshot"]["mapping"]) == 20

    # 揭盲后映射对所有登录用户可读
    login(client, VIEWER)
    assert client.get(f"/api/studies/{sid}/mapping").status_code == 200

    # 不能重复揭盲
    r = client.post(
        f"/api/studies/{sid}/unblind",
        json={
            "operator1": "operator1", "password1": "op123456",
            "operator2": "operator2", "password2": "op234567",
            "reason": REASON,
        },
    )
    assert r.status_code == 400
    assert any("已揭盲" in e for e in r.get_json()["errors"])


def test_unblind_without_codes_rejected(client):
    login(client, ADMIN)
    sid = make_study(client)
    r = client.post(
        f"/api/studies/{sid}/unblind",
        json={
            "operator1": "operator1", "password1": "op123456",
            "operator2": "operator2", "password2": "op234567",
            "reason": REASON,
        },
    )
    assert r.status_code == 400
    assert any("尚未发放" in e for e in r.get_json()["errors"])


def test_replacement_success_status_chain_and_blinding(client):
    login(client, ADMIN)
    sid = make_study(client)
    client.post(f"/api/studies/{sid}/generate")
    mapping = client.get(f"/api/studies/{sid}/mapping").get_json()["mapping"]
    original = mapping[0]
    original_code = original["code"]

    r = replace(client, sid, original_code)
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["retried"] is False
    new_code = body["replacement"]["replacement_code"]
    assert new_code != original_code
    assert body["replacement"]["original_code"] == original_code

    # 原码作废、新码继承同一分层/处理组，二者均保留历史
    admin_mapping = {m["code"]: m for m in client.get(
        f"/api/studies/{sid}/mapping"
    ).get_json()["mapping"]}
    assert admin_mapping[original_code]["status"] == "voided"
    assert admin_mapping[original_code]["replaced_by_code"] == new_code
    assert admin_mapping[new_code]["status"] == "active"
    assert admin_mapping[new_code]["replaces_code"] == original_code
    assert admin_mapping[new_code]["stratum"] == original["stratum"]
    assert admin_mapping[new_code]["group_name"] == original["group_name"]
    chain = client.get(
        f"/api/studies/{sid}/replacement-requests"
    ).get_json()["replacements"]
    assert chain[0]["replacement_code"] == new_code

    detail = client.get(f"/api/studies/{sid}").get_json()["study"]
    assert detail["total_code_count"] == 21
    assert detail["active_code_count"] == 20
    assert detail["voided_code_count"] == 1

    # 普通用户能看到状态/替换关系，但仍不能看到处理组
    login(client, VIEWER)
    codes = {c["code"]: c for c in client.get(
        f"/api/studies/{sid}/codes"
    ).get_json()["codes"]}
    assert codes[original_code]["status"] == "voided"
    assert codes[original_code]["replaced_by_code"] == new_code
    assert codes[new_code]["status"] == "active"
    assert codes[new_code]["replaces_code"] == original_code
    assert codes[new_code]["group_name"] is None
    assert client.get(f"/api/studies/{sid}/mapping").status_code == 403
    chain = client.get(
        f"/api/studies/{sid}/replacement-requests"
    ).get_json()["replacements"]
    assert chain[0]["replacement_code"] == new_code
    assert "group_name" not in chain[0]


def test_replacement_idempotency_and_parameter_conflict(client):
    login(client, ADMIN)
    sid = make_study(client)
    client.post(f"/api/studies/{sid}/generate")
    codes = client.get(f"/api/studies/{sid}/codes").get_json()["codes"]
    original_code, other_code = codes[0]["code"], codes[1]["code"]

    first = replace(client, sid, original_code, request_id="idem")
    assert first.status_code == 201
    new_code = first.get_json()["replacement"]["replacement_code"]

    retry = replace(client, sid, original_code, request_id="idem")
    assert retry.status_code == 200
    assert retry.get_json()["retried"] is True
    assert retry.get_json()["replacement"]["replacement_code"] == new_code

    changed_code = replace(client, sid, other_code, request_id="idem")
    assert changed_code.status_code == 409
    changed_reason = replace(
        client, sid, original_code, reason="另一个原因", request_id="idem"
    )
    assert changed_reason.status_code == 409

    rows = client.get(f"/api/studies/{sid}/codes").get_json()["codes"]
    assert len(rows) == 21
    assert all(c["code"] != new_code or c["status"] == "active" for c in rows)


def test_replacement_failures_leave_data_unchanged(client):
    login(client, ADMIN)
    sid = make_study(client)
    client.post(f"/api/studies/{sid}/generate")
    original_code = first_code(client, sid)

    missing = replace(client, sid, "SMP-NOTEXIST", request_id="missing")
    assert missing.status_code == 404
    blank_reason = replace(client, sid, original_code, reason="   ", request_id="blank")
    assert blank_reason.status_code == 400

    succeeded = replace(client, sid, original_code, request_id="done")
    assert succeeded.status_code == 201
    voided = replace(
        client, sid, original_code, reason="再次损毁", request_id="again"
    )
    assert voided.status_code == 409

    assert len(client.get(f"/api/studies/{sid}/codes").get_json()["codes"]) == 21
    login(client, VIEWER)
    denied = replace(client, sid, original_code, request_id="denied")
    assert denied.status_code == 403


def test_replacement_after_unblinding_is_idempotent_but_new_request_rejected(client):
    login(client, ADMIN)
    sid = make_study(client)
    client.post(f"/api/studies/{sid}/generate")
    original_code = first_code(client, sid)
    before = replace(client, sid, original_code, request_id="before-unblind")
    assert before.status_code == 201
    new_code = before.get_json()["replacement"]["replacement_code"]

    assert client.post(
        f"/api/studies/{sid}/unblind",
        json={
            "operator1": "operator1", "password1": "op123456",
            "operator2": "operator2", "password2": "op234567",
            "reason": REASON,
        },
    ).status_code == 200

    retry = replace(client, sid, original_code, request_id="before-unblind")
    assert retry.status_code == 200
    assert retry.get_json()["replacement"]["replacement_code"] == new_code

    active_code = next(
        c["code"]
        for c in client.get(f"/api/studies/{sid}/codes").get_json()["codes"]
        if c["code"] != new_code and c["status"] == "active"
    )
    new_request = replace(client, sid, active_code, request_id="after-unblind")
    assert new_request.status_code == 400
    assert any("已揭盲" in e for e in new_request.get_json()["errors"])
    assert len(client.get(f"/api/studies/{sid}/codes").get_json()["codes"]) == 21

    snapshot = client.get(
        f"/api/studies/{sid}/unblinding-events"
    ).get_json()["events"][0]["snapshot"]
    assert len(snapshot["mapping"]) == 21
    assert snapshot["replacements"][0]["original_code"] == original_code
    assert snapshot["replacements"][0]["replacement_code"] == new_code


def test_concurrent_replacement_same_code_succeeds_once(client):
    login(client, ADMIN)
    sid = make_study(client)
    client.post(f"/api/studies/{sid}/generate")
    original_code = first_code(client, sid)
    barrier = threading.Barrier(2)
    results = []

    def worker(index):
        barrier.wait()
        r = replace(client, sid, original_code, request_id=f"parallel-{index}")
        results.append((r.status_code, r.get_json()))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(status for status, _ in results)
    assert statuses == [201, 409]
    assert len(client.get(f"/api/studies/{sid}/codes").get_json()["codes"]) == 21


def test_pages_render(client):
    login(client, ADMIN)
    sid = make_study(client)
    client.post(f"/api/studies/{sid}/generate")
    replace(client, sid, first_code(client, sid), request_id="page-replace")
    for path in [
        "/",
        f"/studies/{sid}",
        f"/studies/{sid}/mapping",
        f"/studies/{sid}/unblind",
    ]:
        assert client.get(path).status_code == 200, path

    event_id = client.post(
        f"/api/studies/{sid}/unblind",
        json={
            "operator1": "operator1", "password1": "op123456",
            "operator2": "operator2", "password2": "op234567",
            "reason": REASON,
        },
    ).get_json()["event_id"]
    assert client.get(f"/unblinding/{event_id}").status_code == 200
