from datetime import datetime, timedelta, timezone

import pytest

from app.engine import (
    EVENT_ENTER,
    EVENT_LEAVE,
    compute_handover_report,
)

BASE = datetime(2026, 10, 2, 8, 0, 0, tzinfo=timezone.utc)

PLAIN_FENCE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]],
}

INSIDE = (0.005, 0.005)
OUTSIDE = (0.02, 0.02)

START_SEC = 100
END_SEC = 400
MAX_GAP = 30.0
MIN_OVERLAP = 30.0


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
    r = client.post("/fences", json={"name": "交班测试", "geometry": PLAIN_FENCE})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def create_task(
    client,
    fence_id,
    members=("d0", "d1", "d2"),
    name="三班接力",
    start=START_SEC,
    end=END_SEC,
    max_gap=MAX_GAP,
    min_overlap=MIN_OVERLAP,
):
    r = client.post(
        f"/fences/{fence_id}/handover-tasks",
        json={
            "name": name,
            "members": list(members),
            "start_at": iso(start),
            "end_at": iso(end),
            "max_gap_s": max_gap,
            "min_overlap_s": min_overlap,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def report(client, fence_id, task_id):
    r = client.get(f"/fences/{fence_id}/handover-tasks/{task_id}/report")
    assert r.status_code == 200, r.text
    return r.json()


def test_create_task_validation_and_crud(client):
    fence_id = create_fence(client)

    # 围栏不存在 -> 404
    r = client.post(
        f"/fences/999/handover-tasks",
        json={
            "name": "x",
            "members": ["a", "b"],
            "start_at": iso(0),
            "end_at": iso(10),
            "max_gap_s": 5,
            "min_overlap_s": 5,
        },
    )
    assert r.status_code == 404

    def bad(body_mod):
        body = {
            "name": "bad",
            "members": ["a", "b"],
            "start_at": iso(0),
            "end_at": iso(10),
            "max_gap_s": 5,
            "min_overlap_s": 5,
        }
        body.update(body_mod)
        return client.post(f"/fences/{fence_id}/handover-tasks", json=body)

    # 设备数量必须 2～8
    assert bad({"members": ["a"]}).status_code == 422
    assert bad({"members": [f"d{i}" for i in range(9)]}).status_code == 422
    assert bad({"members": ["a", "a"]}).status_code == 422          # 不得重复
    assert bad({"members": ["a", "  "]}).status_code == 422         # 不得为空
    assert bad({"start_at": iso(10), "end_at": iso(10)}).status_code == 422  # 窗口无效
    assert bad({"start_at": iso(20), "end_at": iso(10)}).status_code == 422
    assert bad({"max_gap_s": -1}).status_code == 422                # 空档上限非负
    assert bad({"min_overlap_s": -1}).status_code == 422            # 最短交叠非负
    # min_overlap_s=0 合法：只要两段真正相交即达标（契约与引擎判定一致）
    created = bad({"min_overlap_s": 0})
    assert created.status_code == 201
    assert client.delete(
        f"/fences/{fence_id}/handover-tasks/{created.json()['id']}"
    ).status_code == 204
    assert bad({"name": ""}).status_code == 422

    task = create_task(client, fence_id, members=("d2", "d0", "d1"), name="交班A")
    assert task["fence_id"] == fence_id
    assert task["members"] == ["d2", "d0", "d1"]  # 按交班顺序保存
    assert task["max_gap_s"] == MAX_GAP
    assert task["min_overlap_s"] == MIN_OVERLAP
    assert datetime.fromisoformat(task["start_at"]) == dts(START_SEC)
    assert datetime.fromisoformat(task["end_at"]) == dts(END_SEC)

    # 列表按 id 升序
    create_task(client, fence_id, members=("d3", "d4"), name="交班B")
    tasks = client.get(f"/fences/{fence_id}/handover-tasks").json()
    assert [t["name"] for t in tasks] == ["交班A", "交班B"]

    # 删除；重复删除与跨围栏访问 -> 404
    tid = tasks[0]["id"]
    assert client.delete(f"/fences/{fence_id}/handover-tasks/{tid}").status_code == 204
    assert client.delete(f"/fences/{fence_id}/handover-tasks/{tid}").status_code == 404
    assert client.get(
        f"/fences/{fence_id}/handover-tasks/{tid}/report"
    ).status_code == 404
    other = create_fence(client)
    assert client.get(
        f"/fences/{other}/handover-tasks/{tasks[1]['id']}/report"
    ).status_code == 404


def test_passing_overlap_chain(client):
    fence_id = create_fence(client)
    # d0 [50,200)、d1 [180,300)（交叠 20s<30，不达标）；d1 另有 [150,300) 通过
    # 这里直接构造达标链：d0[50,200) d1[170,300) 交叠30 d2[270,400) 交叠30
    submit(client, [
        pt("d0", 50), pt("d0", 200, inside=False),
        pt("d1", 170), pt("d1", 300, inside=False),
        pt("d2", 270), pt("d2", 400, inside=False),
    ])
    task = create_task(client, fence_id)
    rep = report(client, fence_id, task["id"])

    assert rep["passed"] is True
    assert rep["passed_steps"] == 2
    assert rep["first_violation_at"] is None
    assert [s["device_id"] for s in rep["steps"]] == ["d0", "d1", "d2"]
    assert [s["status"] for s in rep["steps"]] == ["passed"] * 3
    s1 = rep["steps"][1]
    assert datetime.fromisoformat(s1["interval_start"]) == dts(170)
    assert datetime.fromisoformat(s1["interval_end"]) == dts(300)
    assert datetime.fromisoformat(s1["takeover_at"]) == dts(170)
    assert s1["violation_at"] is None
    s2 = rep["steps"][2]
    assert datetime.fromisoformat(s2["takeover_at"]) == dts(270)


def test_passing_gap_chain_separated(client):
    fence_id = create_fence(client)
    # d0[50,150) d1[170,250) 空档 20<=30；d1->d2 [250,280) 空档 0（首尾相接）
    submit(client, [
        pt("d0", 50), pt("d0", 150, inside=False),
        pt("d1", 170), pt("d1", 250, inside=False),
        pt("d2", 250), pt("d2", 320, inside=False),
    ])
    task = create_task(client, fence_id, min_overlap=1)  # 本例无交叠，门槛设小不误伤
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert rep["passed_steps"] == 2
    assert datetime.fromisoformat(rep["steps"][2]["interval_start"]) == dts(250)


def test_gap_exceeded_stops_chain(client):
    fence_id = create_fence(client)
    # d0[50,150)，d1 仅 [200,300)：空档 50 > 30 -> gap_exceeded，d2 不再交班
    submit(client, [
        pt("d0", 50), pt("d0", 150, inside=False),
        pt("d1", 200), pt("d1", 300, inside=False),
        pt("d2", 250), pt("d2", 350, inside=False),
    ])
    task = create_task(client, fence_id, min_overlap=1)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    assert rep["passed_steps"] == 0
    assert rep["steps"][0]["status"] == "passed"
    fail = rep["steps"][1]
    assert fail["status"] == "gap_exceeded"
    # 首个违规时刻 = d0 止端 150 + 最大空档 30 = 180
    assert datetime.fromisoformat(fail["violation_at"]) == dts(180)
    assert datetime.fromisoformat(fail["interval_start"]) == dts(200)
    assert rep["steps"][2]["status"] == "not_reached"
    assert rep["steps"][2]["violation_at"] is None
    assert datetime.fromisoformat(rep["first_violation_at"]) == dts(180)

    # 放宽空档上限到 50（含等于）：d0->d1 达标；d1[200,300)->d2[250,350) 交叠50达标
    task2 = create_task(client, fence_id, name="宽松交班", max_gap=50, min_overlap=30)
    rep2 = report(client, fence_id, task2["id"])
    assert rep2["passed"] is True
    assert rep2["passed_steps"] == 2


def test_overlap_insufficient_stops_chain(client):
    fence_id = create_fence(client)
    # d0[50,200) d1[180,300) 交叠 20 < 30 -> overlap_insufficient，违规时刻取交叠结束 200
    submit(client, [
        pt("d0", 50), pt("d0", 200, inside=False),
        pt("d1", 180), pt("d1", 300, inside=False),
        pt("d2", 250), pt("d2", 350, inside=False),
    ])
    task = create_task(client, fence_id)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    fail = rep["steps"][1]
    assert fail["status"] == "overlap_insufficient"
    assert datetime.fromisoformat(fail["violation_at"]) == dts(200)
    assert datetime.fromisoformat(fail["interval_start"]) == dts(180)
    assert rep["steps"][2]["status"] == "not_reached"

    # 交叠恰好等于门槛（d1 提前到 170，交叠 30）-> 通过
    submit(client, [pt("d1", 170)])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert rep["steps"][1]["status"] == "passed"


def test_absent_successor_and_first_absent(client):
    fence_id = create_fence(client)
    # d0[50,200)，d1 从无轨迹 -> absent（违规时刻取窗口结束），d2 not_reached
    submit(client, [
        pt("d0", 50), pt("d0", 200, inside=False),
        pt("d2", 250), pt("d2", 350, inside=False),
    ])
    task = create_task(client, fence_id)
    rep = report(client, fence_id, task["id"])
    assert rep["steps"][0]["status"] == "passed"
    assert rep["steps"][1]["status"] == "absent"
    assert rep["steps"][1]["interval_start"] is None
    assert datetime.fromisoformat(rep["steps"][1]["violation_at"]) == dts(END_SEC)
    assert rep["steps"][2]["status"] == "not_reached"

    # 首台设备即缺席：违规时刻为窗口开始
    task2 = create_task(client, fence_id, members=("x0", "x1"), name="无人接班")
    rep2 = report(client, fence_id, task2["id"])
    assert rep2["passed"] is False
    assert rep2["passed_steps"] == 0
    assert rep2["steps"][0]["status"] == "absent"
    assert datetime.fromisoformat(rep2["steps"][0]["violation_at"]) == dts(START_SEC)
    assert rep2["steps"][1]["status"] == "not_reached"


def test_intervals_clipped_to_window(client):
    fence_id = create_fence(client)
    # d0 窗口前已进入且窗口结束仍未离开；d1 窗口内进入、跨过结束
    submit(client, [
        pt("d0", 0),
        pt("d1", 200),
    ])
    task = create_task(client, fence_id, members=("d0", "d1"))
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    s0, s1 = rep["steps"]
    assert datetime.fromisoformat(s0["interval_start"]) == dts(START_SEC)
    assert datetime.fromisoformat(s0["interval_end"]) == dts(END_SEC)
    assert datetime.fromisoformat(s1["interval_start"]) == dts(200)
    assert datetime.fromisoformat(s1["interval_end"]) == dts(END_SEC)


def test_repeated_in_out_selects_longest_chain(client):
    fence_id = create_fence(client)
    # d0 全窗在场；d1 两段：[120,150)（与 d0 交叠但接不上 d2）与 [200,300)；
    # d2[290,380)。经 d1[120,150) 后 d2 空档 140 超限；经 d1[200,300) 交叠 d2 10s。
    submit(client, [
        pt("d0", 0), pt("d0", 450, inside=False),
        pt("d1", 120), pt("d1", 150, inside=False),
        pt("d1", 200), pt("d1", 300, inside=False),
        pt("d2", 290), pt("d2", 380, inside=False),
    ])
    task = create_task(client, fence_id, max_gap=30, min_overlap=10)
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert rep["passed_steps"] == 2
    # 选择能延伸到 d2 的 d1 第二段
    assert datetime.fromisoformat(rep["steps"][1]["interval_start"]) == dts(200)


def test_tie_break_lex_earliest_takeover(client):
    fence_id = create_fence(client)
    # d0 两段 [50,120)、[150,260)；d1 两段 [100,140) 与 [200,300)。
    # 长度为 1 的候选链有两条：d0[50,120)->d1[100,140)（接班 100）与
    # d0[150,260)->d1[200,300)（接班 200）；交班步数并列，取接班时刻字典序
    # 最早的前者（首台到岗也最早）。
    submit(client, [
        pt("d0", 50), pt("d0", 120, inside=False),
        pt("d0", 150), pt("d0", 260, inside=False),
        pt("d1", 100), pt("d1", 140, inside=False),
        pt("d1", 200), pt("d1", 300, inside=False),
    ])
    task = create_task(
        client, fence_id, members=("d0", "d1"), min_overlap=20, start=0, end=400
    )
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert rep["passed_steps"] == 1
    assert datetime.fromisoformat(rep["steps"][0]["interval_start"]) == dts(50)
    assert datetime.fromisoformat(rep["steps"][1]["takeover_at"]) == dts(100)


def test_report_rebuilt_after_backfill(client):
    fence_id = create_fence(client)
    task = create_task(client, fence_id, members=("d0", "d1"), min_overlap=30)

    # 初次：d0[50,200)，d1[180,260) 交叠 20s -> 交叠不足
    submit(client, [
        pt("d0", 50), pt("d0", 200, inside=False),
        pt("d1", 180), pt("d1", 260, inside=False),
    ])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    assert rep["steps"][1]["status"] == "overlap_insufficient"
    # 首个违规时刻 = 交叠结束 200
    assert datetime.fromisoformat(rep["first_violation_at"]) == dts(200)

    # 补录乱序点：d1 在 170 已进入（早于先前首点），交叠变 30s -> 通过
    submit(client, [pt("d1", 170)])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert datetime.fromisoformat(rep["steps"][1]["interval_start"]) == dts(170)
    # 违规已随重算清除，首个违规时刻同步回到 null（旧结果不残留）
    assert rep["first_violation_at"] is None

    # 再补录：d0 在 175 离开后 190 才回，d1[170,260) 与 d0 首段 [50,175) 交叠仅 5s，
    # 与 d0 次段 [190,...) 比较时接班时刻不得早于 d0 次段到岗 -> 无满足时间推进的交叠，
    # 旧通过结果不得残留
    submit(client, [
        pt("d0", 175, inside=False),
        pt("d0", 190),
        pt("d0", 300, inside=False),
    ])
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is False
    assert rep["steps"][1]["status"] in {"overlap_insufficient", "gap_exceeded", "absent"}
    # 首个违规时刻按最新链重新给出（交叠在 d0 首段止端 175 结束）
    assert datetime.fromisoformat(rep["first_violation_at"]) == dts(175)


def test_min_overlap_zero_accepted(client):
    fence_id = create_fence(client)
    # d0[150,200)、d1[199,300)：交叠仅 1s。min_overlap_s=0 时创建并核验通过，
    # 与引擎对 min_overlap_s=0 的判定保持一致
    submit(client, [
        pt("d0", 150), pt("d0", 200, inside=False),
        pt("d1", 199), pt("d1", 300, inside=False),
    ])
    task = create_task(
        client, fence_id, members=("d0", "d1"), max_gap=30, min_overlap=0
    )
    assert task["min_overlap_s"] == 0
    rep = report(client, fence_id, task["id"])
    assert rep["passed"] is True
    assert rep["passed_steps"] == 1


def test_events_scoped_to_fence(client):
    fence_id = create_fence(client)
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
        pt("d0", 50), pt("d0", 200, inside=False),
        pt("d1", 170), pt("d1", 300, inside=False),
    ])
    task = create_task(client, fence_id, members=("d0", "d1"))
    assert report(client, fence_id, task["id"])["passed"] is True

    # 远端围栏无任何进入事件：首台缺席
    task_other = create_task(client, other, members=("d0", "d1"))
    rep = report(client, other, task_other["id"])
    assert rep["passed"] is False
    assert rep["steps"][0]["status"] == "absent"


def test_delete_fence_removes_handover_tasks(client):
    fence_id = create_fence(client)
    create_task(client, fence_id)
    create_task(client, fence_id, members=("d3", "d4"), name="交班B")
    assert client.delete(f"/fences/{fence_id}").status_code == 204

    assert client.get(f"/fences/{fence_id}/handover-tasks").status_code == 404
    new_id = create_fence(client)
    assert client.get(f"/fences/{new_id}/handover-tasks").json() == []


# ---------- 引擎级边界 ----------


def engine_report(events, members, max_gap=30.0, min_overlap=30.0,
                  start=0.0, end=400.0):
    return compute_handover_report(
        events, members, start, end, max_gap, min_overlap
    )


def test_engine_touching_gap_zero():
    # 首尾相接（空档 0）在 max_gap=0 时通过
    events = [
        ("a", EVENT_ENTER, 0), ("a", EVENT_LEAVE, 100),
        ("b", EVENT_ENTER, 100), ("b", EVENT_LEAVE, 200),
    ]
    rep = engine_report(events, ["a", "b"], max_gap=0, min_overlap=1)
    assert rep.passed is True
    assert rep.steps[1].takeover_ts == 100

    # 差 1 秒即超限
    events = [
        ("a", EVENT_ENTER, 0), ("a", EVENT_LEAVE, 100),
        ("b", EVENT_ENTER, 101), ("b", EVENT_LEAVE, 200),
    ]
    rep = engine_report(events, ["a", "b"], max_gap=0, min_overlap=1)
    assert rep.passed is False
    assert rep.steps[1].status == "gap_exceeded"
    assert rep.steps[1].violation_ts == 100


def test_engine_overlap_equal_threshold_and_end_cap():
    # 接班设备更早离场：交叠 = 接班止端 - 接班起端
    events = [
        ("a", EVENT_ENTER, 0), ("a", EVENT_LEAVE, 200),
        ("b", EVENT_ENTER, 170), ("b", EVENT_LEAVE, 200),
    ]
    rep = engine_report(events, ["a", "b"], min_overlap=30)
    assert rep.passed is True  # 恰 30s

    events = [
        ("a", EVENT_ENTER, 0), ("a", EVENT_LEAVE, 200),
        ("b", EVENT_ENTER, 171), ("b", EVENT_LEAVE, 200),
    ]
    rep = engine_report(events, ["a", "b"], min_overlap=30)
    assert rep.passed is False
    assert rep.steps[1].status == "overlap_insufficient"
    assert rep.steps[1].violation_ts == 200  # 交叠在 b 离场时结束


def test_engine_same_instant_leave_enter_leave_first():
    # 同一设备 t=100 leave 后立刻 enter：旧区间 [0,100) 与新区间 [100,200)
    # 前段交班给 b[80,100)：交叠 20；新区间不能作为 b 的接班（时间倒退）
    events = [
        ("a", EVENT_ENTER, 0), ("a", EVENT_LEAVE, 100),
        ("a", EVENT_ENTER, 100), ("a", EVENT_LEAVE, 200),
        ("b", EVENT_ENTER, 80), ("b", EVENT_LEAVE, 100),
    ]
    rep = engine_report(events, ["a", "b"], min_overlap=20)
    assert rep.passed is True
    assert rep.steps[0].interval_start == 0
    assert rep.steps[1].takeover_ts == 80


def test_engine_absent_candidate_before_takeover():
    # d1 仅在 d0 到岗前到场并离开：无满足时间推进的区间 -> absent
    events = [
        ("a", EVENT_ENTER, 200), ("a", EVENT_LEAVE, 300),
        ("b", EVENT_ENTER, 50), ("b", EVENT_LEAVE, 150),
    ]
    rep = engine_report(events, ["a", "b"])
    assert rep.passed is False
    assert rep.steps[1].status == "absent"
    assert rep.steps[1].violation_ts == 400


def test_engine_converging_prefixes_keep_lex_earliest_chain():
    # 多个前缀汇聚到后续区间（两层菱形）：
    # a: [10,35)、[40,90)
    # b: [38,46)（仅 a1 可接：空档 3；对 a2 时间倒退）
    #    [46,85)（仅 a2 可接：首尾相接；与 a1 空档 11 > max_gap）
    # c: [56,110)（两个前缀都能接：b1 交叠 29；b2 空档 10）
    # 通过步数并列（均为 2 步），必须保留完整接班元组字典序更早的
    # a1 -> b1 -> c（元组 (38,56)），而不是较晚的 a2 -> b2 -> c（(46,56)）。
    events = [
        ("a", EVENT_ENTER, 10), ("a", EVENT_LEAVE, 35),
        ("a", EVENT_ENTER, 40), ("a", EVENT_LEAVE, 90),
        ("b", EVENT_ENTER, 38), ("b", EVENT_LEAVE, 46),
        ("b", EVENT_ENTER, 46), ("b", EVENT_LEAVE, 85),
        ("c", EVENT_ENTER, 56), ("c", EVENT_LEAVE, 110),
    ]
    rep = engine_report(events, ["a", "b", "c"], max_gap=10, min_overlap=10)
    assert rep.passed is True
    assert rep.passed_steps == 2
    assert [s.takeover_ts for s in rep.steps] == [10, 38, 56]
    assert rep.steps[0].interval_start == 10 and rep.steps[0].interval_end == 35
    assert rep.steps[1].interval_start == 38 and rep.steps[1].interval_end == 46

    # 三层菱形：汇聚点 c 之后还有一台接班失败，验证失败判定基于保留下来的早链。
    # a1[10,40)、a2[40,80)（同刻 leave+enter 离开先配对成两段）；
    # b1[30,52) 仅 a1 可接（交叠 12；b1 起端早于 a2 到岗，时间不推进）；
    # b2[55,90) 仅 a2 可接（交叠 25；对 a1 空档 15 > max_gap）；
    # c[58,100) 与 b1 空档 6、与 b2 交叠 32，两个前缀都能接 -> 汇聚；
    # d[98,130) 与 c 交叠 2 < 10 -> overlap_insufficient（交叠结束于 100）。
    # 两条 3 设备链步数相同（各 2 步），汇聚节点 c 必须保留接班元组
    # (30,58) 的早链；旧逻辑保留较晚的 (55,58)，回溯会错取 a2/b2 区间。
    events_fail = [
        ("a", EVENT_ENTER, 10), ("a", EVENT_LEAVE, 40),
        ("a", EVENT_ENTER, 40), ("a", EVENT_LEAVE, 80),
        ("b", EVENT_ENTER, 30), ("b", EVENT_LEAVE, 52),
        ("b", EVENT_ENTER, 55), ("b", EVENT_LEAVE, 90),
        ("c", EVENT_ENTER, 58), ("c", EVENT_LEAVE, 100),
        ("d", EVENT_ENTER, 98), ("d", EVENT_LEAVE, 130),
    ]
    rep_fail = engine_report(
        events_fail, ["a", "b", "c", "d"], max_gap=10, min_overlap=10
    )
    assert rep_fail.passed is False
    assert rep_fail.passed_steps == 2
    assert [s.takeover_ts for s in rep_fail.steps[:3]] == [10, 30, 58]
    d_step = rep_fail.steps[3]
    assert d_step.status == "overlap_insufficient"
    assert d_step.violation_ts == 100


def test_engine_same_instant_leave_enter_handover_leave_first():
    # b 在 t=100 同刻离开又进入（离开先配对，两段 [50,100) 与 [100,150)）：
    # a[0,100) 与 b 旧段交叠 50 达标；c[100,200) 与 b 新段首尾相接（空档 0）
    # 达标 -> 整条链通过。若错误按 enter 先处理，b 的新段被吞掉，c 接班无区间
    # 可接，会被误判为 gap_exceeded。
    events = [
        ("a", EVENT_ENTER, 0), ("a", EVENT_LEAVE, 100),
        ("b", EVENT_ENTER, 50), ("b", EVENT_LEAVE, 100),
        ("b", EVENT_ENTER, 100), ("b", EVENT_LEAVE, 150),
        ("c", EVENT_ENTER, 100), ("c", EVENT_LEAVE, 200),
    ]
    rep = engine_report(events, ["a", "b", "c"], start=0, end=200,
                        max_gap=0, min_overlap=10)
    assert rep.passed is True
    assert rep.passed_steps == 2
    assert [s.takeover_ts for s in rep.steps] == [0, 50, 100]
    assert rep.steps[1].interval_start == 50 and rep.steps[1].interval_end == 100


def test_engine_min_overlap_zero_accepted():
    # min_overlap_s=0：只要两段真正相交（接班起端 < 交班止端）即达标
    events = [
        ("a", EVENT_ENTER, 0), ("a", EVENT_LEAVE, 100),
        ("b", EVENT_ENTER, 99), ("b", EVENT_LEAVE, 200),
    ]
    rep = engine_report(events, ["a", "b"], max_gap=30, min_overlap=0)
    assert rep.passed is True

    # 仅首尾相接（无真正相交）时不受 min_overlap=0 影响，仍按空档判定：
    # max_gap=0 允许空档 0 -> 通过
    events = [
        ("a", EVENT_ENTER, 0), ("a", EVENT_LEAVE, 100),
        ("b", EVENT_ENTER, 100), ("b", EVENT_LEAVE, 200),
    ]
    rep = engine_report(events, ["a", "b"], max_gap=0, min_overlap=0)
    assert rep.passed is True
