"""取值域代数测试。"""

from app import domains as D


def d(*parts):
    return D.normalize(list(parts))


def test_empty_domain():
    assert D.is_empty([])
    assert D.is_empty(D.intersect(d(("null",)), d(("int", 0, 5))))


def test_null_intersect_and_diff():
    a = d(("null",), ("int", 0, 10))
    b = d(("null",))
    assert D.is_subset(b, a)
    assert D.difference(b, a) == []
    assert D.difference(a, b) == [("int", 0, 10)]


def test_int_interval_merge():
    a = d(("int", 0, 5), ("int", 6, 10), ("int", 12, 12))
    assert a == [("int", 0, 10), ("int", 12, 12)]


def test_int_diff_cuts():
    a = d(("int", 0, 100))
    b = d(("int", 10, 20), ("int", 50, 50))
    rem = D.difference(a, b)
    assert rem == [("int", 0, 9), ("int", 21, 49), ("int", 51, 100)]
    # 差集 + 被减域 = 原域
    assert D.normalize(rem + list(b)) == a


def test_int_unbounded():
    a = d(("int", None, None))
    b = d(("int", 0, 5))
    assert D.is_subset(b, a)
    assert D.difference(a, b) == [("int", None, -1), ("int", 6, None)]
    assert D.difference(b, a) == []


def test_string_sets():
    all_s = d(("str", "all"))
    finite = d(("str", "set", frozenset({"a", "b"})))
    assert D.is_subset(finite, all_s)
    assert D.difference(all_s, finite) == [("str", "allbut", frozenset({"a", "b"}))]
    rem = D.difference(all_s, d(("str", "allbut", frozenset({"a", "b"}))))
    assert rem == [("str", "set", frozenset({"a", "b"}))]


def test_string_set_operations():
    a = d(("str", "set", frozenset({"a", "b", "c"})))
    b = d(("str", "set", frozenset({"b", "c", "d"})))
    assert D.intersect(a, b) == [("str", "set", frozenset({"b", "c"}))]
    assert D.difference(a, b) == [("str", "set", frozenset({"a"}))]
    # allbut 与 set
    ab = d(("str", "allbut", frozenset({"x"})))
    assert D.intersect(ab, d(("str", "set", frozenset({"x", "y"})))) == [
        ("str", "set", frozenset({"y"}))
    ]


def test_pick_is_member():
    for dom in [
        d(("null",)),
        d(("int", 5, 9)),
        d(("int", None, 3)),
        d(("str", "set", frozenset({"z"}))),
        d(("str", "allbut", frozenset({"s0"}))),
        d(("null",), ("str", "all")),
    ]:
        v = D.pick(dom)
        assert not D.is_empty(D.intersect(dom, D._enum_domain([v]) if v is not None else [("null",)])) or v is None


def test_domain_of_with_enum():
    prop = {"type": ["integer", "null"], "enum": [1, 2, None], "minimum": 0, "maximum": 10}
    dom = D.domain_of(prop)
    assert D.is_subset(dom, d(("null",), ("int", 0, 10)))
    assert D.pick(dom) in (1, 2, None)


def test_enum_filtered_by_range():
    prop = {"type": "integer", "enum": [1, 2, 3], "minimum": 10}
    assert D.is_empty(D.domain_of(prop))
