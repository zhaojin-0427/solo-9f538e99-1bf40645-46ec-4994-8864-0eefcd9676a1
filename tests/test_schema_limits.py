"""合同子集结构校验测试。"""

import pytest

from app.schema_limits import SchemaError, event_is_valid, validate_contract_schema


def good_branch(**over):
    branch = {
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
        "additionalProperties": False,
    }
    branch.update(over)
    return {"anyOf": [branch]}


def test_valid_minimal():
    validate_contract_schema(good_branch())


def test_reject_non_dict_root():
    with pytest.raises(SchemaError):
        validate_contract_schema([])


def test_reject_extra_root_key():
    schema = good_branch()
    schema["$id"] = "x"
    with pytest.raises(SchemaError):
        validate_contract_schema(schema)


def test_branch_count_limits():
    with pytest.raises(SchemaError):
        validate_contract_schema({"anyOf": []})
    with pytest.raises(SchemaError):
        validate_contract_schema(
            {"anyOf": [good_branch()["anyOf"][0] for _ in range(5)]}
        )


def test_additional_properties_required():
    bad = {"anyOf": [{"properties": {"a": {"type": "string"}}, "required": ["a"]}]}
    with pytest.raises(SchemaError):
        validate_contract_schema(bad)


def test_unknown_prop_key():
    bad = good_branch(properties={"a": {"type": "string", "format": "uuid"}})
    with pytest.raises(SchemaError):
        validate_contract_schema(bad)


def test_unknown_type():
    bad = good_branch(properties={"a": {"type": "number"}})
    with pytest.raises(SchemaError):
        validate_contract_schema(bad)


def test_enum_type_mismatch():
    bad = good_branch(properties={"a": {"type": "string", "enum": ["x", 1]}})
    with pytest.raises(SchemaError):
        validate_contract_schema(bad)


def test_minimum_requires_integer():
    bad = good_branch(properties={"a": {"type": "string", "minimum": 1}})
    with pytest.raises(SchemaError):
        validate_contract_schema(bad)


def test_min_gt_max():
    bad = good_branch(properties={"a": {"type": "integer", "minimum": 9, "maximum": 1}})
    with pytest.raises(SchemaError):
        validate_contract_schema(bad)


def test_enum_empty_after_range():
    bad = good_branch(properties={"a": {"type": "integer", "enum": [1, 2], "minimum": 5}})
    with pytest.raises(SchemaError):
        validate_contract_schema(bad)


def test_required_must_be_declared():
    bad = good_branch(required=["a", "ghost"])
    with pytest.raises(SchemaError):
        validate_contract_schema(bad)


def test_too_many_field_names():
    props = {f"f{i}": {"type": "string"} for i in range(7)}
    bad = {"anyOf": [{"properties": props, "required": list(props), "additionalProperties": False}]}
    with pytest.raises(SchemaError):
        validate_contract_schema(bad)


def test_bool_not_integer_in_enum():
    bad = good_branch(properties={"a": {"type": "integer", "enum": [True]}})
    with pytest.raises(SchemaError):
        validate_contract_schema(bad)


def test_duplicate_enum_rejected():
    bad = good_branch(properties={"a": {"type": "string", "enum": ["x", "x"]}})
    with pytest.raises(SchemaError):
        validate_contract_schema(bad)


# --------------------------------------------------------------------------- #
# 实例校验
# --------------------------------------------------------------------------- #
def test_event_validation_basic():
    schema = good_branch(properties={
        "a": {"type": "string", "enum": ["x", "y"]},
        "b": {"type": ["integer", "null"], "minimum": 0, "maximum": 10},
    })
    assert event_is_valid({"a": "x", "b": 3}, schema)
    assert event_is_valid({"a": "y", "b": None}, schema)
    assert not event_is_valid({"a": "z"}, schema)       # 枚举不符
    assert not event_is_valid({"a": "x", "b": 11}, schema)  # 超界
    assert not event_is_valid({"a": "x", "c": 1}, schema)  # 额外字段
    assert not event_is_valid({"b": 1}, schema)         # 缺必填
    assert not event_is_valid([], schema)               # 非对象
    assert not event_is_valid({"a": "x", "b": True}, schema)  # bool 不算 integer


def test_any_of_branch_matching():
    schema = {
        "anyOf": [
            {"properties": {"k": {"type": "string", "enum": ["click"]}},
             "required": ["k"], "additionalProperties": False},
            {"properties": {"k": {"type": "string", "enum": ["view"]},
                            "n": {"type": "integer"}},
             "required": ["k", "n"], "additionalProperties": False},
        ]
    }
    assert event_is_valid({"k": "click"}, schema)
    assert event_is_valid({"k": "view", "n": 1}, schema)
    assert not event_is_valid({"k": "view"}, schema)
