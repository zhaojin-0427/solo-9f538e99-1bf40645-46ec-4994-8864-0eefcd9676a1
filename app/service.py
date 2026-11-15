"""换版编排：结构校验 → 精确兼容性判定 → 事务提交/回滚。"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional, Tuple

from . import compat, database
from .schema_limits import SchemaError, event_is_valid, validate_contract_schema

# 结果常量
ACTIVATED = "activated"
REJECTED = "rejected_incompatible"
CONFLICT = "conflict"
INVALID = "invalid_schema"


class ConflictError(Exception):
    def __init__(self, current_revision: int):
        self.current_revision = current_revision
        super().__init__(f"修订冲突：当前修订已为 {current_revision}")


class _Txn:
    """可重入事务：最外层负责 BEGIN/COMMIT，内层复用同一事务。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.owned = not conn.in_transaction

    def __enter__(self) -> "_Txn":
        if self.owned:
            self.conn.execute("BEGIN IMMEDIATE")
        return self

    def commit(self) -> None:
        if self.owned and self.conn.in_transaction:
            self.conn.execute("COMMIT")

    def rollback(self) -> None:
        if self.owned and self.conn.in_transaction:
            self.conn.execute("ROLLBACK")

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.owned or not self.conn.in_transaction:
            return
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")


def evaluate_candidate(
    conn: sqlite3.Connection,
    endpoint_id: str,
    candidate: dict,
) -> Tuple[str, Dict[str, Any]]:
    """不写库的完整判定，返回 (result, payload)。

    result 为 activated / rejected_incompatible / conflict / invalid_schema。
    """
    endpoint = database.get_endpoint(conn, endpoint_id)
    if endpoint is None:
        raise KeyError(endpoint_id)

    # 1. 结构子集校验
    try:
        validate_contract_schema(candidate)
    except SchemaError as exc:
        return INVALID, {"reason": str(exc)}

    role = endpoint["role"]
    # 2. 兼容性判定（对端所有已发布合同，都要满足包含关系）
    if role == "producer":
        cleaners = database.published_schemas(conn, "cleaner")
        for cleaner in cleaners:
            ce = compat.find_counterexample(candidate, cleaner["schema"])
            if ce is not None:
                _assert_counterexample(candidate, cleaner["schema"], ce)
                return REJECTED, {
                    "reason": "候选生产合同存在清洗器拒收的合法事件",
                    "counterexample": ce,
                    "rejected_cleaner_id": cleaner["endpoint_id"],
                }
    else:
        producers = database.published_schemas(conn, "producer")
        for producer in producers:
            ce = compat.find_counterexample(producer["schema"], candidate)
            if ce is not None:
                _assert_counterexample(producer["schema"], candidate, ce)
                return REJECTED, {
                    "reason": "候选清洗器合同无法接受生产端的部分合法事件",
                    "counterexample": ce,
                    "rejected_cleaner_id": endpoint_id,
                }
    return ACTIVATED, {"reason": "候选合同通过全部兼容性判定"}


def _assert_counterexample(producer_schema: dict, cleaner_schema: dict, event: dict) -> None:
    """引擎自检：反例必须确实是「生产者合法、清洗器拒收」，防止引擎静默出错。"""
    p_ok, c_ok = compat.check_producer_event(event, producer_schema, cleaner_schema)
    if not (p_ok and not c_ok):
        raise RuntimeError(
            f"兼容性引擎内部错误：反例 {event!r} 不满足生产者合法且清洗器拒收 "
            f"(producer_ok={p_ok}, cleaner_ok={c_ok})"
        )


def publish(
    conn: sqlite3.Connection,
    endpoint_id: str,
    candidate: dict,
    expected_revision: Optional[int],
) -> Dict[str, Any]:
    """在一个事务内完成换版。失败抛异常，由路由层转 HTTP。"""
    endpoint = database.get_endpoint(conn, endpoint_id)
    if endpoint is None:
        raise KeyError(endpoint_id)
    role = endpoint["role"]
    current_revision = endpoint["revision"]

    with _Txn(conn) as txn:
        # 拿到写锁后重新读取当前修订：并发下可能已被其他事务推进。
        # 带 expected_revision 时，若与当前修订不符则报冲突；
        # 不带时按提交时的当前合同继续重判（满足「按当前合同重判」语义）。
        locked = database.get_endpoint(conn, endpoint_id)
        latest_revision = locked["revision"]
        if expected_revision is not None and expected_revision != latest_revision:
            database.insert_attempt(
                conn, endpoint_id, role, expected_revision, candidate,
                CONFLICT, f"期望修订 {expected_revision}，当前修订 {latest_revision}",
            )
            txn.commit()
            raise ConflictError(latest_revision)
        current_revision = latest_revision

        # 结构子集校验：不合法记录后抛 SchemaError，由路由层转 HTTP 400
        try:
            validate_contract_schema(candidate)
        except SchemaError as exc:
            database.insert_attempt(
                conn, endpoint_id, role, expected_revision, candidate,
                INVALID, str(exc),
            )
            txn.commit()
            raise

        # 兼容性判定（对端所有已发布合同，都要满足包含关系）
        result, payload = evaluate_candidate(conn, endpoint_id, candidate)

        if result == REJECTED:
            database.insert_attempt(
                conn, endpoint_id, role, expected_revision, candidate,
                REJECTED, payload["reason"],
                counterexample=payload["counterexample"],
                rejected_cleaner_id=payload["rejected_cleaner_id"],
            )
            txn.commit()
            return {
                "result": REJECTED,
                "detail": payload["reason"],
                "counterexample": payload["counterexample"],
                "rejected_cleaner_id": payload["rejected_cleaner_id"],
                "current_revision": current_revision,
            }

        # 通过：启用候选并递增该端修订（写锁内修订号已刷新，不会撞唯一约束）
        new_revision = current_revision + 1
        database.insert_contract(conn, endpoint_id, new_revision, candidate)
        database.bump_revision(conn, endpoint_id, new_revision)
        database.insert_attempt(
            conn, endpoint_id, role, expected_revision, candidate,
            ACTIVATED, f"候选合同已启用，修订 {current_revision} -> {new_revision}",
        )
        return {
            "result": ACTIVATED,
            "detail": payload["reason"],
            "previous_revision": current_revision,
            "new_revision": new_revision,
        }


def register_endpoint(
    conn: sqlite3.Connection,
    endpoint_id: str,
    role: str,
    display_name: str,
    initial_schema: Optional[dict],
) -> Dict[str, Any]:
    """登记新端（生产端至多 1 个、清洗器至多 3 个），可选携带首版合同。"""
    if role not in ("producer", "cleaner"):
        raise ValueError("role 只能是 producer 或 cleaner")
    existing = database.list_endpoints(conn)
    count = sum(1 for e in existing if e["role"] == role)
    limit = database.MAX_PRODUCERS if role == "producer" else database.MAX_CLEANERS
    if count >= limit:
        raise ValueError(f"{role} 端数量已达上限 {limit}")
    if any(e["endpoint_id"] == endpoint_id for e in existing):
        raise ValueError(f"endpoint_id {endpoint_id!r} 已存在")

    if initial_schema is not None:
        validate_contract_schema(initial_schema)

    with _Txn(conn) as txn:
        database.insert_endpoint(conn, endpoint_id, role, display_name)
        response: Dict[str, Any] = {
            "endpoint_id": endpoint_id,
            "role": role,
            "display_name": display_name,
            "revision": 0,
        }
        if initial_schema is not None:
            # 首版同样要与对端已发布合同判定
            result, payload = evaluate_candidate(conn, endpoint_id, initial_schema)
            if result == REJECTED:
                database.insert_attempt(
                    conn, endpoint_id, role, None, initial_schema,
                    REJECTED, payload["reason"],
                    counterexample=payload["counterexample"],
                    rejected_cleaner_id=payload["rejected_cleaner_id"],
                )
                txn.commit()
                return {**response, "initial_contract": "rejected", **payload}
            database.insert_contract(conn, endpoint_id, 1, initial_schema)
            database.bump_revision(conn, endpoint_id, 1)
            database.insert_attempt(
                conn, endpoint_id, role, None, initial_schema,
                ACTIVATED, "首版合同已启用，修订 0 -> 1",
            )
            response["revision"] = 1
            response["initial_contract"] = "activated"
        return response


def validate_event(conn: sqlite3.Connection, endpoint_id: str, event: Any) -> Dict[str, Any]:
    endpoint = database.get_endpoint(conn, endpoint_id)
    if endpoint is None:
        raise KeyError(endpoint_id)
    contract = database.get_current_contract(conn, endpoint_id)
    if contract is None:
        return {"endpoint_id": endpoint_id, "accepted": False,
                "reason": "该端尚未发布任何合同修订"}
    accepted = event_is_valid(event, contract["schema"])
    return {
        "endpoint_id": endpoint_id,
        "revision": contract["revision"],
        "accepted": accepted,
        "reason": "命中 anyOf 分支" if accepted else "未命中任何分支（类型/枚举/区间/额外字段/必填字段不符）",
    }
