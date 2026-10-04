from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)

PLAIN_FENCE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]],
}

# 三条东西向门线，纬度分别为 .003 / .005 / .007：
# 坐标顺序西->东，右=南、左=北，故正向 forward=南向北（纬度增大）
GATE_A = {"type": "LineString", "coordinates": [[0.001, 0.003], [0.009, 0.003]]}
GATE_B = {"type": "LineString", "coordinates": [[0.001, 0.005], [0.009, 0.005]]}
GATE_C = {"type": "LineString", "coordinates": [[0.001, 0.007], [0.009, 0.007]]}


def pt(device, sec, lat, lon=0.005):
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
    r = client.post("/fences", json={"name": "巡检测试围栏", "geometry": PLAIN_FENCE})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def create_gate(client, fence_id, geometry, fwd, rev):
    r = client.post(f"/fences/{fence_id}/gates", json={
        "geometry": geometry, "forward_name": fwd, "reverse_name": rev,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def three_gates(client, fence_id):
    a = create_gate(client, fence_id, GATE_A, "A入", "A出")
    b = create_gate(client, fence_id, GATE_B, "B入", "B出")
    c = create_gate(client, fence_id, GATE_C, "C入", "C出")
    return a, b, c


def create_route(client, fence_id, checkpoints, name="常规巡检"):
    r = client.post(
        f"/fences/{fence_id}/inspection-routes",
        json={"name": name, "checkpoints": checkpoints},
    )
    assert r.status_code == 201, r.text
    return r.json()


def route3(a, b, c, interval=60.0):
    """A(forward, 首点无间隔) -> B(forward) -> C(forward)。"""
    return [
        {"gate_id": a, "direction": "forward"},
        {"gate_id": b, "direction": "forward", "max_interval_s": interval},
        {"gate_id": c, "direction": "forward", "max_interval_s": interval},
    ]


def report(client, fence_id, route_id, device="dev1", start=0, end=200):
    r = client.get(
        f"/fences/{fence_id}/inspection-routes/{route_id}/report",
        params={"device_id": device, "start": iso(start), "end": iso(end)},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_route_lifecycle_and_checkpoint_output(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)

    route = create_route(client, fid, route3(a, b, c, interval=90.0), name="早班路线")
    assert route["fence_id"] == fid
    assert route["name"] == "早班路线"
    cps = route["checkpoints"]
    assert [cp["position"] for cp in cps] == [0, 1, 2]
    assert [cp["gate_id"] for cp in cps] == [a, b, c]
    assert cps[0]["max_interval_s"] is None
    assert cps[1]["max_interval_s"] == 90.0
    assert cps[0]["direction_name"] == "A入"

    # 列表按 id 升序，检查点按顺序返回
    create_route(client, fid, route3(c, b, a, interval=30.0), name="反向路线")
    routes = client.get(f"/fences/{fid}/inspection-routes").json()
    assert [r["name"] for r in routes] == ["早班路线", "反向路线"]
    assert [cp["gate_id"] for cp in routes[1]["checkpoints"]] == [c, b, a]

    # 删除路线；不存在 / 不属于围栏 -> 404
    assert client.delete(f"/fences/{fid}/inspection-routes/{route['id']}").status_code == 204
    assert client.delete(
        f"/fences/{fid}/inspection-routes/{route['id']}"
    ).status_code == 404
    other = create_fence(client)
    # 用第二个围栏访问第一个围栏的路线 -> 404
    remaining = client.get(f"/fences/{fid}/inspection-routes").json()[0]
    assert client.get(
        f"/fences/{other}/inspection-routes/{remaining['id']}/report",
        params={"device_id": "x", "start": iso(0), "end": iso(10)},
    ).status_code == 404


def test_route_create_validation(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)

    # 围栏不存在 -> 404
    r = client.post("/fences/999/inspection-routes",
                    json={"name": "x", "checkpoints": route3(a, b, c)})
    assert r.status_code == 404

    # 检查点数量：1 个 / 9 个 -> 422
    one = [{"gate_id": a, "direction": "forward"}]
    r = client.post(f"/fences/{fid}/inspection-routes",
                    json={"name": "too-few", "checkpoints": one})
    assert r.status_code == 422
    nine = [{"gate_id": a, "direction": "forward"}] + [
        {"gate_id": a, "direction": "forward", "max_interval_s": 10} for _ in range(8)
    ]
    r = client.post(f"/fences/{fid}/inspection-routes",
                    json={"name": "too-many", "checkpoints": nine})
    assert r.status_code == 422  # 同时含重复门线，也是 422

    # 门线重复 -> 422
    dup = [
        {"gate_id": a, "direction": "forward"},
        {"gate_id": a, "direction": "reverse", "max_interval_s": 10},
    ]
    r = client.post(f"/fences/{fid}/inspection-routes",
                    json={"name": "dup", "checkpoints": dup})
    assert r.status_code == 422

    # 首点设了间隔 -> 422
    bad_first = [
        {"gate_id": a, "direction": "forward", "max_interval_s": 10},
        {"gate_id": b, "direction": "forward", "max_interval_s": 10},
    ]
    r = client.post(f"/fences/{fid}/inspection-routes",
                    json={"name": "bad-first", "checkpoints": bad_first})
    assert r.status_code == 422

    # 后续点缺间隔 -> 422
    missing = [
        {"gate_id": a, "direction": "forward"},
        {"gate_id": b, "direction": "forward"},
    ]
    r = client.post(f"/fences/{fid}/inspection-routes",
                    json={"name": "missing-interval", "checkpoints": missing})
    assert r.status_code == 422

    # 负间隔 -> 422
    bad = route3(a, b, c)
    bad[1]["max_interval_s"] = -1
    r = client.post(f"/fences/{fid}/inspection-routes",
                    json={"name": "neg", "checkpoints": bad})
    assert r.status_code == 422

    # 门线不存在 -> 422
    r = client.post(f"/fences/{fid}/inspection-routes", json={
        "name": "ghost",
        "checkpoints": [
            {"gate_id": 999, "direction": "forward"},
            {"gate_id": a, "direction": "forward", "max_interval_s": 10},
        ],
    })
    assert r.status_code == 422

    # 门线属于其他围栏 -> 422
    other = create_fence(client)
    foreign = create_gate(client, other, GATE_A, "外A入", "外A出")
    r = client.post(f"/fences/{fid}/inspection-routes", json={
        "name": "foreign",
        "checkpoints": [
            {"gate_id": a, "direction": "forward"},
            {"gate_id": foreign, "direction": "forward", "max_interval_s": 10},
        ],
    })
    assert r.status_code == 422


def test_report_passes_in_order(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)
    route = create_route(client, fid, route3(a, b, c))

    # 穿越时刻：A@10、B@30、C@50，间隔均 20s <= 60s
    submit(client, [
        pt("dev1", 0, 0.002),
        pt("dev1", 20, 0.004),
        pt("dev1", 40, 0.006),
        pt("dev1", 60, 0.008),
    ])
    rep = report(client, fid, route["id"])
    assert rep["passed"] is True
    assert rep["first_violation_at"] is None
    for i, r in enumerate(rep["results"]):
        assert r["status"] == "passed"
        assert datetime.fromisoformat(r["passed_at"]) == dts(10 + 20 * i)
        assert r["failed_at"] is None
    assert [r["direction_name"] for r in rep["results"]] == ["A入", "B入", "C入"]


def test_report_timeout_on_late_crossing(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)
    route = create_route(client, fid, route3(a, b, c, interval=60.0))

    # A@10；B@80（间隔 70s > 60s），C@150
    submit(client, [
        pt("dev1", 0, 0.002),
        pt("dev1", 20, 0.004),
        pt("dev1", 140, 0.006),
        pt("dev1", 160, 0.008),
    ])
    rep = report(client, fid, route["id"])
    assert rep["passed"] is False
    assert datetime.fromisoformat(rep["first_violation_at"]) == dts(70)  # 间隔截止 10+60
    statuses = [(r["status"], r["passed_at"], r["failed_at"]) for r in rep["results"]]
    assert statuses[0][0] == "passed"
    assert datetime.fromisoformat(statuses[0][1]) == dts(10)
    assert statuses[1][0] == "timeout"
    assert statuses[1][1] is None
    assert datetime.fromisoformat(statuses[1][2]) == dts(70)
    assert statuses[2][0] == "not_reached"
    assert statuses[2][1] is None and statuses[2][2] is None


def test_report_timeout_at_window_end_without_crossing(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)
    route = create_route(client, fid, route3(a, b, c))

    # 只穿过 A@10，之后再无穿越；窗口结束时 B 的间隔早已超限 -> 超时时刻 70
    submit(client, [pt("dev1", 0, 0.002), pt("dev1", 20, 0.004)])
    rep = report(client, fid, route["id"], end=200)
    assert rep["passed"] is False
    assert datetime.fromisoformat(rep["first_violation_at"]) == dts(70)
    assert rep["results"][0]["status"] == "passed"
    assert rep["results"][1]["status"] == "timeout"
    assert datetime.fromisoformat(rep["results"][1]["failed_at"]) == dts(70)
    assert rep["results"][2]["status"] == "not_reached"

    # 窗口在间隔截止前结束：尚不能判超时，B 保持 not_reached，无违规时刻
    early = report(client, fid, route["id"], end=50)
    assert early["passed"] is False
    assert early["first_violation_at"] is None
    assert [r["status"] for r in early["results"]] == ["passed", "not_reached", "not_reached"]


def test_interval_boundary_equal_allowed(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)
    route = create_route(client, fid, route3(a, b, c, interval=20.0))

    # A@10、B@30（恰在间隔上限）、C@50（同样恰在上限）-> 全部通过
    submit(client, [
        pt("dev1", 0, 0.002),
        pt("dev1", 20, 0.004),
        pt("dev1", 40, 0.006),
        pt("dev1", 60, 0.008),
    ])
    rep = report(client, fid, route["id"])
    assert rep["passed"] is True


def test_report_sequence_violation_reverse_at_current_gate(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)
    route = create_route(client, fid, route3(a, b, c))

    # 首条相关穿越即 A 反向（北向南）@10 -> 检查点 0 顺序违规
    submit(client, [pt("dev1", 0, 0.004), pt("dev1", 20, 0.002)])
    rep = report(client, fid, route["id"])
    assert rep["passed"] is False
    assert datetime.fromisoformat(rep["first_violation_at"]) == dts(10)
    r0 = rep["results"][0]
    assert r0["status"] == "sequence_violation"
    assert datetime.fromisoformat(r0["failed_at"]) == dts(10)
    assert r0["passed_at"] is None
    assert [r["status"] for r in rep["results"][1:]] == ["not_reached", "not_reached"]

    # 即使之后按正向补穿全部门线，首个违规已使路线失败（重放不恢复）
    submit(client, [
        pt("dev1", 30, 0.004),
        pt("dev1", 40, 0.006),
        pt("dev1", 50, 0.008),
    ])
    rep = report(client, fid, route["id"])
    assert rep["passed"] is False
    assert rep["results"][0]["status"] == "sequence_violation"


def test_report_sequence_violation_later_gate_forward(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)
    route = create_route(client, fid, route3(a, b, c))

    # 先穿了后续门线 C 的正向 @10 -> 检查点 2 顺序违规
    submit(client, [pt("dev1", 0, 0.006), pt("dev1", 20, 0.008)])
    rep = report(client, fid, route["id"])
    assert rep["passed"] is False
    assert datetime.fromisoformat(rep["first_violation_at"]) == dts(10)
    assert [r["status"] for r in rep["results"]] == [
        "not_reached", "not_reached", "sequence_violation",
    ]
    assert rep["results"][2]["gate_id"] == c
    assert datetime.fromisoformat(rep["results"][2]["failed_at"]) == dts(10)


def test_other_gates_ignored_and_already_passed_gate_ignored(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)
    # 不在路线上的第四道门线
    d = create_gate(client, fid, {
        "type": "LineString",
        "coordinates": [[0.001, 0.001], [0.009, 0.001]],
    }, "D入", "D出")
    route = create_route(client, fid, route3(a, b, c))

    # 先穿非路线门线 D 的正/反向（忽略），再按序正向通过 A、B、C；
    # 通过 A 之后又反向、正向再穿 A（已通过门线，忽略）
    submit(client, [
        pt("dev1", 5, 0.000),
        pt("dev1", 15, 0.002),   # D forward @10
        pt("dev1", 25, 0.000),   # D reverse @20
        pt("dev1", 35, 0.004),   # A forward @32.5（段 25~35 跨纬度 .003）
        pt("dev1", 45, 0.002),   # A reverse @40（已通过门线，忽略）
        pt("dev1", 60, 0.004),   # A forward @52.5（已通过门线，忽略）
        pt("dev1", 75, 0.006),   # B forward @67.5
        pt("dev1", 90, 0.008),   # C forward @82.5
    ])
    rep = report(client, fid, route["id"])
    assert rep["passed"] is True, rep
    assert [r["status"] for r in rep["results"]] == ["passed"] * 3
    assert [datetime.fromisoformat(r["passed_at"]) for r in rep["results"]] == [
        dts(32.5), dts(67.5), dts(82.5),
    ]
    # 门线 D 的穿越存在于 crossing 记录但与核验无关（含 25~35 段的一次正向）
    crossings = client.get("/crossings", params={"device_id": "dev1", "gate_id": d}).json()
    assert len(crossings) >= 2


def test_reverse_crossing_later_gate_is_sequence_violation(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)
    route = create_route(client, fid, route3(a, b, c))

    # 尚未穿过 A 时先反向穿过后续门线 B（北向南）@10 -> 顺序违规记在检查点 1
    submit(client, [pt("dev1", 0, 0.006), pt("dev1", 20, 0.004)])
    rep = report(client, fid, route["id"])
    assert rep["passed"] is False
    assert datetime.fromisoformat(rep["first_violation_at"]) == dts(10)
    assert [r["status"] for r in rep["results"]] == [
        "not_reached", "sequence_violation", "not_reached",
    ]
    assert rep["results"][1]["gate_id"] == b
    assert datetime.fromisoformat(rep["results"][1]["failed_at"]) == dts(10)


def test_half_open_window_boundaries(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)
    route = create_route(client, fid, route3(a, b, c))

    # A@0（恰在窗口起点，计入）、B@30、C@50
    submit(client, [
        pt("dev1", -20, 0.002),
        pt("dev1", 20, 0.004),
        pt("dev1", 40, 0.006),
        pt("dev1", 60, 0.008),
    ])
    rep = report(client, fid, route["id"], start=0, end=200)
    assert rep["passed"] is True
    assert datetime.fromisoformat(rep["results"][0]["passed_at"]) == dts(0)

    # 窗口终点恰为 C 穿越时刻 50：[start,end) 不含 -> C 未到达（且未超间隔）
    rep = report(client, fid, route["id"], start=0, end=50)
    assert rep["passed"] is False
    assert rep["first_violation_at"] is None
    assert [r["status"] for r in rep["results"]] == ["passed", "passed", "not_reached"]

    # start >= end -> 422
    r = client.get(
        f"/fences/{fid}/inspection-routes/{route['id']}/report",
        params={"device_id": "dev1", "start": iso(10), "end": iso(10)},
    )
    assert r.status_code == 422

    # 路线不存在 / 围栏不存在 -> 404
    assert client.get(
        f"/fences/{fid}/inspection-routes/999/report",
        params={"device_id": "dev1", "start": iso(0), "end": iso(10)},
    ).status_code == 404
    assert client.get(
        f"/fences/999/inspection-routes/{route['id']}/report",
        params={"device_id": "dev1", "start": iso(0), "end": iso(10)},
    ).status_code == 404


def test_report_recomputed_after_backfill(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)
    route = create_route(client, fid, route3(a, b, c))

    # 初版轨迹只穿 A@10：B 超时
    submit(client, [pt("dev1", 0, 0.002), pt("dev1", 20, 0.004)])
    rep = report(client, fid, route["id"])
    assert rep["passed"] is False
    assert rep["results"][1]["status"] == "timeout"

    # 补录后续轨迹点（按序到达）：B@30、C@50 -> 通过
    submit(client, [pt("dev1", 40, 0.006), pt("dev1", 60, 0.008)])
    rep = report(client, fid, route["id"])
    assert rep["passed"] is True
    assert rep["first_violation_at"] is None
    assert [datetime.fromisoformat(r["passed_at"]) for r in rep["results"]] == [
        dts(10), dts(30), dts(50),
    ]

    # 乱序补录更早的点：A 穿越时刻随插值改变（0~20 段中点插入后重算），报告同步
    # 先制造另一台设备的乱序场景
    submit(client, [
        pt("dev2", 60, 0.008),
        pt("dev2", 0, 0.002),
        pt("dev2", 40, 0.006),
        pt("dev2", 20, 0.004),
    ])
    rep2 = report(client, fid, route["id"], device="dev2")
    assert rep2["passed"] is True
    assert [datetime.fromisoformat(r["passed_at"]) for r in rep2["results"]] == [
        dts(10), dts(30), dts(50),
    ]


def test_delete_gate_referenced_by_route_rejected(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)
    route = create_route(client, fid, route3(a, b, c))

    # 被引用的门线不能删除
    assert client.delete(f"/fences/{fid}/gates/{b}").status_code == 409
    # 门线仍在，且报告仍可核验
    assert len(client.get(f"/fences/{fid}/gates").json()) == 3

    # 删除路线后即可删除门线
    assert client.delete(
        f"/fences/{fid}/inspection-routes/{route['id']}"
    ).status_code == 204
    assert client.delete(f"/fences/{fid}/gates/{b}").status_code == 204
    assert [g["id"] for g in client.get(f"/fences/{fid}/gates").json()] == [a, c]


def test_delete_fence_cascades_routes(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)
    create_route(client, fid, route3(a, b, c))
    assert len(client.get(f"/fences/{fid}/inspection-routes").json()) == 1
    assert client.delete(f"/fences/{fid}").status_code == 204
    assert client.get(f"/fences/{fid}/inspection-routes").status_code == 404


def test_report_independent_per_device(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)
    route = create_route(client, fid, route3(a, b, c))

    # dev1 全程按序通过；dev2 反向穿 A
    submit(client, [
        pt("dev1", 0, 0.002),
        pt("dev1", 20, 0.004),
        pt("dev1", 40, 0.006),
        pt("dev1", 60, 0.008),
        pt("dev2", 0, 0.004),
        pt("dev2", 20, 0.002),
    ])
    assert report(client, fid, route["id"], device="dev1")["passed"] is True
    rep2 = report(client, fid, route["id"], device="dev2")
    assert rep2["passed"] is False
    assert rep2["results"][0]["status"] == "sequence_violation"

    # 从未出现过的设备：全部 not_reached（首点无间隔，窗口内无超时）
    rep3 = report(client, fid, route["id"], device="ghost")
    assert rep3["passed"] is False
    assert rep3["first_violation_at"] is None
    assert [r["status"] for r in rep3["results"]] == ["not_reached"] * 3


def test_route_with_reverse_direction_checkpoints(client):
    fid = create_fence(client)
    a, b, c = three_gates(client, fid)
    # 反方向巡检：C(reverse) -> B(reverse) -> A(reverse)
    route = create_route(client, fid, [
        {"gate_id": c, "direction": "reverse"},
        {"gate_id": b, "direction": "reverse", "max_interval_s": 60},
        {"gate_id": a, "direction": "reverse", "max_interval_s": 60},
    ])
    # 北向南依次穿过 C@10、B@30、A@50
    submit(client, [
        pt("dev1", 0, 0.008),
        pt("dev1", 20, 0.006),
        pt("dev1", 40, 0.004),
        pt("dev1", 60, 0.002),
    ])
    rep = report(client, fid, route["id"])
    assert rep["passed"] is True
    assert [r["direction"] for r in rep["results"]] == ["reverse"] * 3
    assert [r["direction_name"] for r in rep["results"]] == ["C出", "B出", "A出"]
