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


def test_pages_render(client):
    login(client, ADMIN)
    sid = make_study(client)
    client.post(f"/api/studies/{sid}/generate")
    for path in ["/", f"/studies/{sid}", f"/studies/{sid}/mapping", f"/studies/{sid}/unblind"]:
        assert client.get(path).status_code == 200, path


# ---------------------------------------------------------------- 样品补发

def _first_code(client, sid):
    return client.get(f"/api/studies/{sid}/codes").get_json()["codes"][0]


def _replace(client, sid, old_code, reason="样品运输途中损毁，无法检测", request_id="req-1"):
    return client.post(
        f"/api/studies/{sid}/replacements",
        json={"old_code": old_code, "reason": reason, "request_id": request_id},
    )


def _setup_with_codes(client):
    login(client, ADMIN)
    sid = make_study(client)
    client.post(f"/api/studies/{sid}/generate")
    old = _first_code(client, sid)
    return sid, old


def test_replace_success_voids_old_and_inherits_group(client):
    sid, old = _setup_with_codes(client)
    r = _replace(client, sid, old["code"])
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["replayed"] is False
    repl = body["replacement"]
    assert repl["old_code"] == old["code"]
    assert repl["new_code"].startswith("SMP-")
    assert repl["new_code"] != old["code"]

    # 新码继承原分层与处理组（管理员可见组别）
    admin_mapping = {
        m["code"]: m for m in client.get(f"/api/studies/{sid}/mapping").get_json()["mapping"]
    }
    o, n = admin_mapping[old["code"]], admin_mapping[repl["new_code"]]
    assert o["status"] == "voided"
    assert o["replaced_by_code"] == repl["new_code"]
    assert n["status"] == "active"
    assert n["replacement_of_code"] == old["code"]
    assert n["stratum"] == o["stratum"]
    assert n["group_name"] == o["group_name"]

    # 有效码总数不变（原码作废 + 新码有效）
    codes = client.get(f"/api/studies/{sid}/codes").get_json()["codes"]
    assert len(codes) == 21
    assert sum(1 for c in codes if c["status"] == "active") == 20
    assert sum(1 for c in codes if c["status"] == "voided") == 1

    # 补发记录可查（含请求标识、原因、继承的分层/组别）
    repls = client.get(f"/api/studies/{sid}/replacements").get_json()["replacements"]
    assert len(repls) == 1
    assert repls[0]["old_code"] == old["code"]
    assert repls[0]["new_code"] == repl["new_code"]
    assert repls[0]["stratum"] == o["stratum"]
    assert repls[0]["group_name"] == o["group_name"]


def test_replace_idempotent_same_request_id_returns_original(client):
    sid, old = _setup_with_codes(client)
    reason = "样品运输途中损毁，无法检测"
    r1 = _replace(client, sid, old["code"], reason=reason, request_id="idem-1")
    assert r1.status_code == 201
    new_code = r1.get_json()["replacement"]["new_code"]

    r2 = _replace(client, sid, old["code"], reason=reason, request_id="idem-1")
    assert r2.status_code == 200
    body = r2.get_json()
    assert body["replayed"] is True
    assert body["replacement"]["new_code"] == new_code

    # 未产生第二条补发/第二个新码
    codes = client.get(f"/api/studies/{sid}/codes").get_json()["codes"]
    assert sum(1 for c in codes if c["status"] == "voided") == 1
    assert len(client.get(f"/api/studies/{sid}/replacements").get_json()["replacements"]) == 1


def test_replace_same_request_id_changed_params_rejected(client):
    sid, old = _setup_with_codes(client)
    assert _replace(client, sid, old["code"], request_id="dup-1").status_code == 201

    codes = client.get(f"/api/studies/{sid}/codes").get_json()["codes"]
    other = next(c for c in codes if c["code"] != old["code"] and c["status"] == "active")

    # 改换样品码 -> 409
    r = _replace(client, sid, other["code"], request_id="dup-1")
    assert r.status_code == 409
    # 改换原因 -> 409
    r = _replace(client, sid, old["code"], reason="另一个不同的补发原因哦", request_id="dup-1")
    assert r.status_code == 409

    # 数据未变
    codes = client.get(f"/api/studies/{sid}/codes").get_json()["codes"]
    assert sum(1 for c in codes if c["status"] == "voided") == 1


def test_replace_validation_failures_leave_data_unchanged(client):
    sid, old = _setup_with_codes(client)

    # 码不存在
    r = _replace(client, sid, "SMP-NOPE000")
    assert r.status_code == 400
    assert any("不存在" in e for e in r.get_json()["errors"])

    # 原因空白
    r = _replace(client, sid, old["code"], reason="   ")
    assert r.status_code == 400
    assert any("原因" in e for e in r.get_json()["errors"])

    # 请求标识空白
    r = _replace(client, sid, old["code"], request_id="")
    assert r.status_code == 400

    # 已作废的码
    _replace(client, sid, old["code"], request_id="ok-1")
    r = _replace(client, sid, old["code"], request_id="other-2")
    assert r.status_code == 400
    assert any("作废" in e for e in r.get_json()["errors"])

    codes = client.get(f"/api/studies/{sid}/codes").get_json()["codes"]
    assert len(codes) == 21  # 只有一次成功补发
    assert sum(1 for c in codes if c["status"] == "voided") == 1


def test_replace_rejected_after_unblinding_and_retry_returns_original(client):
    sid, old = _setup_with_codes(client)
    r1 = _replace(client, sid, old["code"], request_id="before-unblind")
    assert r1.status_code == 201
    original = r1.get_json()["replacement"]

    client.post(
        f"/api/studies/{sid}/unblind",
        json={
            "operator1": "operator1", "password1": "op123456",
            "operator2": "operator2", "password2": "op234567",
            "reason": REASON,
        },
    )

    # 揭盲后新请求失败且不改变数据
    codes = client.get(f"/api/studies/{sid}/codes").get_json()["codes"]
    active = next(c for c in codes if c["status"] == "active")
    r = _replace(client, sid, active["code"], request_id="after-unblind")
    assert r.status_code == 400
    assert any("揭盲" in e for e in r.get_json()["errors"])
    assert len(client.get(f"/api/studies/{sid}/codes").get_json()["codes"]) == 21

    # 揭盲前成功的请求在揭盲后重试仍返回原记录
    r = _replace(client, sid, old["code"], request_id="before-unblind")
    assert r.status_code == 200
    assert r.get_json()["replacement"]["new_code"] == original["new_code"]


def test_replace_admin_only(client):
    login(client, ADMIN)
    sid = make_study(client)
    client.post(f"/api/studies/{sid}/generate")
    code = _first_code(client, sid)["code"]
    login(client, {"username": "operator1", "password": "op123456"})
    r = _replace(client, sid, code)
    assert r.status_code == 403
    login(client, VIEWER)
    r = _replace(client, sid, code)
    assert r.status_code == 403


def test_viewer_blinded_to_replacement_group_before_unblinding(client):
    sid, old = _setup_with_codes(client)
    _replace(client, sid, old["code"])

    login(client, VIEWER)
    codes = client.get(f"/api/studies/{sid}/codes").get_json()
    assert codes["blinded"] is True
    for c in codes["codes"]:
        assert c["group_name"] is None
        # 状态与替换关系仍可见，且不泄露组别
        assert c["status"] in ("active", "voided")
    repls = client.get(f"/api/studies/{sid}/replacements").get_json()
    assert all(r["group_name"] is None for r in repls["replacements"])


def test_unblinding_snapshot_keeps_full_replacement_chain(client):
    sid, old = _setup_with_codes(client)
    _replace(client, sid, old["code"], request_id="chain-1")

    client.post(
        f"/api/studies/{sid}/unblind",
        json={
            "operator1": "operator1", "password1": "op123456",
            "operator2": "operator2", "password2": "op234567",
            "reason": REASON,
        },
    )
    snap = client.get(f"/api/studies/{sid}/unblinding-events").get_json()["events"][0]["snapshot"]

    by_code = {m["code"]: m for m in snap["mapping"]}
    assert by_code[old["code"]]["status"] == "voided"
    assert len(snap["replacements"]) == 1
    chain = snap["replacements"][0]
    assert chain["old_code"] == old["code"]
    assert by_code[chain["new_code"]]["replacement_of_code"] == old["code"]
    assert chain["request_id"] == "chain-1"
    assert chain["reason"]
    assert chain["group_name"] == by_code[chain["new_code"]]["group_name"]


def test_concurrent_replacements_of_same_code_only_one_succeeds(client):
    import threading

    login(client, ADMIN)
    sid = make_study(client)
    client.post(f"/api/studies/{sid}/generate")
    old = _first_code(client, sid)["code"]
    reason = "样品运输途中损毁，无法检测"

    results = []
    barrier = threading.Barrier(2)

    def worker(rid):
        c = client.application.test_client()
        r = c.post("/api/login", json=ADMIN)
        assert r.status_code == 200
        barrier.wait()
        resp = c.post(
            f"/api/studies/{sid}/replacements",
            json={"old_code": old, "reason": reason, "request_id": rid},
        )
        results.append((resp.status_code, resp.get_json()))

    t1 = threading.Thread(target=worker, args=("conc-1",))
    t2 = threading.Thread(target=worker, args=("conc-2",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    statuses = sorted(s for s, _ in results)
    assert statuses[0] in (200, 201)
    assert statuses[1] == 400  # 第二个请求：码已作废
    codes = client.get(f"/api/studies/{sid}/codes").get_json()["codes"]
    assert sum(1 for c in codes if c["status"] == "voided") == 1
    assert len([c for c in codes if c["replacement_of_code"]]) == 1
    assert len(client.get(f"/api/studies/{sid}/replacements").get_json()["replacements"]) == 1


def test_replacement_code_globally_unique_across_studies(client):
    login(client, ADMIN)
    sid1 = make_study(client, name="研究甲")
    client.post(f"/api/studies/{sid1}/generate")
    sid2 = make_study(client, name="研究乙")
    client.post(f"/api/studies/{sid2}/generate")

    code1 = _first_code(client, sid1)["code"]
    r = _replace(client, sid1, code1, request_id="g-1")
    assert r.status_code == 201
    new_code = r.get_json()["replacement"]["new_code"]

    other = client.get(f"/api/studies/{sid2}/codes").get_json()["codes"]
    assert all(c["code"] != new_code for c in other)
    assert new_code != code1


def test_multi_hop_replacement_chain(client):
    sid, a = _setup_with_codes(client)
    r1 = _replace(client, sid, a["code"], request_id="hop-1")
    b = r1.get_json()["replacement"]["new_code"]
    assert r1.get_json()["replacement"]["new_code_status"] == "active"

    # 新码再次损毁 -> 继续补发，形成 A -> B -> C
    r2 = _replace(client, sid, b, reason="第二次损毁原因足够长哦", request_id="hop-2")
    c = r2.get_json()["replacement"]["new_code"]

    by_code = {x["code"]: x for x in client.get(f"/api/studies/{sid}/codes").get_json()["codes"]}
    assert by_code[a["code"]]["status"] == "voided"
    assert by_code[a["code"]]["replaced_by_code"] == b
    assert by_code[b]["status"] == "voided"
    assert by_code[b]["replacement_of_code"] == a["code"]
    assert by_code[b]["replaced_by_code"] == c
    assert by_code[c]["status"] == "active"
    assert by_code[c]["replacement_of_code"] == b

    # 重试第一跳：仍返回原记录 B，但 B 此刻已作废
    r = _replace(client, sid, a["code"], request_id="hop-1")
    assert r.status_code == 200
    assert r.get_json()["replayed"] is True
    assert r.get_json()["replacement"]["new_code"] == b
    assert r.get_json()["replacement"]["new_code_status"] == "voided"

