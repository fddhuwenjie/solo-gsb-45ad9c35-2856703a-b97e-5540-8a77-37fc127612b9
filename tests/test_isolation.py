from datetime import datetime, timedelta, timezone

import pytest

from app.engine import (
    EVENT_ENTER,
    EVENT_LEAVE,
    IsolationGroup,
    compute_isolation_report,
)

BASE = datetime(2026, 10, 4, 8, 0, 0, tzinfo=timezone.utc)


# 三个横向部分重叠的正方形围栏（y 范围均为 0..0.01，宽 0.010、相邻左边界间距 0.006）：
#   F1 x∈[0.000,0.010]  F2 x∈[0.006,0.016]  F3 x∈[0.012,0.022]
# 仅 F1 区 x∈[0,.006)；F1∩F2 x∈[.006,.010)；仅 F2 区 x∈[.010,.012)；
# F2∩F3 x∈[.012,.016)；仅 F3 区 x∈[.016,.022]。
def fence_geom(x0):
    return {
        "type": "Polygon",
        "coordinates": [
            [[x0, 0], [x0 + 0.010, 0], [x0 + 0.010, 0.01], [x0, 0.01], [x0, 0]]
        ],
    }


F1_GEO, F2_GEO, F3_GEO = fence_geom(0.0), fence_geom(0.006), fence_geom(0.012)
# 与三者都不相交的远端围栏
F4_GEO = fence_geom(0.5)
F5_GEO = fence_geom(0.6)

Y = 0.005
ONLY1 = (Y, 0.003)    # 仅 F1
BOTH12 = (Y, 0.008)   # F1∩F2
ONLY2 = (Y, 0.011)    # 仅 F2
BOTH23 = (Y, 0.014)   # F2∩F3
ONLY3 = (Y, 0.019)    # 仅 F3
OUTSIDE = (0.9, 0.9)

START_SEC, END_SEC = 100, 400


def pt(device, sec, latlon):
    lat, lon = latlon
    return {
        "device_id": device,
        "ts": (BASE + timedelta(seconds=sec)).isoformat(),
        "lat": lat,
        "lon": lon,
    }


def iso(sec):
    return (BASE + timedelta(seconds=sec)).isoformat()


def dts(sec):
    return BASE + timedelta(seconds=sec)


def create_fences(client):
    ids = []
    for g in (F1_GEO, F2_GEO, F3_GEO, F4_GEO, F5_GEO):
        r = client.post("/fences", json={"name": f"F{len(ids)+1}", "geometry": g})
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    return ids  # f1..f5


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def create_task(client, groups, name="隔离核验"):
    body = {"name": name, "groups": []}
    for fence_ids, start, end in groups:
        body["groups"].append({
            "fence_ids": list(fence_ids),
            "start_at": iso(start),
            "end_at": iso(end),
        })
    r = client.post("/isolation-tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def report(client, task_id):
    r = client.get(f"/isolation-tasks/{task_id}/report")
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 创建校验与 CRUD ----------


def test_create_validation_and_crud(client):
    f1, f2, f3, f4, f5 = create_fences(client)

    # 围栏不存在 -> 422
    r = client.post("/isolation-tasks", json={
        "name": "x",
        "groups": [
            {"fence_ids": [f1, 9999], "start_at": iso(0), "end_at": iso(10)},
            {"fence_ids": [f3, f4], "start_at": iso(0), "end_at": iso(10)},
        ],
    })
    assert r.status_code == 422

    def bad(groups, name="bad"):
        return client.post("/isolation-tasks", json={"name": name, "groups": groups})

    def grp(fences, s=0, e=10):
        return {"fence_ids": list(fences), "start_at": iso(s), "end_at": iso(e)}

    # 组数必须 2～8
    assert bad([grp([f1, f2])]).status_code == 422
    assert bad([grp([f1, f2]) for _ in range(9)]).status_code == 422
    # 每组围栏数必须 2～4
    assert bad([grp([f1]), grp([f2, f3])]).status_code == 422
    assert bad([grp([f1, f2, f3, f4, f5]), grp([f1, f2])]).status_code == 422
    # 组内围栏不得重复
    assert bad([grp([f1, f1]), grp([f2, f3])]).status_code == 422
    # 窗口无效
    assert bad([grp([f1, f2], 10, 10), grp([f3, f4])]).status_code == 422
    assert bad([grp([f1, f2], 20, 10), grp([f3, f4])]).status_code == 422
    # 空名
    assert bad([grp([f1, f2]), grp([f3, f4])], name="").status_code == 422

    # 合法：同一围栏可跨组复用
    task = create_task(client, [
        ([f2, f1], START_SEC, END_SEC),
        ([f4, f5, f2], START_SEC, END_SEC),
    ], name="隔离A")
    # 组按提交顺序，组内围栏按 ID 排序
    assert [g["position"] for g in task["groups"]] == [0, 1]
    assert task["groups"][0]["fence_ids"] == sorted([f2, f1])
    assert task["groups"][1]["fence_ids"] == sorted([f4, f5, f2])

    # 列表按 id 升序
    create_task(client, [([f1, f3], START_SEC, END_SEC), ([f4, f5], START_SEC, END_SEC)],
                name="隔离B")
    tasks = client.get("/isolation-tasks").json()
    assert [t["name"] for t in tasks] == ["隔离A", "隔离B"]

    # 删除；重复删除与不存在报告 -> 404
    tid = tasks[0]["id"]
    assert client.delete(f"/isolation-tasks/{tid}").status_code == 204
    assert client.delete(f"/isolation-tasks/{tid}").status_code == 404
    assert client.get(f"/isolation-tasks/{tid}/report").status_code == 404


def test_report_missing_task_404(client):
    assert client.get("/isolation-tasks/999/report").status_code == 404


# ---------- 基本冲突核验 ----------


def test_basic_conflict_segment(client):
    f1, f2, f3, f4, f5 = create_fences(client)
    # d1：F1@110 -> 重叠区@130（两围栏冲突）-> 仅 F2@200（F1 离开）-> 外@300
    submit(client, [
        pt("d1", 110, ONLY1),
        pt("d1", 130, BOTH12),
        pt("d1", 200, ONLY2),
        pt("d1", 300, OUTSIDE),
    ])
    task = create_task(client, [
        ([f1, f2], START_SEC, END_SEC),
        ([f4, f5], START_SEC, END_SEC),
    ])
    rep = report(client, task["id"])
    assert rep["passed"] is False
    assert len(rep["devices"]) == 1
    dev = rep["devices"][0]
    assert dev["device_id"] == "d1"
    assert datetime.fromisoformat(dev["first_conflict_at"]) == dts(130)
    assert len(dev["groups"]) == 1  # 只有第 0 组冲突
    g0 = dev["groups"][0]
    assert g0["position"] == 0
    assert datetime.fromisoformat(g0["first_conflict_at"]) == dts(130)
    assert len(g0["segments"]) == 1
    seg = g0["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(130)
    assert datetime.fromisoformat(seg["end"]) == dts(200)
    assert seg["duration_s"] == pytest.approx(70.0)
    assert seg["active_fence_ids"] == [f1, f2]


def test_passing_when_no_overlap(client):
    f1, f2, f3, f4, f5 = create_fences(client)
    # d1 只在 F1，d2 只在 F2，从不同时处于两个围栏
    submit(client, [
        pt("d1", 110, ONLY1), pt("d1", 300, OUTSIDE),
        pt("d2", 110, ONLY2), pt("d2", 300, OUTSIDE),
    ])
    task = create_task(client, [
        ([f1, f2], START_SEC, END_SEC),
        ([f4, f5], START_SEC, END_SEC),
    ])
    rep = report(client, task["id"])
    assert rep["passed"] is True
    assert rep["devices"] == []


def test_active_set_change_splits_segments(client):
    f1, f2, f3, f4, f5 = create_fences(client)
    # d1：F1@110 -> F1∩F2@130（{f1,f2}）-> 线 x=.010 无法停留，改用 F2∩F3：
    # 设备依次处于 {f1,f2} 与 {f2,f3}，活跃集合变化必须切分为两段。
    # 轨迹：F1@110 -> 重叠12@130 -> 仅 F2@160 -> 重叠23@200 -> 仅 F3@250
    submit(client, [
        pt("d1", 110, ONLY1),
        pt("d1", 130, BOTH12),
        pt("d1", 160, ONLY2),
        pt("d1", 200, BOTH23),
        pt("d1", 250, ONLY3),
    ])
    # 单组含三个围栏 f1,f2,f3
    task = create_task(client, [
        ([f1, f2, f3], START_SEC, END_SEC),
        ([f4, f5], START_SEC, END_SEC),
    ])
    rep = report(client, task["id"])
    assert rep["passed"] is False
    segs = rep["devices"][0]["groups"][0]["segments"]
    def sec(v):
        return int(datetime.fromisoformat(v).timestamp() - BASE.timestamp())
    assert [(sec(s["start"]), sec(s["end"]), s["active_fence_ids"]) for s in segs] == [
        (130, 160, [f1, f2]),
        (200, 250, [f2, f3]),
    ]


def test_groups_judged_independently(client):
    f1, f2, f3, f4, f5 = create_fences(client)
    # 组0=[f1,f2] 冲突；组1=[f4,f5] 不冲突；另一任务视角下 f1,f3 不重叠
    submit(client, [
        pt("d1", 110, ONLY1),
        pt("d1", 130, BOTH12),
        pt("d1", 200, ONLY2),
    ])
    task = create_task(client, [
        ([f1, f2], START_SEC, END_SEC),
        ([f1, f3], START_SEC, END_SEC),  # f1 与 f3 不重叠 -> 永不冲突
        ([f4, f5], START_SEC, END_SEC),
    ])
    rep = report(client, task["id"])
    assert rep["passed"] is False
    positions = [g["position"] for g in rep["devices"][0]["groups"]]
    assert positions == [0]


def test_devices_stable_sorted(client):
    f1, f2, f3, f4, f5 = create_fences(client)
    # zeta 与 alpha 都冲突；alpha 还在另一组冲突 -> 设备按 ID 排序、组按 position
    submit(client, [
        pt("zeta", 110, ONLY1), pt("zeta", 130, BOTH12), pt("zeta", 200, OUTSIDE),
        pt("alpha", 110, ONLY1), pt("alpha", 130, BOTH12), pt("alpha", 200, OUTSIDE),
    ])
    # 让 alpha 同时在组 1 冲突：把组1设为 [f1,f2]（同围栏跨组允许）
    task = create_task(client, [
        ([f1, f2], START_SEC, END_SEC),
        ([f2, f1], START_SEC, END_SEC),
    ])
    rep = report(client, task["id"])
    assert [d["device_id"] for d in rep["devices"]] == ["alpha", "zeta"]
    for d in rep["devices"]:
        assert [g["position"] for g in d["groups"]] == [0, 1]


# ---------- 半开边界与窗口截断 ----------


def test_window_clipping(client):
    f1, f2, f3, f4, f5 = create_fences(client)
    # 冲突区间 [130,200)：窗口 [150,180) 只截出 [150,180)
    submit(client, [
        pt("d1", 110, ONLY1),
        pt("d1", 130, BOTH12),
        pt("d1", 200, ONLY2),
    ])
    task = create_task(client, [
        ([f1, f2], 150, 180),
        ([f4, f5], 150, 180),
    ])
    rep = report(client, task["id"])
    seg = rep["devices"][0]["groups"][0]["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(150)
    assert datetime.fromisoformat(seg["end"]) == dts(180)
    # 首个冲突时刻仍为窗口内片段起端（截断到 150）
    assert datetime.fromisoformat(
        rep["devices"][0]["first_conflict_at"]) == dts(150)


def test_conflict_ending_at_window_end_included(client):
    f1, f2, f3, f4, f5 = create_fences(client)
    # 设备在窗口结束 end=200 离开其中一个围栏：leave@end 不含，冲突片段可取到 end
    submit(client, [
        pt("d1", 110, ONLY1),
        pt("d1", 130, BOTH12),
        pt("d1", 200, ONLY2),  # F1 leave@200
    ])
    task = create_task(client, [
        ([f1, f2], START_SEC, 200),
        ([f4, f5], START_SEC, 200),
    ])
    rep = report(client, task["id"])
    seg = rep["devices"][0]["groups"][0]["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(130)
    assert datetime.fromisoformat(seg["end"]) == dts(200)


def test_enter_at_window_start_counts(client):
    f1, f2, f3, f4, f5 = create_fences(client)
    # 两台设备？不——同一设备在 start=130 进入第二个围栏（轨迹点在重叠区）
    submit(client, [
        pt("d1", 100, ONLY1),
        pt("d1", 130, BOTH12),  # F2 enter@130 == 窗口 start
        pt("d1", 200, ONLY2),
    ])
    task = create_task(client, [
        ([f1, f2], 130, END_SEC),
        ([f4, f5], 130, END_SEC),
    ])
    rep = report(client, task["id"])
    seg = rep["devices"][0]["groups"][0]["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(130)


# ---------- 引擎级半开边界：同刻 leave 先于 enter ----------


def test_engine_same_instant_leave_before_enter():
    # 围栏 1、2；设备对两围栏各有区间。t=20 同时：离开 f1、进入 f2
    # f1: [0,20)，f2: [20,50) -> [20,..) 只在 f2，不冲突；t<20 只在 f1
    events = [
        ("d", 1, EVENT_ENTER, 0), ("d", 1, EVENT_LEAVE, 20),
        ("d", 2, EVENT_ENTER, 20), ("d", 2, EVENT_LEAVE, 50),
    ]
    groups = [IsolationGroup(0, (1, 2), 0, 100), IsolationGroup(1, (3, 4), 0, 100)]
    rep = compute_isolation_report(events, groups)
    assert rep.passed is True
    assert rep.results == []

    # 同刻 f1 leave + f1 enter（同一围栏），且 f2 全程在场：同刻离开先配对，
    # 新区间起端含该刻，冲突连续不断点
    events = [
        ("d", 2, EVENT_ENTER, 0), ("d", 2, EVENT_LEAVE, 60),
        ("d", 1, EVENT_ENTER, 0), ("d", 1, EVENT_LEAVE, 20),
        ("d", 1, EVENT_ENTER, 20), ("d", 1, EVENT_LEAVE, 60),
    ]
    rep = compute_isolation_report(events, [IsolationGroup(0, (1, 2), 0, 100)])
    assert rep.passed is False
    segs = rep.results[0].groups[0].segments
    assert [(s.start, s.end, s.active) for s in segs] == [(0, 60, (1, 2))]


def test_engine_active_set_change_split():
    # {1,2}@[10,20)，{2,3}@[20,30)：同刻 f1 leave、f3 enter（f2 全程）
    events = [
        ("d", 2, EVENT_ENTER, 0), ("d", 2, EVENT_LEAVE, 40),
        ("d", 1, EVENT_ENTER, 10), ("d", 1, EVENT_LEAVE, 20),
        ("d", 3, EVENT_ENTER, 20), ("d", 3, EVENT_LEAVE, 30),
    ]
    groups = [IsolationGroup(0, (1, 2, 3), 0, 100), IsolationGroup(1, (9, 8), 0, 100)]
    rep = compute_isolation_report(events, groups)
    assert rep.passed is False
    g0 = rep.results[0].groups[0]
    assert [(s.start, s.end, s.active) for s in g0.segments] == [
        (10, 20, (1, 2)),
        (20, 30, (2, 3)),
    ]
    assert g0.first_conflict_ts == 10
    assert rep.results[0].first_conflict_ts == 10
    # 第 1 组围栏 8/9 无事件 -> 不出现在结果组中
    assert [g.position for g in rep.results[0].groups] == [0]


def test_engine_window_boundaries_half_open():
    # 冲突 [10,40)，截到 [20,40)；end 处 leave 不算提前结束
    events = [
        ("d", 1, EVENT_ENTER, 10), ("d", 1, EVENT_LEAVE, 40),
        ("d", 2, EVENT_ENTER, 10), ("d", 2, EVENT_LEAVE, 40),
    ]
    rep = compute_isolation_report(events, [IsolationGroup(0, (1, 2), 20, 40)])
    segs = rep.results[0].groups[0].segments
    assert [(s.start, s.end) for s in segs] == [(20, 40)]

    # 冲突恰止于窗口 start（leave@start）：start 起已不在围栏 -> 无冲突
    events = [
        ("d", 1, EVENT_ENTER, 0), ("d", 1, EVENT_LEAVE, 20),
        ("d", 2, EVENT_ENTER, 0), ("d", 2, EVENT_LEAVE, 20),
    ]
    rep = compute_isolation_report(events, [IsolationGroup(0, (1, 2), 20, 40)])
    assert rep.passed is True


def test_engine_sorting_and_first_conflict():
    # 设备 b 早冲突、a 晚冲突；a 在多组冲突取最早 first_conflict
    events = [
        ("b", 1, EVENT_ENTER, 0), ("b", 1, EVENT_LEAVE, 50),
        ("b", 2, EVENT_ENTER, 0), ("b", 2, EVENT_LEAVE, 50),
        # a：围栏 2 全程在场；组0={1,2} 冲突 [30,80)；组1={2,3} 冲突 [10,80)
        ("a", 2, EVENT_ENTER, 0), ("a", 2, EVENT_LEAVE, 80),
        ("a", 1, EVENT_ENTER, 30), ("a", 1, EVENT_LEAVE, 80),
        ("a", 3, EVENT_ENTER, 10), ("a", 3, EVENT_LEAVE, 80),
    ]
    groups = [
        IsolationGroup(0, (1, 2), 0, 100),
        IsolationGroup(1, (2, 3), 0, 100),
    ]
    rep = compute_isolation_report(events, groups)
    assert [r.device_id for r in rep.results] == ["a", "b"]
    a = rep.results[0]
    assert a.first_conflict_ts == 10  # 组1 自 10 起 {2,3}，早于组0 的 30
    assert [g.position for g in a.groups] == [0, 1]
    assert a.groups[0].first_conflict_ts == 30
    assert a.groups[1].first_conflict_ts == 10


# ---------- 实时重建、无快照 ----------


def test_leave_then_reenter_produces_separate_segments(client):
    f1, f2, f3, f4, f5 = create_fences(client)
    # d1：冲突 [130,160)，离开（仅 F2）后又在 200 回到重叠区 -> 两段独立冲突
    submit(client, [
        pt("d1", 110, ONLY1),
        pt("d1", 130, BOTH12),
        pt("d1", 160, ONLY2),
        pt("d1", 200, BOTH12),
        pt("d1", 250, ONLY1),
        pt("d1", 300, OUTSIDE),
    ])
    task = create_task(client, [
        ([f1, f2], START_SEC, END_SEC),
        ([f4, f5], START_SEC, END_SEC),
    ])
    rep = report(client, task["id"])
    assert rep["passed"] is False
    segs = rep["devices"][0]["groups"][0]["segments"]
    def sec(v):
        return int(datetime.fromisoformat(v).timestamp() - BASE.timestamp())
    assert [(sec(s["start"]), sec(s["end"])) for s in segs] == [
        (130, 160),
        (200, 250),
    ]


def test_four_fence_group(client):
    f1, f2, f3, f4, f5 = create_fences(client)
    # 四个围栏一组：设备同时处于 f1、f2、f4（f3 未进入）-> 活跃集合按 ID 排序
    # 构造：让 f4 与 f1、f2 也重叠。直接复用 F4_GEO 太靠东，故新建一个覆盖重叠区的围栏
    cover = client.post("/fences", json={
        "name": "cover",
        "geometry": {"type": "Polygon", "coordinates": [
            [[0.005, 0.001], [0.012, 0.001], [0.012, 0.009], [0.005, 0.009],
             [0.005, 0.001]]]},
    })
    assert cover.status_code == 201, cover.text
    fc = cover.json()["id"]
    submit(client, [
        pt("d1", 110, ONLY1),
        pt("d1", 130, BOTH12),  # 同时处于 f1、f2、fc
        pt("d1", 200, OUTSIDE),
    ])
    task = create_task(client, [
        ([f1, f2, f3, fc], START_SEC, END_SEC),
        ([f4, f5], START_SEC, END_SEC),
    ])
    rep = report(client, task["id"])
    seg = rep["devices"][0]["groups"][0]["segments"][0]
    assert seg["active_fence_ids"] == sorted([f1, f2, fc])


def test_report_recomputed_after_backfill(client):
    f1, f2, f3, f4, f5 = create_fences(client)
    task = create_task(client, [
        ([f1, f2], START_SEC, END_SEC),
        ([f4, f5], START_SEC, END_SEC),
    ])
    # 初始：d1 只在 F1 -> 通过
    submit(client, [pt("d1", 110, ONLY1), pt("d1", 300, OUTSIDE)])
    rep = report(client, task["id"])
    assert rep["passed"] is True

    # 补录中间点使其进入重叠区 -> 出现冲突
    submit(client, [pt("d1", 150, BOTH12)])
    rep = report(client, task["id"])
    assert rep["passed"] is False
    seg = rep["devices"][0]["groups"][0]["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(150)

    # 再补点离开 F1（只在 F2）-> 冲突解除，旧片段不残留
    submit(client, [pt("d1", 200, ONLY2)])
    rep = report(client, task["id"])
    seg = rep["devices"][0]["groups"][0]["segments"][0]
    assert datetime.fromisoformat(seg["end"]) == dts(200)

    # 补一个更早的界外点，证明全量重算：d1 自窗口前在外，110 才进入 F1 不变；
    # 改为补点抹掉 150 的重叠（在 150 时刻放一个 ONLY1 的点会与原 150 点重复，
    # 故用新设备 d2 验证无快照污染）
    submit(client, [pt("d2", 150, BOTH12), pt("d2", 160, ONLY1)])
    rep = report(client, task["id"])
    devs = {d["device_id"]: d for d in rep["devices"]}
    assert set(devs) == {"d1", "d2"}
    d2seg = devs["d2"]["groups"][0]["segments"][0]
    assert datetime.fromisoformat(d2seg["start"]) == dts(150)
    assert datetime.fromisoformat(d2seg["end"]) == dts(160)


# ---------- 删除围栏清理关联任务 ----------


def test_delete_fence_cleans_isolation_tasks(client):
    f1, f2, f3, f4, f5 = create_fences(client)
    # 任务 A 引用 f1（组0）；任务 B 只引用 f4/f5
    task_a = create_task(client, [
        ([f1, f2], START_SEC, END_SEC),
        ([f3, f1], START_SEC, END_SEC),
    ], name="引用f1")
    task_b = create_task(client, [
        ([f4, f5], START_SEC, END_SEC),
        ([f4, f5], START_SEC, END_SEC),
    ], name="不引用f1")

    assert client.delete(f"/fences/{f1}").status_code == 204

    # 引用 f1 的任务随围栏整体删除
    assert client.get(f"/isolation-tasks/{task_a['id']}").status_code == 404
    assert client.get(f"/isolation-tasks/{task_a['id']}/report").status_code == 404
    # 不引用 f1 的任务保留
    rep = report(client, task_b["id"])
    assert rep["id"] == task_b["id"]

    # 删除剩余围栏也级联清空
    for fid in (f2, f3, f4, f5):
        assert client.delete(f"/fences/{fid}").status_code == 204
    assert client.get(f"/isolation-tasks/{task_b['id']}").status_code == 404
    assert client.get("/isolation-tasks").json() == []


def test_delete_fence_removes_group_fence_rows(client):
    from app.database import get_db
    from app.models import IsolationGroupFenceRow, IsolationGroupRow

    f1, f2, f3, f4, f5 = create_fences(client)
    create_task(client, [
        ([f1, f2], START_SEC, END_SEC),
        ([f3, f4], START_SEC, END_SEC),
    ])
    client.delete(f"/fences/{f1}")
    gen = get_db()
    db = next(gen)
    try:
        assert db.query(IsolationGroupFenceRow).count() == 0
        assert db.query(IsolationGroupRow).count() == 0
    finally:
        db.close()
