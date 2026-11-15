"""合同兼容性的精确判定引擎。

判定命题：**生产合同 P 允许的每一个事件对象，都被清洗器合同 C 接受**。

思路（完整判定，不是抽样）：

1. 对 P 的每个分支，按「可选字段的出现组合」枚举至多 2^k 个键集合
   （k≤6，故至多 64 个）。每个键集合在该分支上确定一个 *生产者盒子*：
   各字段取值域的笛卡尔积。
2. 选出能接受该键集合对象的清洗器分支：其 required 字段都在对象中，且对象
   的每个键都在其 properties 中声明（分支都禁止额外字段；清洗器额外声明的
   可选字段不影响它接受不含这些字段的对象）。
3. 判定生产者盒子是否被这些清洗器分支盒子的并集覆盖。字段取值域是
   null / 整数区间 / 字符串集合三类片段的有限并，覆盖问题用递归拆分求解：
   逐维在清洗器域的边界点/枚举点上把生产者域切成互不相交的片段，叶子节点
   只需做「单片段 ⊆ 单片段」判断。所有拆分都由清洗器域中有限的边界决定，
   因此递归必然终止；未被任何清洗器盒子覆盖的叶子会构造出一个具体的
   反例事件。
"""

from __future__ import annotations

from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

from . import domains
from .schema_limits import branch_matches

Counterexample = Dict[str, Any]


class BranchUnsatisfiable(ValueError):
    """合同分支的 required 字段取值域为空，该分支永远无法被命中。"""


# --------------------------------------------------------------------------- #
# 分支的静态结构
# --------------------------------------------------------------------------- #
class BranchInfo:
    def __init__(self, branch: dict):
        self.branch = branch
        self.props: Dict[str, dict] = branch["properties"]
        self.required: set[str] = set(branch.get("required", []))
        self.optional: set[str] = set(self.props) - self.required
        # 每个字段的完整取值域
        self.domains: Dict[str, domains.Domain] = {
            name: domains.domain_of(prop) for name, prop in self.props.items()
        }

    def check_satisfiable(self) -> None:
        for name in self.required:
            if domains.is_empty(self.domains[name]):
                raise BranchUnsatisfiable(
                    f"required 字段 {name!r} 的取值域为空（enum/区间无交集），分支不可满足"
                )

    def key_sets(self) -> List[Tuple[str, ...]]:
        """该分支可以产出的全部键集合：required ∪ 任意可选字段子集。"""
        opts = sorted(self.optional)
        base = tuple(sorted(self.required))
        result = [base]
        for r in range(1, len(opts) + 1):
            for combo in combinations(opts, r):
                result.append(tuple(sorted(base + combo)))
        return result


# --------------------------------------------------------------------------- #
# 盒子覆盖：递归拆分
# --------------------------------------------------------------------------- #
def _find_uncovered_point(
    fields: List[str],
    producer_box: Dict[str, domains.Domain],
    cleaner_boxes: List[Dict[str, domains.Domain]],
    idx: int,
    assignment: Dict[str, Any],
) -> Optional[Counterexample]:
    """在生产者盒子中找一个不被任何清洗器盒子覆盖的点；找不到返回 None。

    逐维递归：在第 idx 维上，用所有清洗器盒子该维片段的边界/枚举点把生产者
    域拆成互不相交的小片段。拆分保证每个小片段对每个清洗器片段要么整体包含、
    要么完全不相交。叶子节点直接做「小片段 ⊆ 某清洗器同维片段」的整体判断，
    避免用代表值推断整体包含。
    """
    if idx == len(fields):
        # 叶子：当前逐维小片段的笛卡尔积是否被某个清洗器盒子整体包含
        for box in cleaner_boxes:
            if all(
                domains.is_subset(producer_box[f], box[f])
                for f in fields
            ):
                return None
        return {f: domains.pick(producer_box[f]) for f in fields}

    field = fields[idx]
    p_domain = producer_box[field]
    cutter_parts: List[domains.Part] = []
    for box in cleaner_boxes:
        cutter_parts.extend(box[field])
    pieces = _split_by(p_domain, cutter_parts)

    for piece in pieces:
        sub_dom: domains.Domain = [piece]
        assignment[field] = domains.pick(sub_dom)
        original = producer_box[field]
        producer_box[field] = sub_dom
        point = _find_uncovered_point(
            fields, producer_box, cleaner_boxes, idx + 1, assignment
        )
        producer_box[field] = original
        del assignment[field]
        if point is not None:
            return point
    return None


def _split_by(dom: domains.Domain, cutter_parts: List[domains.Part]) -> List[domains.Part]:
    """用清洗器片段的边界/枚举点把生产者域拆成互不相交的有序片段。

    注意：拆分出的片段即使同类型相邻也**不做并集合并**——例如 "all" 被枚举点
    切开后必须保留「有限集合」与「其余全部」两个片段，因为清洗器对它们的
    包含关系可能不同。
    """
    out: List[domains.Part] = []
    for part in dom:
        if part[0] == "int":
            out.extend(_split_int(part, [c for c in cutter_parts if c[0] == "int"]))
        elif part[0] == "str":
            out.extend(_split_str_many(part, [c for c in cutter_parts if c[0] == "str"]))
        else:
            out.append(part)
    return _disjoint_normalize(out)


def _split_int(part: domains.Part, cutters: List[domains.Part]) -> List[domains.Part]:
    """一次收集所有切割区间的有限端点，统一分段（正确处理无界区间）。

    在每个有限边界 b 的 b-1/b/b+1 处插入分段点，保证切出的每个片段对每个
    切割区间要么整体包含、要么完全不相交（不会出现部分相交）。
    """
    if not cutters:
        return [part]
    lo, hi = part[1], part[2]
    boundary: set[int] = set()
    if lo is not None:
        boundary.add(lo)
    if hi is not None:
        boundary.add(hi)
    for clo, chi in ((c[1], c[2]) for c in cutters):
        # 闭区间 [clo, chi]：在每个有限端点的两侧及端点本身都插入分段点
        # （clo-1/clo/clo+1、chi-1/chi/chi+1），保证切出的每个片段相对每个
        # 切割区间要么整体包含、要么完全不相交。无界端不产生分段点。
        for b in (clo, chi):
            if b is None:
                continue
            for p in (b - 1, b, b + 1):
                if (lo is None or p >= lo) and (hi is None or p <= hi):
                    boundary.add(p)

    points = sorted(boundary)
    pieces: List[domains.Part] = []
    start = lo  # 可能为 None（负无穷）
    for p in points:
        if start is not None and p < start:
            continue
        pieces.append(("int", start, p))
        start = p + 1
    if hi is None or start is None or start <= hi:
        pieces.append(("int", start, hi))
    return pieces


def _disjoint_normalize(parts: List[domains.Part]) -> List[domains.Part]:
    """归一化但不合并片段（片段已由切割逻辑保证互不相交）。"""
    null_parts = [p for p in parts if p[0] == "null"]
    seen: set = set()
    int_parts = []
    for p in parts:
        if p[0] == "int" and p not in seen:
            seen.add(p)
            int_parts.append(p)
    int_parts.sort(key=lambda p: (1, 0) if p[1] is None else (0, p[1]))
    str_parts = [p for p in parts if p[0] == "str"]
    return (null_parts[:1] if null_parts else []) + int_parts + str_parts


def _split_str_many(part: domains.Part, cutters: List[domains.Part]) -> List[domains.Part]:
    """在所有切割片段涉及的有限枚举点处切开字符串域。

    有限集合中的每个枚举值都独立成片（不同清洗器分支可能只覆盖其中一部分，
    合并成一个集合会让「整体包含」判断失效）；其余值归入剩余片段。
    """
    if not cutters:
        return [part]
    cur = domains._str_set(part)
    points: set[str] = set()
    for c in cutters:
        s = domains._str_set(c)
        if s[0] in ("set", "allbut"):
            points |= set(s[1])
    if not points:
        return [part]
    if cur[0] == "set":
        inside = points & set(cur[1])
    elif cur[0] == "allbut":
        inside = points - set(cur[1])
    else:  # all
        inside = points
    if not inside:
        return [part]

    pieces: List[domains.Part] = [("str", "set", frozenset([v])) for v in sorted(inside)]
    singleton_union = ("set", frozenset(inside))
    rem = domains._str_diff(cur, singleton_union)
    if rem is not None:
        pieces.append(("str", rem[0], *rem[1:]))
    return pieces


# --------------------------------------------------------------------------- #
# 合同级包含判定
# --------------------------------------------------------------------------- #
def find_counterexample(producer_schema: dict, cleaner_schema: dict) -> Optional[Counterexample]:
    """返回一个「生产者合法但清洗器拒收」的事件；全部被接受时返回 None。"""
    p_branches = [BranchInfo(b) for b in producer_schema["anyOf"]]
    c_branches = [BranchInfo(b) for b in cleaner_schema["anyOf"]]
    for bi in p_branches + c_branches:
        bi.check_satisfiable()

    for pbi in p_branches:
        for key_set in pbi.key_sets():
            keys = list(key_set)
            producer_box = {k: pbi.domains[k] for k in keys}
            # 清洗器分支能接受该键集合对象的条件（additionalProperties=false）：
            # 1) 它要求的 required 字段都在对象中；
            # 2) 对象的每个键都在它的 properties 中声明。
            # 清洗器额外声明的可选字段不影响接受。
            key_set_s = set(keys)
            eligible = [
                cbi
                for cbi in c_branches
                if cbi.required <= key_set_s and key_set_s <= set(cbi.props)
            ]
            if not eligible:
                point = {k: domains.pick(producer_box[k]) for k in keys}
                return point
            # 盒子只取对象实际拥有的键
            cleaner_boxes = [{k: cbi.domains[k] for k in keys} for cbi in eligible]
            point = _find_uncovered_point(keys, dict(producer_box), cleaner_boxes, 0, {})
            if point is not None:
                return point
    return None


def is_accepted_by_all(producer_schema: dict, cleaner_schemas: List[dict]) -> bool:
    return all(find_counterexample(producer_schema, c) is None for c in cleaner_schemas)


def check_producer_event(
    event: Counterexample, producer_schema: dict, cleaner_schema: dict
) -> Tuple[bool, bool]:
    """自检辅助：返回 (生产者是否合法, 清洗器是否接受)。"""
    p_ok = any(branch_matches(event, b) for b in producer_schema["anyOf"])
    c_ok = any(branch_matches(event, b) for b in cleaner_schema["anyOf"])
    return p_ok, c_ok
