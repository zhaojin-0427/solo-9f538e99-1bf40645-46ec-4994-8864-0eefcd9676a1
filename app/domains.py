"""取值域代数。

合同字段只允许 string / integer / null 三种原子类型，配合 enum 与整数
minimum/maximum。一个字段的取值域在这里表示为互不相交的若干 *片段*：

    ("null",)               单值 null
    ("int", lo, hi)         闭区间 [lo, hi]（lo/hi 为 int 或 None 表示无界）
    ("str", "all")          全部字符串
    ("str", "set", frozenset[str])  有限字符串集合
    ("str", "allbut", frozenset[str])  除有限集合外的全部字符串

空域用空列表表示。所有运算都返回归一化后的片段列表，片段之间两两不相交且按
确定性顺序排列，因此可以直接比较 / 递归。
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

Part = Tuple[Any, ...]
Domain = List[Part]


# --------------------------------------------------------------------------- #
# 归一化与基础运算
# --------------------------------------------------------------------------- #
def _norm_int(parts: List[Part]) -> List[Part]:
    """合并相交/相邻的整数闭区间（None 表示无界）。"""
    ivs = [(p[1], p[2]) for p in parts if p[0] == "int"]
    if any(lo is None and hi is None for lo, hi in ivs):
        return [("int", None, None)]

    # 转成半开整数区间 [lo, hi+1)；None 界用哨兵表示。
    NEG_INF = "neg"
    POS_INF = "pos"
    half_open = []
    for lo, hi in ivs:
        half_open.append((NEG_INF if lo is None else lo, POS_INF if hi is None else hi + 1))
    # NEG_INF 排最前；其余按有限下界排序
    half_open.sort(key=lambda x: (-1, 0) if x[0] == NEG_INF else (1, x[0]))

    merged: List[List[Any]] = []
    for lo, hi in half_open:
        if not merged:
            merged.append([lo, hi])
            continue
        last_lo, last_hi = merged[-1]
        # 相邻/相交判定（半开区间：lo <= last_hi 即相接）
        if lo == NEG_INF or (last_hi != POS_INF and lo <= last_hi):
            if hi == POS_INF:
                merged[-1][1] = POS_INF
            elif last_hi != POS_INF and hi > last_hi:
                merged[-1][1] = hi
        else:
            merged.append([lo, hi])

    out: List[Part] = []
    for lo, hi in merged:
        out.append((
            "int",
            None if lo == NEG_INF else lo,
            None if hi == POS_INF else hi - 1,
        ))
    return out


def normalize(parts: List[Part]) -> Domain:
    """把片段列表归一化为互不相交的有序列表。"""
    by_kind: dict[str, List[Part]] = {}
    for p in parts:
        by_kind.setdefault(p[0], []).append(p)

    out: Domain = []
    if by_kind.get("null"):
        out.append(("null",))

    if by_kind.get("int"):
        out.extend(_norm_int(by_kind["int"]))

    str_parts = by_kind.get("str", [])
    if str_parts:
        include: set[str] = set()
        exclude: set[str] = set()
        has_all = False
        for p in str_parts:
            if p[1] == "all":
                has_all = True
            elif p[1] == "set":
                include |= set(p[2])
            else:  # allbut
                has_all = True
                exclude |= set(p[2])
        if has_all:
            include -= exclude
            if include:
                # 有限集合并入全集合后等价于 allbut(exclude - include)
                out.append(("str", "allbut", frozenset(exclude - include)))
            elif exclude:
                out.append(("str", "allbut", frozenset(exclude)))
            else:
                out.append(("str", "all"))
        elif include:
            out.append(("str", "set", frozenset(sorted(include))))

    return out


def intersect(a: Domain, b: Domain) -> Domain:
    """两个域的交集。"""
    out: List[Part] = []
    for pa in a:
        for pb in b:
            ka = pa[0]
            kb = pb[0]
            if ka != kb:
                continue
            if ka == "null":
                out.append(pa)
            elif ka == "int":
                lo = _max_bound(pa[1], pb[1])
                hi = _min_bound(pa[2], pb[2])
                if lo is None or hi is None or lo <= hi:
                    out.append(("int", lo, hi))
            else:  # str
                sa = _str_set(pa)
                sb = _str_set(pb)
                sec = _str_intersect(sa, sb)
                if sec is not None:
                    out.append(("str", sec[0], *sec[1:]))
    return normalize(out)


def difference(a: Domain, b: Domain) -> Domain:
    """域差 a - b。"""
    if not a:
        return []
    if not b:
        return list(a)
    out: List[Part] = []
    for pa in a:
        kind = pa[0]
        b_parts = [p for p in b if p[0] == kind]
        if not b_parts:
            out.append(pa)
            continue
        if kind == "null":
            continue  # null 被 null 完全减去
        if kind == "int":
            for piece in _int_diff_part(pa, b_parts):
                out.append(piece)
        else:
            rem = _str_set(pa)
            for pb in b_parts:
                rem = _str_diff(rem, _str_set(pb))
                if rem is None:
                    break
            if rem is not None:
                out.append(("str", rem[0], *rem[1:]))
    return normalize(out)


def is_empty(d: Domain) -> bool:
    return not d


def is_subset(a: Domain, b: Domain) -> bool:
    return is_empty(difference(a, b))


# --------------------------------------------------------------------------- #
# 具体取值 / 拆分
# --------------------------------------------------------------------------- #
def pick(d: Domain) -> Any:
    """从非空域中取一个确定的具体值（用于生成反例）。"""
    p = d[0]
    if p[0] == "null":
        return None
    if p[0] == "int":
        lo, hi = p[1], p[2]
        if lo is not None:
            return lo
        if hi is not None:
            return hi
        return 0
    s = _str_set(p)
    if s[0] == "set":
        return next(iter(sorted(s[1])))
    if s[0] == "allbut":
        i = 0
        while True:
            cand = f"s{i}"
            if cand not in s[1]:
                return cand
            i += 1
    return "s0"  # all


def partition(d: Domain, sub: Domain) -> List[Domain]:
    """把 d 拆成 [d∩sub, d-sub]，跳过空片段。"""
    return [x for x in (intersect(d, sub), difference(d, sub)) if x]


# --------------------------------------------------------------------------- #
# 从字段 schema 构造取值域
# --------------------------------------------------------------------------- #
def domain_of(prop: dict) -> Domain:
    """把一个字段的 schema（type/enum/minimum/maximum）翻译成取值域。"""
    types = prop["type"]
    if isinstance(types, str):
        types = [types]

    parts: List[Part] = []
    if "null" in types:
        parts.append(("null",))
    if "integer" in types:
        lo = prop.get("minimum")
        hi = prop.get("maximum")
        parts.append(("int", lo, hi))
    if "string" in types:
        parts.append(("str", "all"))

    if "enum" in prop:
        enum_domain = normalize(_enum_domain(prop["enum"]))
        parts = intersect(normalize(parts), enum_domain)
    return normalize(parts)


def _enum_domain(values: List[Any]) -> List[Part]:
    parts: List[Part] = []
    strs: set[str] = set()
    ints: List[int] = []
    has_null = False
    for v in values:
        if v is None:
            has_null = True
        elif isinstance(v, bool):
            continue  # bool 不是合法 integer，schema 校验阶段也会拒绝
        elif isinstance(v, int):
            ints.append(v)
        elif isinstance(v, str):
            strs.add(v)
    if has_null:
        parts.append(("null",))
    parts.extend(("int", i, i) for i in sorted(ints))
    if strs:
        parts.append(("str", "set", frozenset(strs)))
    return parts


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _max_bound(x: Optional[int], y: Optional[int]) -> Optional[int]:
    if x is None:
        return y
    if y is None:
        return x
    return max(x, y)


def _min_bound(x: Optional[int], y: Optional[int]) -> Optional[int]:
    if x is None:
        return y
    if y is None:
        return x
    return min(x, y)


def _int_diff_part(pa: Part, b_parts: List[Part]) -> List[Part]:
    """单个整数区间减去若干整数区间（间隙枚举，正确处理无界）。"""
    lo, hi = pa[1], pa[2]
    # 只保留与本区间相交的切割区间
    cuts: List[Tuple[Optional[int], Optional[int]]] = []
    for pb in b_parts:
        blo, bhi = pb[1], pb[2]
        if blo is not None and hi is not None and blo > hi:
            continue
        if bhi is not None and lo is not None and bhi < lo:
            continue
        cuts.append((blo, bhi))
    cut_norm = _norm_int([("int", x, y) for x, y in cuts])
    cuts = [(p[1], p[2]) for p in cut_norm]

    out: List[Part] = []
    cursor = lo  # 下一个未被覆盖的起点，None 表示负无穷
    tail_bounded = False
    for clo, chi in cuts:
        # clo 左侧的间隙 [cursor, clo-1]
        gap_hi = None if clo is None else clo - 1
        if cursor is None:
            if gap_hi is not None:
                out.append(("int", None, gap_hi))
        elif gap_hi is not None and cursor <= gap_hi:
            out.append(("int", cursor, gap_hi))
        # 推进游标到切割区间右端+1
        if chi is None:
            # 切割器向右无界：本区间右侧全部被覆盖（若本区间也有上界，
            # 到上界为止；总之不会再有右侧间隙）
            cursor = None
            tail_bounded = hi is not None
            break
        cursor = chi + 1
        if hi is not None and cursor > hi:
            return _norm_int(out)
    # 收尾间隙
    if cursor is None:
        # 仅当本区间有有限上界、且没有向右无界的切割器时，才可能有右侧残留
        if hi is not None and not tail_bounded:
            out.append(("int", None, hi))
        # hi 也是 None 时已被完全减去，无残留
    elif hi is None or cursor <= hi:
        out.append(("int", cursor, hi))
    return _norm_int(out)


# 字符串集合统一表示为 ("set", fs) / ("allbut", fs) / ("all",)
def _str_set(p: Part) -> Part:
    if p[1] == "all":
        return ("all",)
    return (p[1], p[2])


def _str_intersect(a: Part, b: Part) -> Optional[Part]:
    if a[0] == "all":
        return b
    if b[0] == "all":
        return a
    if a[0] == "set" and b[0] == "set":
        sec = a[1] & b[1]
        return ("set", frozenset(sec)) if sec else None
    if a[0] == "allbut" and b[0] == "allbut":
        return ("allbut", a[1] | b[1])
    set_p, but_p = (a, b) if a[0] == "set" else (b, a)
    sec = set_p[1] - but_p[1]
    return ("set", frozenset(sec)) if sec else None


def _str_diff(a: Part, b: Part) -> Optional[Part]:
    """返回 a-b；None 表示结果为空。"""
    if a[0] == "all":
        if b[0] == "all":
            return None
        if b[0] == "allbut":
            return ("set", frozenset(b[1])) if b[1] else None
        return ("allbut", frozenset(b[1]))  # all - set
    if a[0] == "set":
        if b[0] == "all":
            return None
        if b[0] == "allbut":
            rem = a[1] & b[1]
            return ("set", frozenset(rem)) if rem else None
        rem = a[1] - b[1]
        return ("set", frozenset(rem)) if rem else None
    # a == allbut
    if b[0] == "all":
        return None
    if b[0] == "allbut":
        # (U-A) - (U-B) = B - A
        rem = b[1] - a[1]
        return ("set", frozenset(rem)) if rem else None
    # (U-A) - B = U - (A ∪ B)
    return ("allbut", frozenset(a[1] | b[1]))
