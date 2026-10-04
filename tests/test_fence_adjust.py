from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)


def square(lon0, lat0, lon1, lat1, holes=None):
    rings = [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]
    for h in holes or []:
        rings.append([
            [h[0], h[1]], [h[2], h[1]], [h[2], h[3]], [h[0], h[3]], [h[0], h[1]]
        ])
    return {"type": "Polygon", "coordinates": rings}


def pt(device, sec, lat, lon):
    return {
        "device_id": device,
        "ts": (BASE + timedelta(seconds=sec)).isoformat(),
        "lat": lat,
        "lon": lon,
    }


def iso(sec):
    return (BASE + timedelta(seconds=sec)).isoformat()


def at(value, sec):
    return datetime.fromisoformat(value) == BASE + timedelta(seconds=sec)


def create_fence(client, geometry, config=None, name="测试围栏"):
    payload = {"name": name, "geometry": geometry, "config": config or {}}
    r = client.post("/fences", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def adjust(client, fence_id, geometry=None, config=None, name="调整后围栏"):
    payload = {"name": name, "geometry": geometry, "config": config if config is not None else {}}
    return client.put(f"/fences/{fence_id}", json=payload)


def event_keys(client, **params):
    r = client.get("/events", params=params)
    assert r.status_code == 200, r.text
    return [(e["device_id"], e["type"], e["ts"]) for e in r.json()]


def test_adjust_boundary_reapplies_to_existing_track(client):
    # 小围栏：dev1 的点 (0.02, 0.005) 在界外，无事件
    fence_id = create_fence(client, square(0, 0, 0.01, 0.01))
    submit(client, [pt("dev1", 0, 0.02, 0.005)])
    assert event_keys(client, device_id="dev1") == []

    r = adjust(client, fence_id, geometry=square(0, 0, 0.03, 0.03))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == fence_id
    assert body["name"] == "调整后围栏"
    assert body["geometry"]["coordinates"][0][2] == [0.03, 0.03]

    # 新边界对存量轨迹生效：点落入扩大后的围栏
    events = event_keys(client, device_id="dev1")
    assert [e[1] for e in events] == ["enter"]
    assert datetime.fromisoformat(events[0][2]) == BASE

    # 缩小边界后同样重算，存量 enter 消失
    r = adjust(client, fence_id, geometry=square(0.03, 0.03, 0.04, 0.04))
    assert r.status_code == 200, r.text
    assert event_keys(client, device_id="dev1") == []


def test_adjust_debounce_params_reapplies(client):
    # 进入 dwell 30s：dev1 仅在场 10s，无事件
    fence_id = create_fence(client, square(0, 0, 0.01, 0.01), {"enter_dwell_s": 30})
    submit(client, [
        pt("dev1", 0, 0.02, 0.02),
        pt("dev1", 10, 0.005, 0.005),
        pt("dev1", 20, 0.02, 0.02),
    ])
    assert event_keys(client, device_id="dev1") == []

    # 放宽 dwell 为 0，新去抖规则对存量轨迹生效
    r = adjust(client, fence_id, geometry=square(0, 0, 0.01, 0.01), config={})
    assert r.status_code == 200, r.text
    events = event_keys(client, device_id="dev1")
    assert [e[1] for e in events] == ["enter", "leave"]
    assert datetime.fromisoformat(events[0][2]) == BASE + timedelta(seconds=10)

    st = client.get("/devices/dev1/status").json()["fences"][0]
    assert st["state"] == "outside"
    assert datetime.fromisoformat(st["since"]) == BASE + timedelta(seconds=20)


def test_adjust_unknown_fence_404(client):
    r = adjust(client, 999, geometry=square(0, 0, 0.01, 0.01))
    assert r.status_code == 404
    assert r.json()["detail"] == "围栏不存在"


def test_adjust_invalid_geometry_and_config_422(client):
    fence_id = create_fence(client, square(0, 0, 0.01, 0.01))

    bowtie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [0.01, 0.01], [0.01, 0], [0, 0.01], [0, 0]]],
    }
    r = adjust(client, fence_id, geometry=bowtie)
    assert r.status_code == 422

    r = client.put(f"/fences/{fence_id}", json={
        "name": "bad-config",
        "geometry": square(0, 0, 0.01, 0.01),
        "config": {"enter_buffer_m": 200, "exit_buffer_m": 100},
    })
    assert r.status_code == 422

    # 拒绝后原定义不变
    got = client.get(f"/fences/{fence_id}").json()
    assert got["geometry"]["coordinates"][0][2] == [0.01, 0.01]


def test_adjust_gate_outside_new_area_conflict_422_and_unchanged(client):
    fence_id = create_fence(client, square(0, 0, 0.04, 0.04))
    gate = client.post(f"/fences/{fence_id}/gates", json={
        "geometry": {"type": "LineString",
                     "coordinates": [[0.01, 0.02], [0.03, 0.02]]},
        "forward_name": "南向北",
        "reverse_name": "北向南",
    }).json()
    gate_id = gate["id"]

    # 南→北穿越门线
    submit(client, [
        pt("dev1", 0, 0.019, 0.02),
        pt("dev1", 10, 0.021, 0.02),
    ])
    crossings_before = client.get("/crossings", params={"device_id": "dev1"}).json()
    assert len(crossings_before) == 1
    events_before = event_keys(client, device_id="dev1")

    # 缩小围栏：门线东半段越出新外环
    r = adjust(client, fence_id, geometry=square(0, 0, 0.015, 0.015))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["conflicting_gate_ids"] == [gate_id]

    # 原围栏、事件与穿越均不改变
    got = client.get(f"/fences/{fence_id}").json()
    assert got["geometry"]["coordinates"][0][2] == [0.04, 0.04]
    gates = client.get(f"/fences/{fence_id}/gates").json()
    assert [g["id"] for g in gates] == [gate_id]
    assert event_keys(client, device_id="dev1") == events_before
    crossings_after = client.get("/crossings", params={"device_id": "dev1"}).json()
    assert len(crossings_after) == 1
    assert crossings_after[0]["id"] == crossings_before[0]["id"]


def test_adjust_gate_crossing_new_hole_conflict_422(client):
    fence_id = create_fence(client, square(0, 0, 0.04, 0.04))
    gate_id = client.post(f"/fences/{fence_id}/gates", json={
        "geometry": {"type": "LineString",
                     "coordinates": [[0.01, 0.02], [0.03, 0.02]]},
        "forward_name": "南向北",
        "reverse_name": "北向南",
    }).json()["id"]

    # 外环不变、新增中央孔洞：门线恰好横穿孔洞
    r = adjust(
        client,
        fence_id,
        geometry=square(0, 0, 0.04, 0.04, holes=[(0.015, 0.015, 0.025, 0.025)]),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["conflicting_gate_ids"] == [gate_id]


def test_adjust_keeps_gates_routes_and_recomputes_crossings(client):
    fence_id = create_fence(client, square(0, 0, 0.02, 0.02))
    g1 = client.post(f"/fences/{fence_id}/gates", json={
        "geometry": {"type": "LineString",
                     "coordinates": [[0.005, 0.01], [0.015, 0.01]]},
        "forward_name": "过一门",
        "reverse_name": "过一门反向",
    }).json()["id"]
    g2 = client.post(f"/fences/{fence_id}/gates", json={
        "geometry": {"type": "LineString",
                     "coordinates": [[0.005, 0.015], [0.015, 0.015]]},
        "forward_name": "过二门",
        "reverse_name": "过二门反向",
    }).json()["id"]
    route_id = client.post(f"/fences/{fence_id}/inspection-routes", json={
        "name": "两门巡检",
        "checkpoints": [
            {"gate_id": g1, "direction": "forward"},
            {"gate_id": g2, "direction": "forward", "max_interval_s": 120},
        ],
    }).json()["id"]

    submit(client, [
        pt("dev1", 0, 0.009, 0.01),
        pt("dev1", 10, 0.011, 0.01),    # 穿一门 forward ~t5
        pt("dev1", 20, 0.014, 0.01),
        pt("dev1", 30, 0.016, 0.01),    # 穿二门 forward ~t25
    ])

    # 扩大围栏（两门与路线均在新范围内）
    r = adjust(client, fence_id, geometry=square(0, 0, 0.04, 0.04))
    assert r.status_code == 200, r.text

    # 原门线与巡检路线仍在、仍可用；穿越按存量轨迹重算
    assert {g["id"] for g in client.get(f"/fences/{fence_id}/gates").json()} == {g1, g2}
    crossings = client.get("/crossings", params={"device_id": "dev1"}).json()
    assert [(c["gate_id"], c["direction"]) for c in crossings] == [
        (g1, "forward"), (g2, "forward")
    ]
    report = client.get(
        f"/fences/{fence_id}/inspection-routes/{route_id}/report",
        params={"device_id": "dev1", "start": iso(0), "end": iso(100)},
    )
    assert report.status_code == 200, report.text
    assert report.json()["passed"] is True


def test_adjust_one_fence_leaves_other_fence_results_unchanged(client):
    f1 = create_fence(client, square(0, 0, 0.02, 0.02), name="A")
    f2 = create_fence(client, square(0.04, 0.04, 0.06, 0.06), name="B")
    submit(client, [
        pt("dev1", 0, 0.005, 0.005),
        pt("dev1", 50, 0.03, 0.03),       # 离开 A
        pt("dev2", 0, 0.05, 0.05),        # 一直在 B 内
    ])

    before_b = event_keys(client, fence_id=f2)
    assert [e[1] for e in before_b] == ["enter"]

    # 调整 A（收紧 dwell），B 的事件结果不变
    r = adjust(client, f1, geometry=square(0, 0, 0.02, 0.02),
               config={"enter_dwell_s": 5})
    assert r.status_code == 200, r.text
    assert event_keys(client, fence_id=f2) == before_b


def test_adjust_refreshes_capacity_clearance_rendezvous_handover_isolation(client):
    # 围栏 1：[0,0]-[0.01,0.01]；dev1 t0 在内、t100 走到 (0.02,0.02) 出界
    f1 = create_fence(client, square(0, 0, 0.01, 0.01), name="主围栏")
    submit(client, [
        pt("dev1", 0, 0.005, 0.005),
        pt("dev1", 100, 0.02, 0.02),
        pt("dev2", 0, 0.005, 0.005),     # dev2 一直留在原位
    ])

    # 容量规则
    rule = client.post(f"/fences/{f1}/capacity-rules", json={
        "effective_from": iso(0), "max_occupancy": 1,
    }).json()
    assert rule["max_occupancy"] == 1

    # 清场任务
    clear = client.post(f"/fences/{f1}/clearance-tasks", json={
        "name": "清场", "start_at": iso(0), "deadline": iso(200),
    }).json()["id"]

    # 协同任务：门槛 120s
    rdz = client.post(f"/fences/{f1}/rendezvous-tasks", json={
        "name": "协同", "members": ["dev1", "dev2"],
        "start_at": iso(0), "end_at": iso(300), "min_co_presence_s": 120,
    }).json()["id"]

    # 交班任务：dev1 → dev2
    hnd = client.post(f"/fences/{f1}/handover-tasks", json={
        "name": "交班", "members": ["dev1", "dev2"],
        "start_at": iso(0), "end_at": iso(300),
        "max_gap_s": 10, "min_overlap_s": 0,
    }).json()["id"]

    # 隔离任务：组0 [f1,f2]，组1 [f2,f3]（f3 不会被进入）
    f2 = create_fence(client, square(0.015, 0.015, 0.03, 0.03), name="邻围栏")
    f3 = create_fence(client, square(0.1, 0.1, 0.11, 0.11), name="远围栏")
    iso_task = client.post("/isolation-tasks", json={
        "name": "隔离",
        "groups": [
            {"fence_ids": [f1, f2], "start_at": iso(0), "end_at": iso(300)},
            {"fence_ids": [f2, f3], "start_at": iso(0), "end_at": iso(300)},
        ],
    }).json()["id"]

    # —— 调整前：dev1 [0,100) 在场后离开 ——
    occ = client.get(f"/fences/{f1}/occupancy", params={"start": iso(0), "end": iso(300)}).json()
    counts = [(s["count"], s["end"][-9:]) for s in occ["segments"]]
    assert occ["segments"][0]["count"] == 2  # dev1/dev2 都在内
    assert occ["segments"][-1]["count"] == 1  # dev1 离开后只剩 dev2

    report = client.get(f"/fences/{f1}/clearance-tasks/{clear}/report").json()
    d1 = next(d for d in report["devices"] if d["device_id"] == "dev1")
    assert d1["status"] == "cleared"

    rdz_before = client.get(f"/fences/{f1}/rendezvous-tasks/{rdz}/report").json()
    assert rdz_before["passed"] is False  # 共同在场仅 [0,100)=100s < 120s

    hnd_before = client.get(f"/fences/{f1}/handover-tasks/{hnd}/report").json()
    assert datetime.fromisoformat(hnd_before["steps"][0]["interval_end"]) == BASE + timedelta(seconds=100)

    iso_before = client.get(f"/isolation-tasks/{iso_task}/report").json()
    assert iso_before["passed"] is True  # dev1 100 后只在 f2

    # —— 扩大围栏 1 至 [0,0]-[0.03,0.03]：dev1 t100 的点仍在界内 ——
    r = adjust(client, f1, geometry=square(0, 0, 0.03, 0.03))
    assert r.status_code == 200, r.text

    # 容量：dev1 不再离开，全程 2 人（规则仍生效）
    occ = client.get(f"/fences/{f1}/occupancy", params={"start": iso(0), "end": iso(300)}).json()
    assert {s["count"] for s in occ["segments"]} == {2}

    # 清场：dev1 截止时仍在场 -> remaining
    report = client.get(f"/fences/{f1}/clearance-tasks/{clear}/report").json()
    d1 = next(d for d in report["devices"] if d["device_id"] == "dev1")
    assert d1["status"] == "remaining"
    assert d1["last_leave_at"] is None

    # 协同：共同在场 [0,300)=300s >= 120s -> passed
    rdz_after = client.get(f"/fences/{f1}/rendezvous-tasks/{rdz}/report").json()
    assert rdz_after["passed"] is True
    seg = rdz_after["segments"][0]
    assert at(seg["start"], 0) and at(seg["end"], 300)

    # 交班：dev1 区间截到窗口结束
    hnd_after = client.get(f"/fences/{f1}/handover-tasks/{hnd}/report").json()
    assert hnd_after["passed"] is True
    assert at(hnd_after["steps"][0]["interval_end"], 300)

    # 隔离：dev1 100 起同时处于 f1 与 f2 -> 冲突片段 [100,300)
    iso_after = client.get(f"/isolation-tasks/{iso_task}/report").json()
    assert iso_after["passed"] is False
    dev = iso_after["devices"][0]
    assert dev["device_id"] == "dev1"
    grp = dev["groups"][0]
    assert grp["position"] == 0
    seg = grp["segments"][0]
    assert at(seg["start"], 100) and at(seg["end"], 300)
    assert seg["active_fence_ids"] == [f1, f2]
