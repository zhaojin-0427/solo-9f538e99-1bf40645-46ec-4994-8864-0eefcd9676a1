"""兼容性引擎测试：手工用例 + 对暴力枚举的差分测试 + jsonschema 交叉校验。"""

import itertools
import random

import jsonschema
import pytest

from app import compat
from app.schema_limits import event_is_valid, validate_contract_schema

# --------------------------------------------------------------------------- #
# 手工用例
# --------------------------------------------------------------------------- #
PRODUCER = {
    "anyOf": [
        {
            "properties": {
                "event_id": {"type": "string"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "x": {"type": "integer", "minimum": 0, "maximum": 4096},
                "dwell_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
            },
            "required": ["event_id", "button"],
            "additionalProperties": False,
        }
    ]
}


def test_identical_contracts_compatible():
    assert compat.find_counterexample(PRODUCER, PRODUCER) is None


def test_superset_cleaner_compatible():
    cleaner = {
        "anyOf": [
            {
                "properties": {
                    "event_id": {"type": "string"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "x": {"type": "integer", "minimum": 0, "maximum": 99999},
                    "dwell_ms": {"type": ["integer", "null"], "minimum": 0, "maximum": 999999},
                },
                "required": ["event_id"],
                "additionalProperties": False,
            }
        ]
    }
    assert compat.find_counterexample(PRODUCER, cleaner) is None


def test_enum_narrowing_gives_counterexample():
    cleaner = {
        "anyOf": [
            {
                "properties": {
                    "event_id": {"type": "string"},
                    "button": {"type": "string", "enum": ["left", "right"]},
                    "x": {"type": "integer", "minimum": 0, "maximum": 4096},
                    "dwell_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
                },
                "required": ["event_id", "button"],
                "additionalProperties": False,
            }
        ]
    }
    ce = compat.find_counterexample(PRODUCER, cleaner)
    assert ce is not None
    assert event_is_valid(ce, PRODUCER)
    assert not event_is_valid(ce, cleaner)
    assert ce["button"] == "middle"


def test_integer_range_narrowing_gives_counterexample():
    cleaner = {
        "anyOf": [
            {
                "properties": {
                    "event_id": {"type": "string"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "x": {"type": "integer", "minimum": 0, "maximum": 100},
                    "dwell_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
                },
                "required": ["event_id", "button"],
                "additionalProperties": False,
            }
        ]
    }
    ce = compat.find_counterexample(PRODUCER, cleaner)
    assert ce is not None
    assert event_is_valid(ce, PRODUCER)
    assert not event_is_valid(ce, cleaner)
    assert ce["x"] == 101


def test_required_narrowing_rejects():
    """清洗器多要求一个必填字段时，生产者不发该字段的事件应成为反例。"""
    cleaner = {
        "anyOf": [
            {
                "properties": {
                    "event_id": {"type": "string"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "x": {"type": "integer", "minimum": 0, "maximum": 4096},
                    "dwell_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
                },
                "required": ["event_id", "button", "x"],
                "additionalProperties": False,
            }
        ]
    }
    ce = compat.find_counterexample(PRODUCER, cleaner)
    assert ce is not None
    assert "x" not in ce
    assert event_is_valid(ce, PRODUCER)
    assert not event_is_valid(ce, cleaner)


def test_cleaner_extra_optional_field_still_accepts():
    """清洗器额外声明一个可选字段（不 required）不影响接受生产者事件。"""
    cleaner = {
        "anyOf": [
            {
                "properties": {
                    "event_id": {"type": "string"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "x": {"type": "integer", "minimum": 0, "maximum": 4096},
                    "dwell_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
                    "trace": {"type": "string"},
                },
                "required": ["event_id", "button"],
                "additionalProperties": False,
            }
        ]
    }
    assert compat.find_counterexample(PRODUCER, cleaner) is None


def test_cleaner_extra_required_key_rejects():
    """清洗器把额外字段设为 required 时，生产者不发该字段的事件成为反例。"""
    cleaner = {
        "anyOf": [
            {
                "properties": {
                    "event_id": {"type": "string"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "x": {"type": "integer", "minimum": 0, "maximum": 4096},
                    "dwell_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
                    "trace": {"type": "string"},
                },
                "required": ["event_id", "button", "trace"],
                "additionalProperties": False,
            }
        ]
    }
    ce = compat.find_counterexample(PRODUCER, cleaner)
    assert ce is not None
    assert "trace" not in ce
    assert event_is_valid(ce, PRODUCER) and not event_is_valid(ce, cleaner)


def test_multi_branch_union_coverage():
    """清洗器用两个分支（按 button 拆分）联合覆盖生产者。"""
    base = {
        "event_id": {"type": "string"},
        "x": {"type": "integer", "minimum": 0, "maximum": 4096},
        "dwell_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
    }
    cleaner = {
        "anyOf": [
            {"properties": {**base, "button": {"type": "string", "enum": ["left", "right"]}},
             "required": ["event_id", "button"], "additionalProperties": False},
            {"properties": {**base, "button": {"type": "string", "enum": ["middle"]}},
             "required": ["event_id", "button"], "additionalProperties": False},
        ]
    }
    assert compat.find_counterexample(PRODUCER, cleaner) is None


def test_multi_branch_union_with_gap():
    """两个分支枚举合起来缺一个值，必须能找到反例。"""
    base = {
        "event_id": {"type": "string"},
        "x": {"type": "integer", "minimum": 0, "maximum": 4096},
        "dwell_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
    }
    cleaner = {
        "anyOf": [
            {"properties": {**base, "button": {"type": "string", "enum": ["left"]}},
             "required": ["event_id", "button"], "additionalProperties": False},
            {"properties": {**base, "button": {"type": "string", "enum": ["right"]}},
             "required": ["event_id", "button"], "additionalProperties": False},
        ]
    }
    ce = compat.find_counterexample(PRODUCER, cleaner)
    assert ce is not None
    assert ce["button"] == "middle"
    assert event_is_valid(ce, PRODUCER) and not event_is_valid(ce, cleaner)


def test_null_type_handling():
    p = {"anyOf": [{"properties": {"v": {"type": ["integer", "null"],
                                         "minimum": -5, "maximum": 5}},
                    "required": ["v"], "additionalProperties": False}]}
    c_ok = {"anyOf": [{"properties": {"v": {"type": ["integer", "null"],
                                            "minimum": -10, "maximum": 10}},
                       "required": ["v"], "additionalProperties": False}]}
    c_bad = {"anyOf": [{"properties": {"v": {"type": "integer",
                                             "minimum": -10, "maximum": 10}},
                        "required": ["v"], "additionalProperties": False}]}
    assert compat.find_counterexample(p, c_ok) is None
    ce = compat.find_counterexample(p, c_bad)
    assert ce == {"v": None}


def test_optional_fields_key_set_enumeration():
    p = {
        "anyOf": [
            {"properties": {"a": {"type": "integer", "minimum": 0, "maximum": 3},
                            "b": {"type": "string"}},
             "required": ["a"], "additionalProperties": False}
        ]
    }
    # 清洗器只接受同时带 a,b 的对象 -> 缺 b 的生产者事件成为反例
    c = {
        "anyOf": [
            {"properties": {"a": {"type": "integer", "minimum": 0, "maximum": 3},
                            "b": {"type": "string"}},
             "required": ["a", "b"], "additionalProperties": False}
        ]
    }
    ce = compat.find_counterexample(p, c)
    assert ce is not None and "b" not in ce


def test_unsatisfiable_branch_detected():
    p = {"anyOf": [{"properties": {"a": {"type": "integer", "enum": [1], "minimum": 5}},
                    "required": ["a"], "additionalProperties": False}]}
    with pytest.raises(compat.BranchUnsatisfiable):
        compat.find_counterexample(p, p)


# --------------------------------------------------------------------------- #
# 差分模糊测试：引擎判定 vs 有限值空间暴力枚举
# --------------------------------------------------------------------------- #
INTS = [-32, -7, -1, 0, 1, 5, 9, 10, 11, 50, 99, 100]
STRS = ["a", "b", "c", "click", "view"]
VALUES = [None] + INTS + STRS


def _random_prop(rng: random.Random) -> dict:
    types = rng.sample(["string", "integer", "null"], rng.randint(1, 3))
    prop: dict = {"type": types if len(types) > 1 else types[0]}
    if "integer" in types and rng.random() < 0.7:
        lo = rng.choice([None, -10, 0, 5, 50])
        hi = rng.choice([None, 1, 10, 100])
        if lo is not None:
            prop["minimum"] = lo
        if hi is not None:
            prop["maximum"] = hi
    if rng.random() < 0.6:
        pool: list = []
        if "null" in types:
            pool.append(None)
        if "integer" in types:
            pool.extend(rng.sample(INTS, rng.randint(1, 4)))
        if "string" in types:
            pool.extend(rng.sample(STRS, rng.randint(1, 3)))
        rng.shuffle(pool)
        prop["enum"] = pool
    return prop


def _random_schema(rng: random.Random) -> dict:
    n_fields = rng.randint(1, 4)
    names = [f"f{i}" for i in range(n_fields)]
    n_branches = rng.randint(1, 3)
    branches = []
    for _ in range(n_branches):
        chosen = rng.sample(names, rng.randint(1, len(names)))
        props = {n: _random_prop(rng) for n in chosen}
        required = rng.sample(chosen, rng.randint(1, len(chosen)))
        branches.append({
            "properties": props,
            "required": required,
            "additionalProperties": False,
        })
    schema = {"anyOf": branches}
    try:
        validate_contract_schema(schema)
    except Exception:
        return _random_schema(rng)
    # 排除不可满足分支
    for b in branches:
        for name in b["required"]:
            from app import domains
            if domains.is_empty(domains.domain_of(b["properties"][name])):
                return _random_schema(rng)
    return schema


def _brute_force_covered(producer: dict, cleaner: dict) -> bool:
    """枚举有限值空间内所有生产者合法事件，检查是否全部被清洗器接受。"""
    field_names = set()
    for b in producer["anyOf"]:
        field_names.update(b["properties"])
    names = sorted(field_names)
    # 每个字段的出现/取值组合；键集合按子集枚举
    for r in range(0, len(names) + 1):
        for present in itertools.combinations(names, r):
            present_set = set(present)
            choices = []
            for n in present:
                choices.append([v for v in VALUES if _field_allows(producer, n, v)])
            if any(not c for c in choices):
                continue
            for combo in itertools.product(*choices):
                event = dict(zip(present, combo))
                if event_is_valid(event, producer):
                    if not event_is_valid(event, cleaner):
                        return False
    return True


def _field_allows(schema: dict, name: str, value) -> bool:
    for b in schema["anyOf"]:
        if name in b["properties"]:
            from app.schema_limits import _check_value
            if _check_value(value, b["properties"][name]):
                return True
    return False


@pytest.mark.parametrize("seed", range(120))
def test_engine_matches_brute_force(seed):
    rng = random.Random(seed)
    producer = _random_schema(rng)
    cleaner = _random_schema(rng)

    brute = _brute_force_covered(producer, cleaner)
    ce = compat.find_counterexample(producer, cleaner)
    engine = ce is None

    assert engine == brute, (
        f"seed={seed} 引擎与暴力枚举不一致\nproducer={producer}\n"
        f"cleaner={cleaner}\nce={ce}"
    )
    if ce is not None:
        # 反例必须名副其实
        assert event_is_valid(ce, producer)
        assert not event_is_valid(ce, cleaner)
        # jsonschema 库交叉确认
        jsonschema.Draft202012Validator(producer).validate(ce)
        assert jsonschema.Draft202012Validator(cleaner).iter_errors(ce)
        assert any(True for _ in jsonschema.Draft202012Validator(cleaner).iter_errors(ce))


def test_jsonschema_library_accepts_our_subset():
    """我们生成的合同必须是标准 2020-12 合法 schema。"""
    rng = random.Random(4242)
    for _ in range(20):
        s = _random_schema(rng)
        jsonschema.Draft202012Validator.check_schema(s)
