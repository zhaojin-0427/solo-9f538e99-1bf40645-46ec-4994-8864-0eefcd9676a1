"""FastAPI 入口：合同登记、换版、反例查询。

启动后访问 /docs 可直接在 Swagger UI 中登记端、提交候选合同换版，
并在 /api/change-attempts 查看每次换版的反例。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import database, samples, service
from .schema_limits import SchemaError, validate_contract_schema


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动播种：幂等，已存在端则跳过
    with database.get_conn() as conn:
        if not database.list_endpoints(conn):
            for endpoint_id, role, name, schema in samples.SEED_ENDPOINTS:
                service.register_endpoint(conn, endpoint_id, role, name, schema)
            # 尝试一次不兼容换版，方便 /docs 首次打开就能看到反例
            try:
                service.publish(
                    conn, "producer-main", samples.PRODUCER_V2_INCOMPATIBLE, None
                )
            except Exception:  # noqa: BLE001 - 播种失败不应阻止启动
                pass
    yield


app = FastAPI(
    title="点击事件合同登记/换版服务",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "生产端与至多 3 个清洗器的 JSON Schema 2020-12 平面对象合同管理。"
        "换版采用**精确**兼容性判定（非抽样）：生产合同允许的每个对象都必须被"
        "所有清洗器接受；不兼容时返回一条合法生产事件及拒收清洗器，候选不启用。"
    ),
)


# --------------------------------------------------------------------------- #
# 请求/响应模型
# --------------------------------------------------------------------------- #
class RegisterRequest(BaseModel):
    endpoint_id: str = Field(..., pattern=r"^[A-Za-z0-9_\-]{1,64}$", description="端唯一标识")
    role: str = Field(..., description="producer 或 cleaner")
    display_name: str = Field(..., min_length=1, max_length=128)
    initial_schema: Optional[Dict[str, Any]] = Field(
        None, description="可选的首版合同；提供时同样执行完整兼容性判定"
    )


class PublishRequest(BaseModel):
    candidate_schema: Dict[str, Any] = Field(..., description="候选合同（JSON Schema 2020-12 平面对象子集）")
    expected_revision: Optional[int] = Field(
        None,
        ge=0,
        description="乐观锁：提交时该端当前修订必须等于此值；不带则按当前合同直接重判",
    )


class ValidateRequest(BaseModel):
    event: Any


class DryRunRequest(BaseModel):
    candidate_schema: Dict[str, Any] = Field(..., description="待试判定的候选合同（不写库、不改修订）")


# --------------------------------------------------------------------------- #
# 依赖
# --------------------------------------------------------------------------- #
def conn_dep():
    with database.get_conn() as conn:
        yield conn


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@app.get("/", tags=["meta"])
def root() -> Dict[str, str]:
    return {"service": "click-event-contract-registry", "docs": "/docs", "health": "/api/health"}


@app.get("/api/health", tags=["meta"])
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/endpoints", tags=["endpoints"], status_code=201)
def register(req: RegisterRequest, conn=Depends(conn_dep)) -> Dict[str, Any]:
    try:
        return service.register_endpoint(
            conn, req.endpoint_id, req.role, req.display_name, req.initial_schema
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/endpoints", tags=["endpoints"])
def endpoints(conn=Depends(conn_dep)) -> List[Dict[str, Any]]:
    return database.list_endpoints(conn)


@app.get("/api/endpoints/{endpoint_id}", tags=["endpoints"])
def endpoint_detail(endpoint_id: str, conn=Depends(conn_dep)) -> Dict[str, Any]:
    ep = database.get_endpoint(conn, endpoint_id)
    if ep is None:
        raise HTTPException(status_code=404, detail=f"端 {endpoint_id!r} 不存在")
    contract = database.get_current_contract(conn, endpoint_id)
    ep["current_contract"] = contract
    return ep


@app.get("/api/endpoints/{endpoint_id}/revisions", tags=["endpoints"])
def revisions(endpoint_id: str, conn=Depends(conn_dep)) -> List[Dict[str, Any]]:
    if database.get_endpoint(conn, endpoint_id) is None:
        raise HTTPException(status_code=404, detail=f"端 {endpoint_id!r} 不存在")
    return database.list_revisions(conn, endpoint_id)


@app.get("/api/endpoints/{endpoint_id}/revisions/{revision}", tags=["endpoints"])
def revision_detail(endpoint_id: str, revision: int, conn=Depends(conn_dep)) -> Dict[str, Any]:
    data = database.get_revision(conn, endpoint_id, revision)
    if data is None:
        raise HTTPException(status_code=404, detail="修订不存在")
    return data


@app.put("/api/endpoints/{endpoint_id}/contract", tags=["contracts"])
def publish_contract(endpoint_id: str, req: PublishRequest, conn=Depends(conn_dep)) -> Dict[str, Any]:
    """提交候选合同换版。

    * 通过 → 启用候选，该端 revision +1；
    * 不兼容 → 200 返回 result=rejected_incompatible，附合法生产事件反例与
      拒收清洗器，候选不启用，原发布保留；
    * 修订冲突 → 409，原发布保留；
    * 结构不合法 → 400。
    """
    if database.get_endpoint(conn, endpoint_id) is None:
        raise HTTPException(status_code=404, detail=f"端 {endpoint_id!r} 不存在")
    try:
        return service.publish(conn, endpoint_id, req.candidate_schema, req.expected_revision)
    except service.ConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": str(exc), "current_revision": exc.current_revision},
        )
    except SchemaError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/endpoints/{endpoint_id}/validate-event", tags=["contracts"])
def validate_event(endpoint_id: str, req: ValidateRequest, conn=Depends(conn_dep)) -> Dict[str, Any]:
    if database.get_endpoint(conn, endpoint_id) is None:
        raise HTTPException(status_code=404, detail=f"端 {endpoint_id!r} 不存在")
    return service.validate_event(conn, endpoint_id, req.event)


@app.post("/api/endpoints/{endpoint_id}/compatibility-check", tags=["contracts"])
def compatibility_check(endpoint_id: str, req: DryRunRequest, conn=Depends(conn_dep)) -> Dict[str, Any]:
    """无副作用试判定：结构校验 + 完整兼容性判定，不写库、不改修订。"""
    if database.get_endpoint(conn, endpoint_id) is None:
        raise HTTPException(status_code=404, detail=f"端 {endpoint_id!r} 不存在")
    try:
        result, payload = service.evaluate_candidate(conn, endpoint_id, req.candidate_schema)
    except SchemaError as exc:
        return {"result": service.INVALID, "detail": str(exc)}
    return {"endpoint_id": endpoint_id, "result": result, **payload}


@app.post("/api/schema/validate", tags=["contracts"])
def schema_validate(req: PublishRequest) -> Dict[str, Any]:
    """仅校验候选合同是否符合允许的 JSON Schema 子集。"""
    try:
        validate_contract_schema(req.candidate_schema)
    except SchemaError as exc:
        return {"valid": False, "reason": str(exc)}
    return {"valid": True, "reason": "符合 JSON Schema 2020-12 平面对象子集约束"}


@app.get("/api/change-attempts", tags=["audit"])
def change_attempts(limit: int = Query(50, ge=1, le=500), conn=Depends(conn_dep)) -> List[Dict[str, Any]]:
    """换版尝试记录：含每次拒绝的反例事件与拒收清洗器，供 /docs 查看。"""
    return database.list_attempts(conn, limit)
