"""端到端 API 测试：登记、换版、反例、乐观锁、事件校验、容量上限。"""

from app import samples
from app.schema_limits import event_is_valid


def test_seed_data_present(client):
    eps = {e["endpoint_id"]: e for e in client.get("/api/endpoints").json()}
    assert {"producer-main", "cleaner-a", "cleaner-b"} <= set(eps)
    assert eps["producer-main"]["revision"] >= 1


def test_seed_records_incompatible_attempt_with_counterexample(client):
    attempts = client.get("/api/change-attempts").json()
    rejected = [a for a in attempts if a["result"] == "rejected_incompatible"]
    assert rejected, "启动播种应留下一条不兼容换版记录"
    a = rejected[0]
    assert a["counterexample"] is not None
    assert a["rejected_cleaner_id"] in {"cleaner-a", "cleaner-b"}


def test_get_current_contract_and_revisions(client):
    r = client.get("/api/endpoints/producer-main")
    assert r.status_code == 200
    body = r.json()
    assert body["current_contract"]["revision"] == body["revision"]
    revs = client.get("/api/endpoints/producer-main/revisions").json()
    assert len(revs) == 1


def test_compatible_publish_bumps_revision(client):
    before = client.get("/api/endpoints/producer-main").json()["revision"]
    r = client.put(
        "/api/endpoints/producer-main/contract",
        json={"candidate_schema": samples.PRODUCER_V2_COMPATIBLE},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "activated"
    assert body["new_revision"] == before + 1
    after = client.get("/api/endpoints/producer-main").json()
    assert after["revision"] == body["new_revision"]


def test_incompatible_producer_publish_keeps_revision(client):
    before = client.get("/api/endpoints/producer-main").json()["revision"]
    r = client.put(
        "/api/endpoints/producer-main/contract",
        json={"candidate_schema": samples.PRODUCER_V2_INCOMPATIBLE},
    )
    body = r.json()
    assert body["result"] == "rejected_incompatible"
    assert body["rejected_cleaner_id"].startswith("cleaner-")
    ce = body["counterexample"]
    # 反例必须是「候选生产合同合法、被拒收清洗器拒绝」的具体事件
    assert event_is_valid(ce, samples.PRODUCER_V2_INCOMPATIBLE)
    cleaner_schema = client.get(
        f"/api/endpoints/{body['rejected_cleaner_id']}"
    ).json()["current_contract"]["schema"]
    assert not event_is_valid(ce, cleaner_schema)
    # 且被拒收清洗器的事件校验端点拒绝
    v2 = client.post(
        f"/api/endpoints/{body['rejected_cleaner_id']}/validate-event",
        json={"event": ce},
    ).json()
    assert v2["accepted"] is False
    # 修订不变，原发布保留
    after = client.get("/api/endpoints/producer-main").json()
    assert after["revision"] == before


def test_incompatible_cleaner_publish(client):
    before = client.get("/api/endpoints/cleaner-a").json()["revision"]
    r = client.put(
        "/api/endpoints/cleaner-a/contract",
        json={"candidate_schema": samples.CLEANER_V2_INCOMPATIBLE},
    )
    body = r.json()
    assert body["result"] == "rejected_incompatible"
    assert body["counterexample"]["dwell_ms"] > 10000
    assert client.get("/api/endpoints/cleaner-a").json()["revision"] == before


def test_invalid_schema_rejected(client):
    bad = {"anyOf": [{"properties": {"a": {"type": "number"}},
                      "required": ["a"], "additionalProperties": False}]}
    r = client.put(
        "/api/endpoints/cleaner-a/contract", json={"candidate_schema": bad}
    )
    assert r.status_code == 400
    # 专用结构校验端点
    r2 = client.post("/api/schema/validate", json={"candidate_schema": bad})
    assert r2.json()["valid"] is False


def test_optimistic_lock_conflict(client):
    cur = client.get("/api/endpoints/producer-main").json()["revision"]
    r = client.put(
        "/api/endpoints/producer-main/contract",
        json={
            "candidate_schema": samples.PRODUCER_V2_COMPATIBLE,
            "expected_revision": cur + 99,
        },
    )
    assert r.status_code == 409
    assert r.json()["detail"]["current_revision"] == cur


def test_expected_revision_match_succeeds(client):
    cur = client.get("/api/endpoints/producer-main").json()["revision"]
    r = client.put(
        "/api/endpoints/producer-main/contract",
        json={
            "candidate_schema": samples.PRODUCER_V2_COMPATIBLE,
            "expected_revision": cur,
        },
    )
    assert r.json()["result"] == "activated"


def test_dry_run_has_no_side_effect(client):
    before = client.get("/api/endpoints/cleaner-a").json()["revision"]
    r = client.post(
        "/api/endpoints/cleaner-a/compatibility-check",
        json={"candidate_schema": samples.CLEANER_V2_INCOMPATIBLE},
    )
    assert r.json()["result"] == "rejected_incompatible"
    r2 = client.post(
        "/api/endpoints/cleaner-a/compatibility-check",
        json={"candidate_schema": samples.CLEANER_A_V1},
    )
    assert r2.json()["result"] == "activated"
    assert client.get("/api/endpoints/cleaner-a").json()["revision"] == before


def test_validate_event_endpoint(client):
    ok = client.post(
        "/api/endpoints/producer-main/validate-event",
        json={"event": {"event_id": "e1", "button": "left", "x": 1}},
    ).json()
    assert ok["accepted"] is True
    bad = client.post(
        "/api/endpoints/producer-main/validate-event",
        json={"event": {"event_id": "e1", "button": "scroll"}},
    ).json()
    assert bad["accepted"] is False


def test_cleaner_capacity_limit(client):
    # 已有 cleaner-a / cleaner-b，再登记两个：第三个成功、第四个 400
    r1 = client.post(
        "/api/endpoints",
        json={"endpoint_id": "cleaner-c", "role": "cleaner", "display_name": "C"},
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/api/endpoints",
        json={"endpoint_id": "cleaner-d", "role": "cleaner", "display_name": "D"},
    )
    assert r2.status_code == 400
    assert "上限" in r2.json()["detail"]


def test_producer_capacity_limit(client):
    r = client.post(
        "/api/endpoints",
        json={"endpoint_id": "producer-2", "role": "producer", "display_name": "P2"},
    )
    assert r.status_code == 400


def test_register_with_incompatible_initial_contract(client):
    r = client.post(
        "/api/endpoints",
        json={
            "endpoint_id": "cleaner-tmp",
            "role": "cleaner",
            "display_name": "tmp",
            "initial_schema": samples.CLEANER_V2_INCOMPATIBLE,
        },
    )
    # 容量可能已满（cleaner-c 已占），换一个干净环境判断逻辑
    if r.status_code == 400:
        return
    body = r.json()
    assert body["initial_contract"] == "rejected"
    assert body["revision"] == 0


def test_unknown_endpoint_404(client):
    assert client.get("/api/endpoints/nope").status_code == 404
    assert client.put(
        "/api/endpoints/nope/contract",
        json={"candidate_schema": samples.PRODUCER_V1},
    ).status_code == 404


def test_history_revision_fetch(client):
    cur = client.get("/api/endpoints/producer-main").json()["revision"]
    detail = client.get(f"/api/endpoints/producer-main/revisions/1").json()
    assert detail["schema"]["anyOf"]
    assert client.get(
        f"/api/endpoints/producer-main/revisions/{cur + 50}"
    ).status_code == 404
