from datetime import datetime, timedelta, timezone

from app.engine import EVENT_ENTER, EVENT_LEAVE, compute_clearance

BASE = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)

PLAIN_FENCE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]],
}

INSIDE = (0.005, 0.005)
OUTSIDE = (0.02, 0.02)

START_SEC = 100
DEADLINE_SEC = 300


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
    r = client.post("/fences", json={"name": "清场测试", "geometry": PLAIN_FENCE})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def create_task(client, fence_id, name="台风清场", start=START_SEC, deadline=DEADLINE_SEC):
    r = client.post(
        f"/fences/{fence_id}/clearance-tasks",
        json={"name": name, "start_at": iso(start), "deadline": iso(deadline)},
    )
    assert r.status_code == 201, r.text
    return r.json()


def report(client, fence_id, task_id):
    r = client.get(f"/fences/{fence_id}/clearance-tasks/{task_id}/report")
    assert r.status_code == 200, r.text
    return r.json()


def by_device(rep):
    return {d["device_id"]: d for d in rep["devices"]}


def test_create_task_validation_and_crud(client):
    fence_id = create_fence(client)

    # 围栏不存在 -> 404
    r = client.post(
        "/fences/999/clearance-tasks",
        json={"name": "x", "start_at": iso(0), "deadline": iso(10)},
    )
    assert r.status_code == 404

    # 开始必须早于截止：相等、倒置均 422
    for s, d in ((100, 100), (200, 100)):
        r = client.post(
            f"/fences/{fence_id}/clearance-tasks",
            json={"name": "bad", "start_at": iso(s), "deadline": iso(d)},
        )
        assert r.status_code == 422

    task = create_task(client, fence_id, name="应急清场A")
    assert task["fence_id"] == fence_id
    assert task["name"] == "应急清场A"
    assert datetime.fromisoformat(task["start_at"]) == dts(START_SEC)
    assert datetime.fromisoformat(task["deadline"]) == dts(DEADLINE_SEC)

    # 同名任务允许（仅名称非唯一约束），列表按 id 升序
    create_task(client, fence_id, name="应急清场A")
    tasks = client.get(f"/fences/{fence_id}/clearance-tasks").json()
    assert [t["name"] for t in tasks] == ["应急清场A", "应急清场A"]

    # 删除；围栏不存在/任务不属于该围栏 -> 404
    tid = tasks[0]["id"]
    assert client.delete(f"/fences/{fence_id}/clearance-tasks/{tid}").status_code == 204
    assert client.delete(f"/fences/{fence_id}/clearance-tasks/{tid}").status_code == 404
    assert client.get(f"/fences/{fence_id}/clearance-tasks/{tid}/report").status_code == 404
    # 任务不属于该围栏 -> 404
    other = create_fence(client)
    assert client.delete(
        f"/fences/{other}/clearance-tasks/{tasks[1]['id']}"
    ).status_code == 404
    assert client.get(
        f"/fences/{other}/clearance-tasks/{tasks[1]['id']}/report"
    ).status_code == 404


def test_report_half_open_boundaries(client):
    fence_id = create_fence(client)
    submit(client, [
        pt("a-enter-exact", 100), pt("a-enter-exact", 200, inside=False),   # start 处 enter -> 计入
        pt("b-leave-exact", 0), pt("b-leave-exact", 100, inside=False),     # start 处 leave -> 排除
        pt("c-enter-after", 150), pt("c-enter-after", 200, inside=False),   # 窗口内才进入 -> 不核对
        pt("d-leave-deadline", 50), pt("d-leave-deadline", 300, inside=False),  # deadline 处 leave -> 已清
        pt("e-enter-deadline", 0), pt("e-enter-deadline", 250, inside=False),
        pt("e-enter-deadline", 300),                                        # deadline 处又 enter -> 未清
        pt("f-still-inside", 0),                                            # 截止仍在场
        pt("g-cleared-early", 90), pt("g-cleared-early", 120, inside=False),
    ])

    task = create_task(client, fence_id)
    rep = report(client, fence_id, task["id"])

    assert rep["summary"] == {
        "start_count": 5,
        "cleared_count": 3,
        "remaining_count": 2,
    }
    devices = by_device(rep)
    assert list(devices) == [
        "a-enter-exact",
        "d-leave-deadline",
        "e-enter-deadline",
        "f-still-inside",
        "g-cleared-early",
    ]
    assert "b-leave-exact" not in devices
    assert "c-enter-after" not in devices

    assert devices["a-enter-exact"]["status"] == "cleared"
    assert datetime.fromisoformat(devices["a-enter-exact"]["last_leave_at"]) == dts(200)
    assert devices["d-leave-deadline"]["status"] == "cleared"
    assert datetime.fromisoformat(devices["d-leave-deadline"]["last_leave_at"]) == dts(300)
    assert devices["g-cleared-early"]["status"] == "cleared"
    assert datetime.fromisoformat(devices["g-cleared-early"]["last_leave_at"]) == dts(120)

    assert devices["e-enter-deadline"]["status"] == "remaining"
    assert devices["e-enter-deadline"]["last_leave_at"] is None
    assert devices["f-still-inside"]["status"] == "remaining"
    assert devices["f-still-inside"]["last_leave_at"] is None


def test_leave_and_reenter_must_leave_again(client):
    fence_id = create_fence(client)
    submit(client, [
        # 离开后再进入，截止前未再离开 -> remaining
        pt("reenter-remains", 0), pt("reenter-remains", 150, inside=False),
        pt("reenter-remains", 200),
        # 离开后再进入，再次离开并持续在外 -> cleared，最后有效离开取第二次
        pt("reenter-cleared", 0), pt("reenter-cleared", 150, inside=False),
        pt("reenter-cleared", 200), pt("reenter-cleared", 260, inside=False),
    ])

    task = create_task(client, fence_id)
    rep = report(client, fence_id, task["id"])
    assert rep["summary"] == {"start_count": 2, "cleared_count": 1, "remaining_count": 1}
    devices = by_device(rep)
    assert devices["reenter-remains"]["status"] == "remaining"
    assert devices["reenter-remains"]["last_leave_at"] is None
    assert devices["reenter-cleared"]["status"] == "cleared"
    assert datetime.fromisoformat(
        devices["reenter-cleared"]["last_leave_at"]
    ) == dts(260)


def test_report_rebuilt_from_current_events_after_backfill(client):
    fence_id = create_fence(client)
    task = create_task(client, fence_id)

    # dev1：表面上 t=200 离开且未回；dev2：场内未离开
    submit(client, [
        pt("dev1", 0), pt("dev1", 200, inside=False),
        pt("dev2", 0),
    ])
    rep = report(client, fence_id, task["id"])
    assert rep["summary"] == {"start_count": 2, "cleared_count": 1, "remaining_count": 1}
    devices = by_device(rep)
    assert devices["dev1"]["status"] == "cleared"
    assert datetime.fromisoformat(devices["dev1"]["last_leave_at"]) == dts(200)

    # 补录乱序点：dev1 在 t=250 又进入且持续到截止 -> 变为 remaining
    submit(client, [pt("dev1", 250)])
    rep = report(client, fence_id, task["id"])
    assert rep["summary"] == {"start_count": 2, "cleared_count": 0, "remaining_count": 2}
    devices = by_device(rep)
    assert devices["dev1"]["status"] == "remaining"
    assert devices["dev1"]["last_leave_at"] is None

    # 再补录：dev1 在截止前再次离开 -> 重新变为 cleared，最后离开更新
    submit(client, [pt("dev1", 280, inside=False)])
    rep = report(client, fence_id, task["id"])
    assert rep["summary"] == {"start_count": 2, "cleared_count": 1, "remaining_count": 1}
    devices = by_device(rep)
    assert devices["dev1"]["status"] == "cleared"
    assert datetime.fromisoformat(devices["dev1"]["last_leave_at"]) == dts(280)

    # 补录更早的点：dev3 实际在 start 之前已在场 -> 起始名单同步扩大
    submit(client, [pt("dev3", 50), pt("dev3", 150, inside=False)])
    rep = report(client, fence_id, task["id"])
    assert rep["summary"]["start_count"] == 3
    assert rep["summary"]["cleared_count"] == 2
    devices = by_device(rep)
    assert "dev3" in devices and devices["dev3"]["status"] == "cleared"


def test_empty_start_roster(client):
    fence_id = create_fence(client)
    task = create_task(client, fence_id)
    # 无任何事件
    rep = report(client, fence_id, task["id"])
    assert rep["devices"] == []
    assert rep["summary"] == {"start_count": 0, "cleared_count": 0, "remaining_count": 0}

    # 只有窗口内才进入的设备，同样不进名单
    submit(client, [pt("late", 150), pt("late", 200, inside=False)])
    rep = report(client, fence_id, task["id"])
    assert rep["devices"] == []
    assert rep["summary"]["start_count"] == 0


def test_delete_fence_removes_clearance_tasks(client):
    fence_id = create_fence(client)
    create_task(client, fence_id)
    assert client.delete(f"/fences/{fence_id}").status_code == 204

    # 围栏下任务列表随围栏删除返回 404；新建同 id 围栏时任务表为空
    assert client.get(f"/fences/{fence_id}/clearance-tasks").status_code == 404
    new_id = create_fence(client)
    assert client.get(f"/fences/{new_id}/clearance-tasks").json() == []


# ---------- 引擎级：同刻 leave+enter 离开先重放 ----------


def test_engine_same_instant_leave_enter_leave_first():
    # 窗口内 t=150 同刻离开又进入，新段持续到截止未再离开 -> remaining；
    # enter 先重放会吞掉新段并误报 cleared。
    events = [
        ("d", EVENT_ENTER, 0), ("d", EVENT_LEAVE, 150),
        ("d", EVENT_ENTER, 150),
    ]
    rep = compute_clearance(events, 100, 300)
    assert rep.start_count == 1
    assert rep.results[0].status == "remaining"
    assert rep.results[0].last_leave_ts is None

    # 同刻折返后再次离开：新段止端 260 才是最后有效离开时刻
    events = [
        ("d", EVENT_ENTER, 0), ("d", EVENT_LEAVE, 150),
        ("d", EVENT_ENTER, 150), ("d", EVENT_LEAVE, 260),
    ]
    rep = compute_clearance(events, 100, 300)
    assert rep.results[0].status == "cleared"
    assert rep.results[0].last_leave_ts == 260

    # start 处同刻 leave+enter：连续在场，应进入起始名单并按时清场
    events = [
        ("d", EVENT_ENTER, 0), ("d", EVENT_LEAVE, 100),
        ("d", EVENT_ENTER, 100), ("d", EVENT_LEAVE, 200),
    ]
    rep = compute_clearance(events, 100, 300)
    assert rep.start_count == 1
    assert rep.results[0].status == "cleared"
    assert rep.results[0].last_leave_ts == 200

    # deadline 处同刻 leave+enter：新区间起端含截止刻 -> remaining
    events = [
        ("d", EVENT_ENTER, 0), ("d", EVENT_LEAVE, 300),
        ("d", EVENT_ENTER, 300),
    ]
    rep = compute_clearance(events, 100, 300)
    assert rep.results[0].status == "remaining"
