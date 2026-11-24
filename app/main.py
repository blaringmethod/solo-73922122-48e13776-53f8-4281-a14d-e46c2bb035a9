"""FastAPI 路由与请求模型。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from math import floor
from typing import Annotated, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from . import db, intervals, timing

Side = Literal["collector", "library"]

# SQLite 以 64 位有符号整数存储帧号，超出范围在入参层直接 422，避免入库失败
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
FrameInt = Annotated[int, Field(ge=INT64_MIN, le=INT64_MAX)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="质检影像缺帧核对 API",
    description=(
        "登记每批影像应有的帧号范围；采集端与影像库各凭清单号 "
        "登记/更正/撤回连续号段；按批核对四类最大连续段，按帧追查两侧清单；"
        "按班次汇总补档结清帧次、超时帧次与累计欠档帧秒。"
        "全程以号段为单位存储与计算，不逐帧展开。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


# ---------- 请求 / 响应模型 ----------

class BatchIn(BaseModel):
    batch_id: str = Field(..., min_length=1, description="批次号")
    frame_start: FrameInt = Field(..., description="应有帧号范围起点（含）")
    frame_end: FrameInt = Field(..., description="应有帧号范围终点（含）")

    @field_validator("frame_end")
    @classmethod
    def _check_range(cls, v, info):
        start = info.data.get("frame_start")
        if start is not None and v < start:
            raise ValueError("frame_end 不能小于 frame_start")
        return v


class ManifestIn(BaseModel):
    manifest_no: str = Field(..., min_length=1, description="清单号，全局唯一")
    batch_id: str = Field(..., min_length=1)
    side: Side
    seg_start: FrameInt = Field(..., description="号段起点（含，闭区间）")
    seg_end: FrameInt = Field(..., description="号段终点（含，闭区间）")

    @field_validator("seg_end")
    @classmethod
    def _check_range(cls, v, info):
        start = info.data.get("seg_start")
        if start is not None and v < start:
            raise ValueError("seg_end 不能小于 seg_start")
        return v


class CorrectionIn(BaseModel):
    seg_start: FrameInt
    seg_end: FrameInt

    @field_validator("seg_end")
    @classmethod
    def _check_range(cls, v, info):
        start = info.data.get("seg_start")
        if start is not None and v < start:
            raise ValueError("seg_end 不能小于 seg_start")
        return v


class WithdrawIn(BaseModel):
    manifest_no: str = Field(..., min_length=1)


class ShiftStat(BaseModel):
    settled_frames: int = Field(..., description="该侧最后补齐的结清帧次")
    timeout_frames: int = Field(..., description="其中欠档秒数超过阈值的帧次")
    owed_frame_seconds: int = Field(..., description="累计欠档帧秒（帧数 × 欠档秒数）")


class ShiftWindow(BaseModel):
    start: str
    end: str
    start_epoch: int
    end_epoch: int
    end_exclusive: bool = True


class ShiftSummaryOut(BaseModel):
    batch_id: str
    window: ShiftWindow
    timeout_seconds: int
    by_last_filled_side: dict[Side, ShiftStat] = Field(
        ..., description="键为最后补齐的一侧：collector=影像库先齐、采集端最后补齐"
    )
    total: ShiftStat


def _row_to_manifest(row) -> dict:
    return {
        "manifest_no": row["manifest_no"],
        "batch_id": row["batch_id"],
        "side": row["side"],
        "status": row["status"],
        "seg_start": row["seg_start"],
        "seg_end": row["seg_end"],
        "updated_at": row["updated_at"],
    }


# ---------- 健康检查 ----------

@app.get("/health", tags=["运维"])
def health():
    return {"status": "ok"}


# ---------- 批次 ----------

@app.post("/batches", status_code=status.HTTP_201_CREATED, tags=["批次"])
def register_batch(payload: BatchIn, conn=Depends(get_conn)):
    """登记一批影像应有的帧号范围（首尾包含）。"""
    with db.transaction(conn):
        if db.get_batch(conn, payload.batch_id) is not None:
            raise HTTPException(409, f"批次已存在: {payload.batch_id}")
        db.create_batch(conn, payload.batch_id, payload.frame_start, payload.frame_end)
    return {
        "batch_id": payload.batch_id,
        "frame_start": payload.frame_start,
        "frame_end": payload.frame_end,
    }


@app.get("/batches/{batch_id}", tags=["批次"])
def get_batch(batch_id: str, conn=Depends(get_conn)):
    row = db.get_batch(conn, batch_id)
    if row is None:
        raise HTTPException(404, f"批次不存在: {batch_id}")
    return dict(row)


# ---------- 清单：登记 / 更正 / 撤回 ----------

def _validate_in_batch(conn, batch_id: str, start: int, end: int):
    batch = db.get_batch(conn, batch_id)
    if batch is None:
        raise HTTPException(404, f"批次不存在: {batch_id}")
    if start < batch["frame_start"] or end > batch["frame_end"]:
        raise HTTPException(
            422,
            f"号段 [{start}, {end}] 越界，批次范围为 "
            f"[{batch['frame_start']}, {batch['frame_end']}]（首尾包含）",
        )


@app.post("/manifests", status_code=status.HTTP_201_CREATED, tags=["清单"])
def register_manifest(payload: ManifestIn, conn=Depends(get_conn)):
    """采集端或影像库凭清单号登记一个连续号段。"""
    with db.transaction(conn):
        _validate_in_batch(conn, payload.batch_id, payload.seg_start, payload.seg_end)
        if db.get_manifest(conn, payload.manifest_no) is not None:
            raise HTTPException(409, f"清单号已存在: {payload.manifest_no}")

        region = (payload.seg_start, payload.seg_end)
        other = "library" if payload.side == "collector" else "collector"
        old_side_cov = timing.coverage_in(
            conn, payload.batch_id, payload.side, *region
        )
        other_cov = timing.coverage_in(conn, payload.batch_id, other, *region)

        db.insert_manifest(
            conn,
            payload.manifest_no,
            payload.batch_id,
            payload.side,
            payload.seg_start,
            payload.seg_end,
        )
        # 只刷新该号段所在的局部区间
        db.refresh_region(
            conn, payload.batch_id, payload.side, payload.seg_start, payload.seg_end
        )
        new_side_cov = timing.coverage_in(conn, payload.batch_id, payload.side, *region)
        # 计时账与清单、覆盖同事务提交
        timing.apply_coverage_transition(
            conn,
            payload.batch_id,
            payload.side,
            region,
            old_side_cov,
            new_side_cov,
            other_cov,
        )
        row = db.get_manifest(conn, payload.manifest_no)
    return _row_to_manifest(row)


@app.put("/manifests/{manifest_no}", tags=["清单"])
def correct_manifest(manifest_no: str, payload: CorrectionIn, conn=Depends(get_conn)):
    """更正清单号段：只更新旧号段与新号段涉及的区间，不重扫整批。"""
    with db.transaction(conn):
        row = db.get_manifest(conn, manifest_no)
        if row is None:
            raise HTTPException(404, f"清单不存在: {manifest_no}")
        if row["status"] == "withdrawn":
            raise HTTPException(409, f"清单已撤回，不能更正: {manifest_no}")
        _validate_in_batch(conn, row["batch_id"], payload.seg_start, payload.seg_end)

        side = row["side"]
        other = "library" if side == "collector" else "collector"
        region = intervals.merge(
            [(row["seg_start"], row["seg_end"]), (payload.seg_start, payload.seg_end)]
        )
        # 以提交前后的状态为准：先取旧覆盖与另一侧覆盖，再改清单、重建覆盖
        # 一笔更正内的多段共用同一入库整秒，口径一致
        changed_at = timing.now_epoch()
        before = [
            (
                timing.coverage_in(conn, row["batch_id"], side, s, e),
                timing.coverage_in(conn, row["batch_id"], other, s, e),
            )
            for s, e in region
        ]
        db.update_manifest_segment(conn, manifest_no, payload.seg_start, payload.seg_end)
        for start, end in region:
            db.refresh_region(conn, row["batch_id"], side, start, end)
        for (start, end), (old_side_cov, other_cov) in zip(region, before):
            new_side_cov = timing.coverage_in(conn, row["batch_id"], side, start, end)
            timing.apply_coverage_transition(
                conn,
                row["batch_id"],
                side,
                (start, end),
                old_side_cov,
                new_side_cov,
                other_cov,
                at=changed_at,
            )
        row = db.get_manifest(conn, manifest_no)
    return _row_to_manifest(row)


@app.post("/manifests/withdraw", tags=["清单"])
def withdraw_manifest(payload: WithdrawIn, conn=Depends(get_conn)):
    """撤回清单：同侧其他清单覆盖的帧仍算有记录。幂等冲突显式报错。"""
    with db.transaction(conn):
        row = db.get_manifest(conn, payload.manifest_no)
        if row is None:
            raise HTTPException(404, f"清单不存在: {payload.manifest_no}")
        if row["status"] == "withdrawn":
            raise HTTPException(409, f"清单已撤回: {payload.manifest_no}")

        side = row["side"]
        other = "library" if side == "collector" else "collector"
        region = (row["seg_start"], row["seg_end"])
        old_side_cov = timing.coverage_in(conn, row["batch_id"], side, *region)
        other_cov = timing.coverage_in(conn, row["batch_id"], other, *region)

        db.mark_manifest_withdrawn(conn, payload.manifest_no)
        # 仅在原号段区间内重算覆盖
        db.refresh_region(conn, row["batch_id"], side, *region)
        new_side_cov = timing.coverage_in(conn, row["batch_id"], side, *region)
        timing.apply_coverage_transition(
            conn,
            row["batch_id"],
            side,
            region,
            old_side_cov,
            new_side_cov,
            other_cov,
        )
        row = db.get_manifest(conn, payload.manifest_no)
    return _row_to_manifest(row)


@app.get("/manifests/{manifest_no}", tags=["清单"])
def get_manifest(manifest_no: str, conn=Depends(get_conn)):
    row = db.get_manifest(conn, manifest_no)
    if row is None:
        raise HTTPException(404, f"清单不存在: {manifest_no}")
    return _row_to_manifest(row)


@app.get("/batches/{batch_id}/manifests", tags=["清单"])
def list_manifests(
    batch_id: str,
    side: Optional[Side] = None,
    conn=Depends(get_conn),
):
    """查看批次下的清单（演示/排查用）。"""
    if db.get_batch(conn, batch_id) is None:
        raise HTTPException(404, f"批次不存在: {batch_id}")
    sql = "SELECT * FROM manifests WHERE batch_id=?"
    params: list = [batch_id]
    if side is not None:
        sql += " AND side=?"
        params.append(side)
    sql += " ORDER BY side, seg_start"
    return [_row_to_manifest(r) for r in conn.execute(sql, params).fetchall()]


# ---------- 核对 ----------

@app.get("/batches/{batch_id}/reconcile", tags=["核对"])
def reconcile_batch(batch_id: str, conn=Depends(get_conn)):
    """按批返回四类最大连续段：双方齐全 / 仅采集端 / 仅影像库 / 双方皆缺。"""
    try:
        return db.reconcile(conn, batch_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))


def _epoch_from_iso(value: str, field: str) -> int:
    """把 ISO 8601 UTC 时刻（如 2026-11-24T08:00:00Z）解析为 Unix 秒。"""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(422, f"{field} 不是合法的 ISO 8601 时刻: {value}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return floor(parsed.astimezone(timezone.utc).timestamp())


@app.get(
    "/batches/{batch_id}/shift-summary",
    tags=["补档时效"],
    response_model=ShiftSummaryOut,
)
def shift_summary(
    batch_id: str,
    start: str = Query(..., description="班次起点，UTC ISO 8601，左闭，如 2026-11-24T08:00:00Z"),
    end: str = Query(..., description="班次终点，UTC ISO 8601，右开，如 2026-11-24T16:00:00Z"),
    timeout_seconds: int = Query(..., ge=0, description="超时秒数；欠档严格大于该值才算超时"),
    conn=Depends(get_conn),
):
    """按最后补齐的一侧汇总结清帧次、超时帧次和累计欠档帧秒。

    结清按服务端记录的入库 UTC 整秒归入 ``[start, end)``；帧秒等于
    各结清帧欠档秒数之和（帧数 × 该号段欠档秒数）。只查计时账，
    不重放清单历史。
    """
    try:
        start_epoch = _epoch_from_iso(start, "start")
        end_epoch = _epoch_from_iso(end, "end")
        if end_epoch < start_epoch:
            raise HTTPException(422, "end 不能早于 start")
        result = timing.shift_summary(conn, batch_id, start_epoch, end_epoch, timeout_seconds)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return {
        "batch_id": batch_id,
        "window": {"start": start, "end": end, "start_epoch": start_epoch,
                   "end_epoch": end_epoch, "end_exclusive": True},
        "timeout_seconds": timeout_seconds,
        **result,
    }


@app.get("/batches/{batch_id}/frames/{frame_no}", tags=["核对"])
def trace_frame(batch_id: str, frame_no: int, conn=Depends(get_conn)):
    """按帧追查两侧的清单号及该帧的核对状态。"""
    if not (INT64_MIN <= frame_no <= INT64_MAX):
        raise HTTPException(422, f"帧号超出 64 位整数范围: {frame_no}")
    try:
        return db.trace_frame(conn, batch_id, frame_no)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
