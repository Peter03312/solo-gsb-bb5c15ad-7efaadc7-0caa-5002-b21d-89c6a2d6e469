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
