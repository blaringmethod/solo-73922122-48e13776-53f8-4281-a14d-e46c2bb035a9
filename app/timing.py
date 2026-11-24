"""补档时效账：按号段记录每一轮缺档的起点与结清事件。

口径：
- 一帧只要不是「双方齐全」，就处于某一轮缺档中，本轮起点为
  它上一次变为缺档（或批次登记 / 功能启用）的 UTC 整秒。
- 帧从缺档变为双方齐全时结清一次：欠档秒数 = 结清时刻 - 本轮起点，
  并记录「最后补齐的一侧」。
- 双方齐全后再次缺档，另起一轮；已结清的账保留。
- 一轮缺档中无论缺的是哪一侧、是否两侧同时缺，都沿用同一轮起点。

所有账目均以闭区间号段存储，帧数用 ``end - start + 1`` 计算，
不逐帧展开，因此批次可跨十亿帧。
"""

from __future__ import annotations

import time
from typing import List, Optional, Sequence, Tuple

from . import intervals

Segment = Tuple[int, int]
SIDES = ("collector", "library")


def now_epoch() -> int:
    """服务端当前 UTC 整秒。测试可替换此函数控制时钟。"""
    return int(time.time())


# ---------- 本地号段读取 ----------

def _coverage_all(conn, batch_id: str, side: str) -> List[Segment]:
    rows = conn.execute(
        "SELECT seg_start, seg_end FROM coverage "
        "WHERE batch_id=? AND side=? ORDER BY seg_start",
        (batch_id, side),
    ).fetchall()
    return intervals.merge([(r["seg_start"], r["seg_end"]) for r in rows])


def coverage_in(
    conn, batch_id: str, side: str, lo: int, hi: int
) -> List[Segment]:
    """取某侧在 [lo, hi] 内的覆盖段（已合并、已裁剪）。"""
    rows = conn.execute(
        "SELECT seg_start, seg_end FROM coverage "
        "WHERE batch_id=? AND side=? AND seg_start <= ? AND seg_end >= ? "
        "ORDER BY seg_start",
        (batch_id, side, hi, lo),
    ).fetchall()
    pieces = [(max(r["seg_start"], lo), min(r["seg_end"], hi)) for r in rows]
    return intervals.merge(pieces)


def _open_in(conn, batch_id: str, lo: int, hi: int) -> List:
    return conn.execute(
        "SELECT id, seg_start, seg_end, round_start FROM open_rounds "
        "WHERE batch_id=? AND seg_start <= ? AND seg_end >= ? ORDER BY seg_start",
        (batch_id, hi, lo),
    ).fetchall()


def _open_coverage_in(conn, batch_id: str, lo: int, hi: int) -> List[Segment]:
    rows = _open_in(conn, batch_id, lo, hi)
    pieces = [(max(r["seg_start"], lo), min(r["seg_end"], hi)) for r in rows]
    return intervals.merge(pieces)


# ---------- 写入 ----------

def _insert_open(conn, batch_id: str, seg: Segment, at: int) -> None:
    conn.execute(
        "INSERT INTO open_rounds (batch_id, seg_start, seg_end, round_start) "
        "VALUES (?, ?, ?, ?)",
        (batch_id, seg[0], seg[1], at),
    )


def open_batch(conn, batch_id: str, start: int, end: int, at: Optional[int] = None) -> None:
    """新批次登记：整批从登记时刻开始计时（此时必然还没有清单）。"""
    _insert_open(conn, batch_id, (start, end), now_epoch() if at is None else at)


def ensure_feature_enabled(conn) -> None:
    """首次启用补档账：给旧批次的未齐帧从启用时刻起计时。

    只在首次启动时执行一次并写入 meta；之后重启只沿用持久化的轮次，
    不会重置计时。并发启动时由 ``INSERT OR IGNORE`` 抢标记，只有一个
    进程会真正起账（调用方需在 BEGIN IMMEDIATE 事务内）。
    """
    at = now_epoch()
    cur = conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('timing_enabled_at', ?)",
        (str(at),),
    )
    if cur.rowcount == 0:
        return

    batches = conn.execute(
        "SELECT batch_id, frame_start, frame_end FROM batches"
    ).fetchall()
    for batch in batches:
        both = intervals.intersect(
            _coverage_all(conn, batch["batch_id"], "collector"),
            _coverage_all(conn, batch["batch_id"], "library"),
        )
        missing = intervals.subtract(
            [(batch["frame_start"], batch["frame_end"])], both
        )
        for seg in missing:
            _insert_open(conn, batch["batch_id"], seg, at)


def _settle_segment(
    conn, batch_id: str, last_side: str, seg: Segment, at: int
) -> None:
    """把 seg 内仍开放的轮次结清，记录最后补齐的一侧与欠档秒数。"""
    s, e = seg
    covered: List[Segment] = []
    for row in _open_in(conn, batch_id, s, e):
        os_, oe_, round_start = row["seg_start"], row["seg_end"], row["round_start"]
        ov_s, ov_e = max(os_, s), min(oe_, e)
        if ov_s > ov_e:
            continue
        covered.append((ov_s, ov_e))
        # 切开轮次：结清部分出账，两侧余下部分仍沿用原起点
        conn.execute("DELETE FROM open_rounds WHERE id=?", (row["id"],))
        left = []
        if os_ < ov_s:
            left.append((os_, ov_s - 1))
        if oe_ > ov_e:
            left.append((ov_e + 1, oe_))
        for piece in left:
            _insert_open(conn, batch_id, piece, round_start)
        owed = max(0, at - round_start)
        conn.execute(
            "INSERT INTO settlements "
            "(batch_id, side, settle_at, seg_start, seg_end, owed_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (batch_id, last_side, at, ov_s, ov_e, owed),
        )

    # 账目应覆盖全部未齐帧；若历史数据缺失轮次，按 0 秒兜底结清，
    # 不让一笔改动因为账目缺口而整体失败。
    for us, ue in intervals.subtract([seg], intervals.merge(covered)):
        conn.execute(
            "INSERT INTO settlements "
            "(batch_id, side, settle_at, seg_start, seg_end, owed_seconds) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (batch_id, last_side, at, us, ue),
        )


def _reopen_segment(conn, batch_id: str, seg: Segment, at: int) -> None:
    """双方齐全后再次缺档：为尚无轮次的帧另起一轮（已结清的账照留）。"""
    existing = _open_coverage_in(conn, batch_id, seg[0], seg[1])
    for s, e in intervals.subtract([seg], existing):
        _insert_open(conn, batch_id, (s, e), at)


def apply_coverage_transition(
    conn,
    batch_id: str,
    changed_side: str,
    region: Segment,
    old_side_cov: Sequence[Segment],
    new_side_cov: Sequence[Segment],
    other_cov: Sequence[Segment],
    at: Optional[int] = None,
) -> None:
    """按一次写操作提交前后的覆盖状态更新计时账。

    ``old_side_cov`` / ``new_side_cov`` 是发生变更一侧在 region 内的覆盖，
    ``other_cov`` 是另一侧在 region 内的覆盖（一次写操作不会改动它）。
    只处理 region 内的差异，不重扫整批计时账。
    """
    at = now_epoch() if at is None else at
    old_both = intervals.intersect(old_side_cov, other_cov)
    new_both = intervals.intersect(new_side_cov, other_cov)

    # 新近齐全：结清，最后补齐的一侧就是本次变更的一侧
    for seg in intervals.subtract(new_both, old_both):
        _settle_segment(conn, batch_id, changed_side, seg, at)
    # 新近缺档：另起一轮
    for seg in intervals.subtract(old_both, new_both):
        _reopen_segment(conn, batch_id, seg, at)


# ---------- 班次汇总 ----------

def shift_summary(
    conn, batch_id: str, start: int, end: int, timeout_seconds: int
) -> dict:
    """按最后补齐的一侧汇总 [start, end) 内结清的帧。

    结清时刻为服务端记录的 UTC 整秒，左闭右开；欠档秒数严格大于
    timeout_seconds 才计入超时。帧秒按 ``帧数 × 欠档秒数`` 计算。
    """
    if conn.execute(
        "SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)
    ).fetchone() is None:
        raise LookupError(f"批次不存在: {batch_id}")

    rows = conn.execute(
        "SELECT side, seg_start, seg_end, owed_seconds FROM settlements "
        "WHERE batch_id=? AND settle_at >= ? AND settle_at < ?",
        (batch_id, start, end),
    ).fetchall()

    stats = {side: _empty_stat() for side in SIDES}
    for row in rows:
        item = stats[row["side"]]
        frames = row["seg_end"] - row["seg_start"] + 1
        owed = row["owed_seconds"]
        item["settled_frames"] += frames
        if owed > timeout_seconds:
            item["timeout_frames"] += frames
        item["owed_frame_seconds"] += frames * owed

    total = _empty_stat()
    for item in stats.values():
        for key in total:
            total[key] += item[key]
    return {"by_last_filled_side": stats, "total": total}


def _empty_stat() -> dict:
    return {"settled_frames": 0, "timeout_frames": 0, "owed_frame_seconds": 0}
