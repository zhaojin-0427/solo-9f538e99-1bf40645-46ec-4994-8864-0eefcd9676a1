"""JSON Schema 2020-12 平面对象子集的结构校验，以及事件实例校验。

允许的合同形式（刻意收窄，保证兼容性可精确判定）：

* 顶层只有一个 anyOf，含 1~4 个对象分支；
* 每个分支只允许 properties / required / additionalProperties 三个键，
  且 additionalProperties 必须显式为 false（禁止额外字段）；
* 字段 schema 只允许 type / enum / minimum / maximum；
* type 仅 string / integer / null（单值或数组，数组元素不重复）；
* enum 值类型必须与 type 相容；minimum/maximum 仅用于 integer；
* 参与判定的字段名（所有分支 properties 键的并集）至多 6 个。
"""

from __future__ import annotations

from typing import Any, List

from . import domains

ALLOWED_ROOT_KEYS = {"anyOf", "$schema"}
ALLOWED_BRANCH_KEYS = {"properties", "required", "additionalProperties"}
ALLOWED_PROP_KEYS = {"type", "enum", "minimum", "maximum"}
ALLOWED_TYPES = {"string", "integer", "null"}
MAX_BRANCHES = 4
MAX_FIELD_NAMES = 6


class SchemaError(ValueError):
    """合同 schema 不在允许子集内。"""


def validate_contract_schema(schema: Any) -> None:
    """对候选合同做严格的子集校验，不合法直接抛 SchemaError（可含多条原因）。"""
    errors: List[str] = []

    if not isinstance(schema, dict):
        raise SchemaError("合同根节点必须是 JSON 对象")
    extra_root = set(schema) - ALLOWED_ROOT_KEYS
    if extra_root:
        errors.append(f"根节点只允许 {sorted(ALLOWED_ROOT_KEYS)}，出现: {sorted(extra_root)}")
    branches = schema.get("anyOf")
    if not isinstance(branches, list) or not branches:
        raise SchemaError("根节点必须包含非空数组 anyOf")
    if not 1 <= len(branches) <= MAX_BRANCHES:
        errors.append(f"anyOf 分支数必须在 1~{MAX_BRANCHES} 之间，当前 {len(branches)}")

    all_field_names: set[str] = set()
    for i, branch in enumerate(branches):
        ctx = f"anyOf[{i}]"
        if not isinstance(branch, dict):
            errors.append(f"{ctx} 必须是对象")
            continue
        extra = set(branch) - ALLOWED_BRANCH_KEYS
        if extra:
            errors.append(f"{ctx} 只允许 {sorted(ALLOWED_BRANCH_KEYS)}，出现: {sorted(extra)}")
        if branch.get("additionalProperties") is not False:
            errors.append(f"{ctx} 必须显式声明 additionalProperties=false（禁止额外字段）")

        props = branch.get("properties")
        if not isinstance(props, dict) or not props:
            errors.append(f"{ctx}.properties 必须是非空对象")
            continue
        all_field_names.update(props.keys())
        if len(all_field_names) > MAX_FIELD_NAMES:
            errors.append(
                f"参与判定的字段名并集至多 {MAX_FIELD_NAMES} 个，"
                f"当前 {sorted(all_field_names)}"
            )

        required = branch.get("required", [])
        if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
            errors.append(f"{ctx}.required 必须是字符串数组")
        elif missing := set(required) - set(props):
            errors.append(f"{ctx}.required 含未在 properties 声明的字段: {sorted(missing)}")
        if len(required) != len(set(required)):
            errors.append(f"{ctx}.required 存在重复字段")

        for name, prop in props.items():
            pctx = f"{ctx}.properties.{name}"
            if not isinstance(prop, dict):
                errors.append(f"{pctx} 必须是对象")
                continue
            pextra = set(prop) - ALLOWED_PROP_KEYS
            if pextra:
                errors.append(
                    f"{pctx} 只允许 {sorted(ALLOWED_PROP_KEYS)}，出现: {sorted(pextra)}"
                )
            _validate_prop(pctx, prop, errors)

    if errors:
        raise SchemaError("；".join(errors))


def _validate_prop(ctx: str, prop: dict, errors: List[str]) -> None:
    raw_type = prop.get("type")
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    if not raw_type or not all(isinstance(t, str) for t in types):
        errors.append(f"{ctx}.type 必须是 string/integer/null 或其数组")
        return
    if any(t not in ALLOWED_TYPES for t in types):
        errors.append(f"{ctx}.type 含不支持的类型: {types}")
    if len(set(types)) != len(types):
        errors.append(f"{ctx}.type 存在重复: {types}")
    type_set = set(types)

    if "minimum" in prop and not isinstance(prop["minimum"], int):
        errors.append(f"{ctx}.minimum 必须是整数")
    if "maximum" in prop and not isinstance(prop["maximum"], int):
        errors.append(f"{ctx}.maximum 必须是整数")
    if "minimum" in prop and "integer" not in type_set:
        errors.append(f"{ctx}.minimum 只能用于 integer 类型")
    if "maximum" in prop and "integer" not in type_set:
        errors.append(f"{ctx}.maximum 只能用于 integer 类型")
    if (
        isinstance(prop.get("minimum"), int)
        and isinstance(prop.get("maximum"), int)
        and prop["minimum"] > prop["maximum"]
    ):
        errors.append(f"{ctx}.minimum 大于 maximum，字段无可取值")

    if "enum" in prop:
        enum = prop["enum"]
        if not isinstance(enum, list) or not enum:
            errors.append(f"{ctx}.enum 必须是非空数组")
            return
        if len(set(map(_hash_key, enum))) != len(enum):
            errors.append(f"{ctx}.enum 存在重复值")
        for v in enum:
            if v is None:
                ok = "null" in type_set
            elif isinstance(v, bool):
                ok = False  # JSON 中 bool 不属于 integer
            elif isinstance(v, int):
                ok = "integer" in type_set
            elif isinstance(v, str):
                ok = "string" in type_set
            else:
                ok = False
            if not ok:
                errors.append(f"{ctx}.enum 值 {v!r} 与声明类型 {types} 不相容")
        if domains.is_empty(domains.domain_of(prop)):
            errors.append(f"{ctx} 的 enum 与 type/区间没有交集，字段无可取值")


def _hash_key(v: Any) -> Any:
    if isinstance(v, list):
        return tuple(v)
    return v


# --------------------------------------------------------------------------- #
# 实例校验
# --------------------------------------------------------------------------- #
def _check_value(value: Any, prop: dict) -> bool:
    types = prop["type"]
    if isinstance(types, str):
        types = [types]
    if value is None:
        type_ok = "null" in types
    elif isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str):
            type_ok = "string" in types
        else:
            type_ok = False
    else:
        type_ok = "integer" in types
    if not type_ok:
        return False
    if "enum" in prop and value not in prop["enum"]:
        return False
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in prop and value < prop["minimum"]:
            return False
        if "maximum" in prop and value > prop["maximum"]:
            return False
    return True


def branch_matches(event: dict, branch: dict) -> bool:
    """一个事件对象是否命中某分支（平面对象、禁止额外字段语义）。"""
    props = branch["properties"]
    if set(event) - set(props):
        return False
    for req in branch.get("required", []):
        if req not in event:
            return False
    for name, value in event.items():
        if not _check_value(value, props[name]):
            return False
    return True


def event_is_valid(event: Any, schema: dict) -> bool:
    """事件是否为该合同接受的合法事件（命中 anyOf 任意分支即可）。"""
    if not isinstance(event, dict):
        return False
    return any(branch_matches(event, b) for b in schema["anyOf"])
