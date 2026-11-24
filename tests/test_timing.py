"""补档时效账测试：用可控时钟驱动结清、重开、班次汇总与持久化。"""

import importlib

import pytest


@pytest.fixture()
def clock(monkeypatch):
    import app.timing as timing_mod

    state = {"now": 1000}
    monkeypatch.setattr(timing_mod, "now_epoch", lambda: state["now"])
    return state


def _make_client(db_file):
    from fastapi.testclient import TestClient

    import app.db as db_mod
    import app.main as main_mod

    importlib.reload(db_mod)
    importlib.reload(main_mod)
    return TestClient(main_mod.app)


@pytest.fixture()
def tclient(tmp_path, monkeypatch, clock):
    db_file = tmp_path / "timing.db"
    monkeypatch.setenv("QC_DB_PATH", str(db_file))
    with _make_client(db_file) as c:
        yield c


def _batch(client, bid="B1", start=1, end=100):
    r = client.post("/batches", json={"batch_id": bid, "frame_start": start, "frame_end": end})
    assert r.status_code == 201, r.text


def _manifest(client, no, bid, side, s, e):
    r = client.post(
        "/manifests",
        json={"manifest_no": no, "batch_id": bid, "side": side, "seg_start": s, "seg_end": e},
    )
    assert r.status_code == 201, r.text


def _summary(client, bid, start, end, timeout):
    r = client.get(
        f"/batches/{bid}/shift-summary",
        params={"start": start, "end": end, "timeout_seconds": timeout},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _iso(epoch):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_settle_by_last_filled_side_and_frame_seconds(tclient, clock):
    clock["now"] = 1000
    _batch(tclient)
    clock["now"] = 1010
    _manifest(tclient, "L1", "B1", "library", 1, 100)
    clock["now"] = 1020
    _manifest(tclient, "C1", "B1", "collector", 1, 50)
    clock["now"] = 1030
    _manifest(tclient, "C2", "B1", "collector", 51, 100)

    r = _summary(tclient, "B1", _iso(1020), _iso(1031), 5)
    assert r["by_last_filled_side"]["collector"] == {
        "settled_frames": 100,
        "timeout_frames": 100,
        "owed_frame_seconds": 50 * 20 + 50 * 30,
    }
    assert r["by_last_filled_side"]["library"]["settled_frames"] == 0

    # 欠档秒数严格大于阈值才算超时：20 秒、30 秒对 30 秒阈值均不超时
    r = _summary(tclient, "B1", _iso(1020), _iso(1031), 30)
    assert r["total"]["settled_frames"] == 100
    assert r["total"]["timeout_frames"] == 0

    # 左闭右开：起点包含，终点排除
    assert _summary(tclient, "B1", _iso(1020), _iso(1030), 0)["total"]["settled_frames"] == 50
    assert _summary(tclient, "B1", _iso(1021), _iso(1031), 0)["total"]["settled_frames"] == 50


def test_library_last_fill_counted_on_library_side(tclient, clock):
    clock["now"] = 0
    _batch(tclient, end=10)
    clock["now"] = 4
    _manifest(tclient, "C1", "B1", "collector", 1, 10)
    clock["now"] = 9
    _manifest(tclient, "L1", "B1", "library", 1, 10)
    r = _summary(tclient, "B1", _iso(0), _iso(10), 5)
    assert r["by_last_filled_side"]["library"] == {
        "settled_frames": 10,
        "timeout_frames": 10,
        "owed_frame_seconds": 90,
    }
    assert r["by_last_filled_side"]["collector"]["settled_frames"] == 0


def test_redundant_manifest_does_not_settle_twice(tclient, clock):
    clock["now"] = 0
    _batch(tclient, end=10)
    clock["now"] = 2
    _manifest(tclient, "C1", "B1", "collector", 1, 5)
    _manifest(tclient, "C2", "B1", "collector", 3, 10)
    clock["now"] = 4
    _manifest(tclient, "L1", "B1", "library", 1, 10)
    assert _summary(tclient, "B1", _iso(0), _iso(10), 0)["total"]["settled_frames"] == 10

    # 同侧重叠登记不改变双方齐全状态，不应再结清
    clock["now"] = 6
    _manifest(tclient, "C3", "B1", "collector", 1, 10)
    r = _summary(tclient, "B1", _iso(0), _iso(10), 0)
    r = _summary(tclient, "B1", _iso(0), _iso(10), 0)
    assert r["total"]["settled_frames"] == 10
    assert r["total"]["owed_frame_seconds"] == 10 * 4


def test_shift_window_excludes_outside_settlements(tclient, clock):
    clock["now"] = 0
    _batch(tclient, end=10)
    clock["now"] = 5
    _manifest(tclient, "L1", "B1", "library", 1, 10)
    clock["now"] = 50
    _manifest(tclient, "C1", "B1", "collector", 1, 10)

    assert _summary(tclient, "B1", _iso(0), _iso(50), 0)["total"]["settled_frames"] == 0
    assert _summary(tclient, "B1", _iso(50), _iso(51), 0)["total"]["settled_frames"] == 10


def test_write_outside_region_leaves_other_rounds_alone(tclient, clock):
    clock["now"] = 0
    _batch(tclient, end=30)
    clock["now"] = 1
    _manifest(tclient, "L1", "B1", "library", 1, 30)
    # 1-10 在 t=2 结清，11-30 的轮次起点仍是 0
    clock["now"] = 2
    _manifest(tclient, "C1", "B1", "collector", 1, 10)
    clock["now"] = 12
    _manifest(tclient, "C2", "B1", "collector", 11, 30)
    r = _summary(tclient, "B1", _iso(0), _iso(20), 0)
    assert r["total"]["settled_frames"] == 30
    assert r["total"]["owed_frame_seconds"] == 10 * 2 + 20 * 12


def test_one_missing_side_keeps_same_round_origin(tclient, clock):
    clock["now"] = 0
    _batch(tclient, end=10)
    clock["now"] = 5
    _manifest(tclient, "C1", "B1", "collector", 1, 10)
    clock["now"] = 8
    _manifest(tclient, "L1", "B1", "library", 1, 5)
    # 1-5 此时两侧齐全，6-10 仍缺影像库：同一轮起点 0
    clock["now"] = 20
    _manifest(tclient, "L2", "B1", "library", 6, 10)

    r = _summary(tclient, "B1", _iso(0), _iso(21), 100)
    # 1-5 欠档 8 秒，6-10 沿用本轮起点 0 欠档 20 秒
    assert r["by_last_filled_side"]["library"]["settled_frames"] == 10
    assert r["by_last_filled_side"]["library"]["owed_frame_seconds"] == 5 * 8 + 5 * 20


def test_reopen_after_settle_starts_new_round_and_keeps_old_books(tclient, clock):
    clock["now"] = 1000
    _batch(tclient, end=50)
    clock["now"] = 1010
    _manifest(tclient, "L1", "B1", "library", 1, 50)
    clock["now"] = 1020
    _manifest(tclient, "C1", "B1", "collector", 1, 50)

    # 撤回后 1-50 再次缺档，另起一轮
    clock["now"] = 1040
    assert tclient.post("/manifests/withdraw", json={"manifest_no": "C1"}).status_code == 200
    # 撤回未使状态变化时不应重复起账
    assert tclient.post("/manifests/withdraw", json={"manifest_no": "C1"}).status_code == 409

    clock["now"] = 1050
    _manifest(tclient, "C3", "B1", "collector", 1, 50)

    r = _summary(tclient, "B1", _iso(1000), _iso(2000), 5)
    assert r["by_last_filled_side"]["collector"]["settled_frames"] == 100
    assert r["by_last_filled_side"]["collector"]["owed_frame_seconds"] == 50 * 20 + 50 * 10


def test_correction_uses_before_and_after_state(tclient, clock):
    clock["now"] = 0
    _batch(tclient, end=10)
    clock["now"] = 1
    _manifest(tclient, "L1", "B1", "library", 1, 10)
    clock["now"] = 2
    _manifest(tclient, "C1", "B1", "collector", 1, 5)
    assert _summary(tclient, "B1", _iso(0), _iso(3), 0)["total"]["settled_frames"] == 5

    # 扩展到 1-10：新增 6-10 结清，欠档 10 秒
    clock["now"] = 10
    assert tclient.put("/manifests/C1", json={"seg_start": 1, "seg_end": 10}).status_code == 200
    r = _summary(tclient, "B1", _iso(10), _iso(11), 0)
    assert r["by_last_filled_side"]["collector"]["settled_frames"] == 5
    assert r["by_last_filled_side"]["collector"]["owed_frame_seconds"] == 50

    # 再缩回 1-5：6-10 从齐全变为缺档，另起一轮，旧账仍在
    clock["now"] = 20
    assert tclient.put("/manifests/C1", json={"seg_start": 1, "seg_end": 5}).status_code == 200
    assert _summary(tclient, "B1", _iso(0), _iso(21), 0)["total"]["settled_frames"] == 10
    clock["now"] = 25
    assert tclient.put("/manifests/C1", json={"seg_start": 1, "seg_end": 10}).status_code == 200
    r = _summary(tclient, "B1", _iso(25), _iso(26), 0)
    assert r["by_last_filled_side"]["collector"]["settled_frames"] == 5
    assert r["by_last_filled_side"]["collector"]["owed_frame_seconds"] == 25


def test_failed_write_leaves_timing_books_unchanged(tclient, clock):
    clock["now"] = 0
    _batch(tclient, end=10)
    clock["now"] = 5
    _manifest(tclient, "L1", "B1", "library", 1, 10)
    clock["now"] = 6
    _manifest(tclient, "C1", "B1", "collector", 1, 10)
    before = _summary(tclient, "B1", _iso(0), _iso(7), 0)
    assert before["total"]["settled_frames"] == 10

    clock["now"] = 7
    r = tclient.post(
        "/manifests",
        json={"manifest_no": "X", "batch_id": "B1", "side": "collector",
              "seg_start": 0, "seg_end": 10},
    )
    assert r.status_code == 422
    after = _summary(tclient, "B1", _iso(0), _iso(8), 0)
    assert after["total"] == before["total"]
    assert after["by_last_filled_side"] == before["by_last_filled_side"]


def test_billion_frames_counted_by_arithmetic(tclient, clock):
    clock["now"] = 0
    _batch(tclient, "BIG", 0, 2_000_000_000)
    clock["now"] = 10
    _manifest(tclient, "L1", "BIG", "library", 0, 2_000_000_000)
    clock["now"] = 15
    _manifest(tclient, "C1", "BIG", "collector", 0, 2_000_000_000)

    r = _summary(tclient, "BIG", _iso(15), _iso(16), 5)
    frames = 2_000_000_001
    assert r["by_last_filled_side"]["collector"]["settled_frames"] == frames
    assert r["by_last_filled_side"]["collector"]["owed_frame_seconds"] == frames * 15
    assert r["total"]["timeout_frames"] == frames

    # 未齐量不影响结清：仅覆盖一部分号段同样按号段记账
    clock["now"] = 20
    _batch(tclient, "BIG2", 0, 5_000_000_000)
    clock["now"] = 30
    _manifest(tclient, "L2", "BIG2", "library", 0, 4_999_999_999)
    clock["now"] = 40
    _manifest(tclient, "C2", "BIG2", "collector", 1_000_000_000, 2_000_000_000)
    r = _summary(tclient, "BIG2", _iso(40), _iso(41), 0)
    assert r["total"]["settled_frames"] == 1_000_000_001


def test_restart_continues_timing(tclient, clock, tmp_path, monkeypatch):
    clock["now"] = 100
    _batch(tclient, end=10)
    clock["now"] = 110
    _manifest(tclient, "L1", "B1", "library", 1, 10)
    clock["now"] = 130
    _manifest(tclient, "C1", "B1", "collector", 1, 10)
    assert _summary(tclient, "B1", _iso(0), _iso(1000), 0)["total"]["settled_frames"] == 10

    tclient.__exit__(None, None, None)
    clock["now"] = 500
    with _make_client(tmp_path / "timing.db") as again:
        r = again.get(
            "/batches/B1/shift-summary",
            params={"start": _iso(0), "end": _iso(1000), "timeout_seconds": 0},
        ).json()
        assert r["total"]["settled_frames"] == 10
        # 批次 100 秒登记，130 秒结清：欠档 30 秒，重启后账目不变
        assert r["total"]["owed_frame_seconds"] == 10 * 30


def test_legacy_batch_starts_at_feature_enable(tmp_path, monkeypatch, clock):
    """旧库启用功能时，未齐帧从启用时刻起计时；重复初始化不重置。"""
    import sqlite3

    import app.db as db_mod

    path = tmp_path / "legacy.db"
    monkeypatch.setenv("QC_DB_PATH", str(path))
    conn = sqlite3.connect(path)
    conn.executescript(db_mod.SCHEMA)
    conn.execute("INSERT INTO batches (batch_id, frame_start, frame_end) VALUES ('OLD', 1, 10)")
    conn.execute(
        "INSERT INTO manifests (manifest_no, batch_id, side, seg_start, seg_end) "
        "VALUES ('CL', 'OLD', 'collector', 1, 4)"
    )
    conn.execute(
        "INSERT INTO manifests (manifest_no, batch_id, side, seg_start, seg_end) "
        "VALUES ('LL', 'OLD', 'library', 1, 4)"
    )
    conn.execute(
        "INSERT INTO coverage (batch_id, side, seg_start, seg_end) "
        "VALUES ('OLD', 'collector', 1, 4)"
    )
    conn.execute(
        "INSERT INTO coverage (batch_id, side, seg_start, seg_end) "
        "VALUES ('OLD', 'library', 1, 4)"
    )
    conn.commit()
    conn.close()

    clock["now"] = 5000
    db_mod.init_db(str(path))
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT seg_start, seg_end, round_start FROM open_rounds WHERE batch_id='OLD'"
    ).fetchall()
    assert rows == [(5, 10, 5000)]

    # 功能已启用后再重启：不得重置起点
    clock["now"] = 9000
    db_mod.init_db(str(path))
    rows = conn.execute(
        "SELECT seg_start, seg_end, round_start FROM open_rounds WHERE batch_id='OLD'"
    ).fetchall()
    assert rows == [(5, 10, 5000)]
    conn.close()


def test_timing_failure_rolls_back_manifest_and_coverage(tclient, clock, monkeypatch):
    """计时账写入失败时，清单与覆盖必须一起回滚。"""
    import app.timing as timing_mod

    clock["now"] = 0
    _batch(tclient, end=10)
    clock["now"] = 5
    _manifest(tclient, "L1", "B1", "library", 1, 10)

    def boom(*args, **kwargs):
        raise RuntimeError("timing write failed")

    original = timing_mod._settle_segment
    monkeypatch.setattr(timing_mod, "_settle_segment", boom)
    clock["now"] = 8
    with pytest.raises(RuntimeError):
        tclient.post(
            "/manifests",
            json={"manifest_no": "C1", "batch_id": "B1", "side": "collector",
                  "seg_start": 1, "seg_end": 10},
        )

    # 恢复结清函数（不能 monkeypatch.undo()，那会连可控时钟一起撤销）
    monkeypatch.setattr(timing_mod, "_settle_segment", original)
    assert tclient.get("/manifests/C1").status_code == 404
    r = tclient.get("/batches/B1/reconcile").json()
    assert r["both_present"] == []
    assert r["only_library"] == [{"frame_start": 1, "frame_end": 10}]
    # 未齐轮次仍在，起点仍为批次登记时刻
    clock["now"] = 12
    _manifest(tclient, "C1", "B1", "collector", 1, 10)
    total = _summary(tclient, "B1", _iso(0), _iso(20), 0)["total"]
    assert total["settled_frames"] == 10
    assert total["owed_frame_seconds"] == 10 * 12


def test_shift_summary_validation(tclient):
    _batch(tclient)
    bad = tclient.get(
        "/batches/B1/shift-summary",
        params={"start": "not-a-time", "end": _iso(10), "timeout_seconds": 1},
    )
    assert bad.status_code == 422
    missing = tclient.get(
        "/batches/NOPE/shift-summary",
        params={"start": _iso(0), "end": _iso(10), "timeout_seconds": 1},
    )
    assert missing.status_code == 404
