"""并发换版与强负载差分测试。"""

import random
import threading

import pytest

from app import compat, samples
from app.schema_limits import event_is_valid, validate_contract_schema
from tests.test_compat import (
    _brute_force_covered,
    _random_schema,
)


# --------------------------------------------------------------------------- #
# 更大规模的差分模糊（500 组随机合同对）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", range(500))
def test_heavy_fuzz_vs_brute_force(seed):
    rng = random.Random(10_000 + seed)
    producer = _random_schema(rng)
    cleaner = _random_schema(rng)
    brute = _brute_force_covered(producer, cleaner)
    ce = compat.find_counterexample(producer, cleaner)
    assert (ce is None) == brute
    if ce is not None:
        assert event_is_valid(ce, producer)
        assert not event_is_valid(ce, cleaner)


def test_max_six_fields_and_four_branches_fuzz():
    """贴近上限的合同：4 分支 × 6 字段也要在合理时间内精确判定。"""
    rng = random.Random(7)
    names = [f"f{i}" for i in range(6)]

    def big_schema():
        for _ in range(100):
            branches = []
            ok = True
            for _ in range(4):
                chosen = rng.sample(names, rng.randint(3, 6))
                props = {n: _random_prop(rng) for n in chosen}
                req = rng.sample(chosen, rng.randint(1, len(chosen)))
                # required 字段取值域不能为空，否则分支不可满足
                from app import domains
                if any(domains.is_empty(domains.domain_of(props[n])) for n in req):
                    ok = False
                    break
                branches.append({
                    "properties": props,
                    "required": req,
                    "additionalProperties": False,
                })
            if not ok:
                continue
            s = {"anyOf": branches}
            try:
                validate_contract_schema(s)
            except Exception:
                continue
            return s
        pytest.fail("无法生成合法的大规模合同")

    p, c = big_schema(), big_schema()
    # 仅要求可终止且结果自洽（暴力枚举在 6 字段下太慢，这里只验证反例性质）
    ce = compat.find_counterexample(p, c)
    if ce is not None:
        assert event_is_valid(ce, p) and not event_is_valid(ce, c)
    assert compat.find_counterexample(p, p) is None


def _random_prop(rng):
    from tests.test_compat import _random_prop as rp
    return rp(rng)


# --------------------------------------------------------------------------- #
# 并发换版：多线程同时提交，成功/冲突计数与最终修订必须自洽
# --------------------------------------------------------------------------- #
def test_concurrent_publish_serialization(client):
    cur = client.get("/api/endpoints/producer-main").json()["revision"]
    n = 8
    results = []
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        r = client.put(
            "/api/endpoints/producer-main/contract",
            json={
                "candidate_schema": samples.PRODUCER_V2_COMPATIBLE,
                "expected_revision": cur,  # 全部基于同一旧修订，最多一个成功
            },
        )
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    activated = [r for code, r in results if code == 200 and r.get("result") == "activated"]
    conflicts = [r for code, r in results if code == 409]
    rejected = [r for code, r in results if code == 200 and r.get("result") == "rejected_incompatible"]
    assert len(activated) + len(conflicts) + len(rejected) == n
    assert len(activated) <= 1
    final = client.get("/api/endpoints/producer-main").json()["revision"]
    if activated:
        assert final == cur + 1
    else:
        assert final == cur


def test_concurrent_without_expected_revision_all_rejudged(client):
    """不带 expected_revision 时，每次都按提交时当前合同重判，串行成功。"""
    cur = client.get("/api/endpoints/producer-main").json()["revision"]
    n = 5
    results = []
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        r = client.put(
            "/api/endpoints/producer-main/contract",
            json={"candidate_schema": samples.PRODUCER_V2_COMPATIBLE},
        )
        results.append(r.json())

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 该候选始终兼容，因此全部成功，修订恰好增加 n
    assert all(r["result"] == "activated" for r in results)
    final = client.get("/api/endpoints/producer-main").json()["revision"]
    assert final == cur + n
