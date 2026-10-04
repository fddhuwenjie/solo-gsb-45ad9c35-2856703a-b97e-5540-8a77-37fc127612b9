from datetime import datetime, timedelta, timezone

import pytest

from app.engine import (
    EVENT_ENTER,
    EVENT_LEAVE,
    compute_rendezvous_report,
)

BASE = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)

PLAIN_FENCE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]],
}

INSIDE = (0.005, 0.005)
OUTSIDE = (0.02, 0.02)

START_SEC = 100
END_SEC = 400
MIN_SEC = 60.0


def pt(device, sec, inside=True):
    lat, lon = INSIDE if inside else OUTSIDE
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


def create_fence(client):
    r = client.post("/fences", json={"name": "协同测试", "geometry": PLAIN_FENCE})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def create_task(
    client,
    fence_id,
    members=("d1", "d2"),
    name="班前协同",
    start=START_SEC,
    end=END_SEC,
    min_sec=MIN_SEC,
):
    r = client.post(
        f"/fences/{fence_id}/rendezvous-tasks",
        json={
            "name": name,
            "members": list(members),
            "start_at": iso(start),
            "end_at": iso(end),
            "min_co_presence_s": min_sec,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def report(client, fence_id, task_id):
    r = client.get(f"/fences/{fence_id}/rendezvous-tasks/{task_id}/report")
    assert r.status_code == 200, r.text
    return r.json()


def test_create_task_validation_and_crud(client):
    fence_id = create_fence(client)

    # 围栏不存在 -> 404
    r = client.post(
        f"/fences/999/rendezvous-tasks",
        json={
            "name": "x",
            "members": ["a", "b"],
            "start_at": iso(0),
            "end_at": iso(10),
            "min_co_presence_s": 5,
        },
    )
    assert r.status_code == 404

    def bad(body_mod):
        body = {
            "name": "bad",
            "members": ["a", "b"],
            "start_at": iso(0),
            "end_at": iso(10),
            "min_co_presence_s": 5,
        }
        body.update(body_mod)
        return client.post(f"/fences/{fence_id}/rendezvous-tasks", json=body)

    # 成员数量必须 2～8
    assert bad({"members": ["a"]}).status_code == 422
    assert bad({"members": [f"d{i}" for i in range(9)]}).status_code == 422
    assert bad({"members": ["a", "a"]}).status_code == 422          # 不得重复
    assert bad({"start_at": iso(10), "end_at": iso(10)}).status_code == 422  # 窗口无效
    assert bad({"start_at": iso(20), "end_at": iso(10)}).status_code == 422
    assert bad({"min_co_presence_s": 0}).status_code == 422         # 时长必须为正
    assert bad({"min_co_presence_s": -1}).status_code == 422
    assert bad({"name": ""}).status_code == 422

    task = create_task(client, fence_id, members=("d2", "d1"), name="协同A")
    assert task["fence_id"] == fence_id
    assert task["members"] == ["d2", "d1"]  # 成员按提交顺序保存
    assert task["min_co_presence_s"] == MIN_SEC
    assert datetime.fromisoformat(task["start_at"]) == dts(START_SEC)
    assert datetime.fromisoformat(task["end_at"]) == dts(END_SEC)

    # 列表按 id 升序
    create_task(client, fence_id, members=("d3", "d4"), name="协同B")
    tasks = client.get(f"/fences/{fence_id}/rendezvous-tasks").json()
    assert [t["name"] for t in tasks] == ["协同A", "协同B"]

    # 删除；重复删除与跨围栏访问 -> 404
    tid = tasks[0]["id"]
    assert client.delete(f"/fences/{fence_id}/rendezvous-tasks/{tid}").status_code == 204
    assert client.delete(f"/fences/{fence_id}/rendezvous-tasks/{tid}").status_code == 404
    assert client.get(
        f"/fences/{fence_id}/rendezvous-tasks/{tid}/report"
    ).status_code == 404
    other = create_fence(client)
    assert client.get(
        f"/fences/{other}/rendezvous-tasks/{tasks[1]['id']}/report"
    ).status_code == 404


def test_report_passing_segment(client):
    fence_id = create_fence(client)
    # d1 [50,300)，d2 [120,500)，共同在场 [120,300) = 180s
    submit(client, [
        pt("d1", 50), pt("d1", 300, inside=False),
        pt("d2", 120), pt("d2", 500, inside=False),
    ])
    task = create_task(client, fence_id, members=("d1", "d2"))
    rep = report(client, fence_id, task["id"])

    assert rep["passed"] is True
    assert len(rep["segments"]) == 1
    seg = rep["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(120)
    assert datetime.fromisoformat(seg["end"]) == dts(300)
    assert seg["duration_s"] == pytest.approx(180.0)
    assert seg["members"] == ["d1", "d2"]  # 片段内成员按 ID 排序


def test_report_no_qualified_segment(client):
    fence_id = create_fence(client)
    # d1 [50,150)，d2 [120,200)，共同在场 [120,150) = 30s < 60s
    submit(client, [
        pt("d1", 50), pt("d1", 150, inside=False),
        pt("d2", 120), pt("d2", 200, inside=False),
    ])
    task = create_task(client, fence_id)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    assert rep["segments"] == []


def test_half_open_and_window_boundaries(client):
    fence_id = create_fence(client)

    # 持续时长恰好等于门槛 -> 通过（含等于）
    submit(client, [
        pt("eq1", START_SEC), pt("eq1", 200, inside=False),
        pt("eq2", START_SEC), pt("eq2", START_SEC + 60, inside=False),
    ])
    task = create_task(client, fence_id, members=("eq1", "eq2"))
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert rep["segments"][0]["duration_s"] == pytest.approx(60.0)

    # 差 1 秒 -> 失败
    submit(client, [
        pt("sh1", START_SEC), pt("sh1", 200, inside=False),
        pt("sh2", START_SEC), pt("sh2", START_SEC + 59, inside=False),
    ])
    task = create_task(client, fence_id, members=("sh1", "sh2"))
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False

    # 恰在窗口结束离开：[start,end) 计满整窗；end 处 leave 不含在区间内
    submit(client, [
        pt("we1", 0), pt("we1", END_SEC, inside=False),
        pt("we2", 0), pt("we2", END_SEC, inside=False),
    ])
    task = create_task(client, fence_id, members=("we1", "we2"), min_sec=300)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    seg = rep["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(START_SEC)
    assert datetime.fromisoformat(seg["end"]) == dts(END_SEC)
    assert seg["duration_s"] == pytest.approx(300.0)

    # 一方在窗口起点离开（leave@start 不计入），窗口中途才返回 -> 只有后续片段
    submit(client, [
        pt("ls-a", 0), pt("ls-a", END_SEC, inside=False),
        pt("ls-b", 0), pt("ls-b", START_SEC, inside=False),
        pt("ls-b", 200), pt("ls-b", 300, inside=False),
    ])
    task = create_task(client, fence_id, members=("ls-a", "ls-b"), min_sec=60)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    seg = rep["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(200)
    assert datetime.fromisoformat(seg["end"]) == dts(300)


def test_repeated_in_out_multiple_candidates(client):
    fence_id = create_fence(client)
    # d1 全窗在场；d2：[0,140) 与 [200,300) 两个区间
    # 窗口内候选 [100,140)=40s（不足）、[200,300)=100s（达标）
    submit(client, [
        pt("d1", 0), pt("d1", 500, inside=False),
        pt("d2", 0), pt("d2", 140, inside=False),
        pt("d2", 200), pt("d2", 300, inside=False),
    ])
    task = create_task(client, fence_id)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert len(rep["segments"]) == 1
    seg = rep["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(200)
    assert datetime.fromisoformat(seg["end"]) == dts(300)
    assert seg["duration_s"] == pytest.approx(100.0)

    # 提高门槛到 120s：两个候选都不足 -> 失败
    task2 = create_task(client, fence_id, name="严格协同", min_sec=120)
    rep = report(client, fence_id, task2["id"])
    assert rep["passed"] is False
    assert rep["segments"] == []


def test_all_members_required(client):
    fence_id = create_fence(client)
    # d1、d2 在场一整窗，d3 从未来过
    submit(client, [
        pt("d1", 0), pt("d1", 500, inside=False),
        pt("d2", 0), pt("d2", 500, inside=False),
    ])
    task = create_task(client, fence_id, members=("d1", "d2", "d3"))
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    assert rep["segments"] == []

    # d3 只在 [200,230) 短暂到场 30s（< 60s 门槛）-> 失败
    submit(client, [pt("d3", 200), pt("d3", 230, inside=False)])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    # d3 第二段 [260,300) 与 d2 同时结束，40s 仍不足 -> 失败
    submit(client, [pt("d3", 260), pt("d3", 300, inside=False)])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    # d3 再到 [330,420)，但 d2 原先在 500 才离开的区间被 400 的界外点截断为
    # leave@400：窗内三人交集 [330,400)=70s -> 达标
    submit(client, [
        pt("d3", 330), pt("d3", 420, inside=False),
        pt("d2", 350), pt("d2", 420, inside=False),
    ])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert len(rep["segments"]) == 1
    seg = rep["segments"][0]
    assert datetime.fromisoformat(seg["start"]) == dts(330)
    assert datetime.fromisoformat(seg["end"]) == dts(400)
    assert seg["members"] == ["d1", "d2", "d3"]


def test_report_rebuilt_after_backfill(client):
    fence_id = create_fence(client)
    task = create_task(client, fence_id)

    # 初次：共同在场只有 [100,140)=40s -> 失败
    submit(client, [
        pt("d1", 0), pt("d1", 500, inside=False),
        pt("d2", 0), pt("d2", 140, inside=False),
    ])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    assert rep["segments"] == []

    # 补录乱序点（晚提交的中间轨迹）：d2 在 [200,300) 又在场 -> 出现达标片段
    submit(client, [
        pt("d2", 200), pt("d2", 300, inside=False),
    ])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert len(rep["segments"]) == 1
    assert datetime.fromisoformat(rep["segments"][0]["start"]) == dts(200)

    # 再补录：d1 在 250 离开、290 才回，原达标片段被打断为两段不足片段 -> 重新失败，
    # 旧的 [200,300) 结果不得残留
    submit(client, [
        pt("d1", 250, inside=False),
        pt("d1", 290),
    ])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    assert rep["segments"] == []

    # 补录更早的点：d3 实际自窗口前在场并停留到 380。三人共同候选：
    # [100,140)=40s、[200,250)=50s、[290,300)=10s，门槛 55s 时仍失败
    submit(client, [pt("d3", 0), pt("d3", 380, inside=False)])
    task3 = create_task(client, fence_id, members=("d1", "d2", "d3"), min_sec=55)
    rep = report(client, fence_id, task3["id"])
    assert rep["passed"] is False
    assert rep["segments"] == []


def test_events_scoped_to_fence(client):
    fence_id = create_fence(client)
    # 另一处围栏（几何中心远离本围栏，两台设备对它都在界外）
    r = client.post(
        "/fences",
        json={
            "name": "远端围栏",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[1.0, 1.0], [1.01, 1.0], [1.01, 1.01], [1.0, 1.01], [1.0, 1.0]]
                ],
            },
        },
    )
    assert r.status_code == 201, r.text
    other = r.json()["id"]

    submit(client, [
        pt("d1", 0), pt("d1", 500, inside=False),
        pt("d2", 0), pt("d2", 500, inside=False),
    ])
    # 本围栏：两人共同在场一整窗 -> 通过
    task = create_task(client, fence_id, members=("d1", "d2"), min_sec=1)
    assert report(client, fence_id, task["id"])["passed"] is True

    # 远端围栏：两人从无进入事件 -> 失败；事件按 fence_id 隔离
    task_other = create_task(client, other, members=("d1", "d2"), min_sec=1)
    rep = report(client, other, task_other["id"])
    assert rep["passed"] is False
    assert rep["segments"] == []

    # 不存在的成员（从无轨迹）同样不达标
    task_missing = create_task(client, fence_id, members=("d1", "d4"), min_sec=1)
    assert report(client, fence_id, task_missing["id"])["passed"] is False


def test_delete_fence_removes_rendezvous_tasks(client):
    fence_id = create_fence(client)
    create_task(client, fence_id)
    create_task(client, fence_id, members=("d3", "d4"), name="协同B")
    assert client.delete(f"/fences/{fence_id}").status_code == 204

    # 围栏下任务列表随围栏删除返回 404；新建同 id 围栏时任务表为空
    assert client.get(f"/fences/{fence_id}/rendezvous-tasks").status_code == 404
    new_id = create_fence(client)
    assert client.get(f"/fences/{new_id}/rendezvous-tasks").json() == []


# ---------- 引擎级边界：同刻 enter/leave（半开） ----------


def seg_ts(events, members, start, end, need):
    r = compute_rendezvous_report(events, members, start, end, need)
    return r.passed, [(s.start, s.end) for s in r.segments]


def test_engine_same_instant_enter_leave_half_open():
    # 成员 b 在 t=20 同时 leave 与 enter：旧区间止端不含 20、新区间起端含 20，
    # 共同在场视为连续不断点
    events = [
        ("a", EVENT_ENTER, 0), ("a", EVENT_LEAVE, 100),
        ("b", EVENT_ENTER, 0), ("b", EVENT_LEAVE, 20),
        ("b", EVENT_ENTER, 20), ("b", EVENT_LEAVE, 100),
    ]
    passed, segs = seg_ts(events, ["a", "b"], 0, 100, 100)
    assert passed is True
    assert segs == [(0, 100)]

    # 两成员在同一刻交接：a leave@30、b enter@30 -> 该刻前后都不全员，无片段
    events = [
        ("a", EVENT_ENTER, 0), ("a", EVENT_LEAVE, 30),
        ("b", EVENT_ENTER, 30), ("b", EVENT_LEAVE, 100),
    ]
    passed, segs = seg_ts(events, ["a", "b"], 0, 100, 0)
    assert passed is False
    assert segs == []

    # leave 恰落在门槛时刻：[10,40) 恰 30s 达标
    events = [
        ("a", EVENT_ENTER, 10), ("a", EVENT_LEAVE, 40),
        ("b", EVENT_ENTER, 10), ("b", EVENT_LEAVE, 40),
    ]
    passed, segs = seg_ts(events, ["a", "b"], 0, 100, 30)
    assert passed is True
    assert segs == [(10, 40)]
