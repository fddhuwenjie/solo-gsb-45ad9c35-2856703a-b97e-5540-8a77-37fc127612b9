"""围栏调整（PUT /fences/{id}）端到端测试。

覆盖：
- 新边界与新去抖参数对存量轨迹生效（事件/当前状态重算）
- 404 / 422（几何、参数沿用创建校验）
- 现有门线落到新有效区域外或穿过新孔洞 -> 422 列出冲突门线 ID，原数据不变
- 成功调整保留门线/巡检路线，穿越按新边界重算
- 只影响被调整围栏，其他围栏结果不变
- 容量/清场/协同/交班/隔离报告再次查询反映新事件
"""
from datetime import datetime, timedelta, timezone

import pytest

BASE = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)

# 外环 (0,0)-(0.01,0.01)，约 1.1km 见方；中央孔洞 (0.004,0.004)-(0.006,0.006)
FENCE_WITH_HOLE = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]],
        [[0.004, 0.004], [0.006, 0.004], [0.006, 0.006], [0.004, 0.006], [0.004, 0.004]],
    ],
}
PLAIN_FENCE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]],
}
# 扩大到 (0,0)-(0.03,0.03)：原界外点 (0.02,0.02) 变为内部
EXPANDED_FENCE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0.03, 0], [0.03, 0.03], [0, 0.03], [0, 0]]],
}
# 同名扩大围栏，中央带孔洞（孔洞覆盖 0.004~0.006，穿过东西向门线）
EXPANDED_WITH_HOLE = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [0.03, 0], [0.03, 0.03], [0, 0.03], [0, 0]],
        [[0.004, 0.004], [0.006, 0.004], [0.006, 0.006], [0.004, 0.006], [0.004, 0.004]],
    ],
}
# 收缩到 (0.004,0.004)-(0.02,0.02)：东西向门线西端点 0.0025 落在界外
SHRUNK_FENCE = {
    "type": "Polygon",
    "coordinates": [[[0.004, 0.004], [0.02, 0.004], [0.02, 0.02], [0.004, 0.02], [0.004, 0.004]]],
}

GATE_EW = {
    "type": "LineString",
    "coordinates": [[0.0025, 0.005], [0.0075, 0.005]],
}


def pt(device, sec, lat, lon, base=BASE):
    return {
        "device_id": device,
        "ts": (base + timedelta(seconds=sec)).isoformat(),
        "lat": lat,
        "lon": lon,
    }


def iso(sec, base=BASE):
    return (base + timedelta(seconds=sec)).isoformat()


def create_fence(client, geometry=PLAIN_FENCE, config=None, name="测试围栏"):
    r = client.post("/fences", json={"name": name, "geometry": geometry, "config": config or {}})
    assert r.status_code == 201, r.text
    return r.json()


def update_fence(client, fence_id, geometry, config=None, name="测试围栏", expect=200):
    payload = {"name": name, "geometry": geometry, "config": config or {}}
    r = client.put(f"/fences/{fence_id}", json=payload)
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r.json()


def submit(client, points):
    r = client.post("/points", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def get_events(client, **params):
    r = client.get("/events", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def create_gate(client, fence_id, geometry=GATE_EW, fwd="南->北", rev="北->南", min_disp=0.0):
    r = client.post(
        f"/fences/{fence_id}/gates",
        json={
            "geometry": geometry,
            "forward_name": fwd,
            "reverse_name": rev,
            "min_displacement_m": min_disp,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_update_not_found(client):
    r = client.put("/fences/999", json={"name": "x", "geometry": PLAIN_FENCE, "config": {}})
    assert r.status_code == 404


def test_update_invalid_geometry_and_config_rejected(client):
    fence = create_fence(client)
    bowtie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [0.01, 0.01], [0.01, 0], [0, 0.01], [0, 0]]],
    }
    r = client.put(f"/fences/{fence['id']}", json={"name": "bad", "geometry": bowtie, "config": {}})
    assert r.status_code == 422

    # 参数校验沿用创建：enter_buffer_m 不得大于 exit_buffer_m
    r = client.put(
        f"/fences/{fence['id']}",
        json={
            "name": "bad-config",
            "geometry": PLAIN_FENCE,
            "config": {"enter_buffer_m": 200, "exit_buffer_m": 100},
        },
    )
    assert r.status_code == 422

    # 非 Polygon 类型同样 422
    r = client.put(
        f"/fences/{fence['id']}",
        json={"name": "bad-type", "geometry": {"type": "Point", "coordinates": [0, 0]}, "config": {}},
    )
    assert r.status_code == 422

    # 校验失败后围栏原样保留
    got = client.get(f"/fences/{fence['id']}").json()
    assert got["geometry"] == PLAIN_FENCE
    assert got["name"] == "测试围栏"


def test_new_boundary_applies_to_existing_track(client):
    fence = create_fence(client)
    submit(client, [
        pt("dev1", 0, 0.02, 0.02),    # 原界外
        pt("dev1", 10, 0.005, 0.005),  # 原界内 -> enter@10
        pt("dev1", 40, 0.02, 0.02),    # 原界外 -> leave@40
    ])
    assert [e["type"] for e in get_events(client, device_id="dev1")] == ["enter", "leave"]

    updated = update_fence(client, fence["id"], EXPANDED_FENCE, name="扩大后的围栏")
    assert updated["name"] == "扩大后的围栏"
    # created_at 不随调整改变
    assert updated["created_at"] == fence["created_at"]

    events = get_events(client, device_id="dev1")
    # (0.02,0.02) 在新边界内：首点即进入，后续一直在内，无 leave
    assert [(e["type"], e["fence_id"]) for e in events] == [("enter", fence["id"])]
    assert datetime.fromisoformat(events[0]["ts"]) == BASE

    status = client.get("/devices/dev1/status").json()["fences"]
    st = next(s for s in status if s["fence_id"] == fence["id"])
    assert st["state"] == "inside"
    assert datetime.fromisoformat(st["since"]) == BASE


def test_new_debounce_config_applies_to_existing_track(client):
    fence = create_fence(client)
    submit(client, [
        pt("dev1", 0, 0.02, 0.02),
        pt("dev1", 10, 0.005, 0.005),  # 进入
        pt("dev1", 20, 0.02, 0.02),    # 10s 后离开
    ])
    assert len(get_events(client, device_id="dev1")) == 2

    # 进入需持续 30s：本次仅停留 10s，调整后不应再有 enter/leave
    update_fence(client, fence["id"], PLAIN_FENCE, config={"enter_dwell_s": 30})
    assert get_events(client, device_id="dev1") == []

    st = next(
        s for s in client.get("/devices/dev1/status").json()["fences"]
        if s["fence_id"] == fence["id"]
    )
    assert st["state"] == "outside"


def test_conflicting_gate_outside_new_area_rejected_and_unchanged(client):
    fence = create_fence(client)
    gate = create_gate(client, fence["id"])
    submit(client, [
        pt("dev1", 0, 0.004, 0.005),
        pt("dev1", 20, 0.006, 0.005),   # 正向横穿门线
    ])
    before_events = get_events(client, fence_id=fence["id"])
    before_crossings = client.get("/crossings", params={"fence_id": fence["id"]}).json()
    assert len(before_crossings) == 1

    # 新边界把西门线端点 0.0025 甩到界外 -> 422 并列出冲突门线
    resp = update_fence(client, fence["id"], SHRUNK_FENCE, expect=422)
    assert resp["detail"]["conflicting_gate_ids"] == [gate["id"]]

    # 原围栏与衍生记录均不改变
    got = client.get(f"/fences/{fence['id']}").json()
    assert got["geometry"] == PLAIN_FENCE
    gates = client.get(f"/fences/{fence['id']}/gates").json()
    assert [g["id"] for g in gates] == [gate["id"]]
    assert get_events(client, fence_id=fence["id"]) == before_events
    crossings = client.get("/crossings", params={"fence_id": fence["id"]}).json()
    assert crossings == before_crossings


def test_conflicting_gate_through_new_hole_rejected(client):
    fence = create_fence(client)
    gate1 = create_gate(client, fence["id"])
    # 南门线（lat=0.002），不在孔洞纵向范围内
    gate2 = create_gate(
        client, fence["id"],
        geometry={"type": "LineString", "coordinates": [[0.0025, 0.002], [0.0075, 0.002]]},
        fwd="南2", rev="北2",
    )

    # 扩大边界（合法）但新孔洞穿过 gate1（lat=0.005），gate2 不受影响
    resp = update_fence(client, fence["id"], EXPANDED_WITH_HOLE, expect=422)
    assert resp["detail"]["conflicting_gate_ids"] == [gate1["id"]]
    assert gate2["id"] not in resp["detail"]["conflicting_gate_ids"]


def test_successful_update_keeps_gates_and_routes_recomputes_crossings(client):
    fence = create_fence(client)
    gate = create_gate(client, fence["id"])
    # 巡检路线引用该门线（两个检查点）
    second_gate = create_gate(
        client, fence["id"],
        geometry={"type": "LineString", "coordinates": [[0.005, 0.006], [0.005, 0.008]]},
        fwd="西->东", rev="东->西",
    )
    r = client.post(
        f"/fences/{fence['id']}/inspection-routes",
        json={
            "name": "路线",
            "checkpoints": [
                {"gate_id": gate["id"], "direction": "forward"},
                {"gate_id": second_gate["id"], "direction": "forward", "max_interval_s": 300},
            ],
        },
    )
    assert r.status_code == 201, r.text
    route_id = r.json()["id"]

    submit(client, [
        pt("dev1", 0, 0.004, 0.005),
        pt("dev1", 20, 0.006, 0.005),    # 正向横穿 gate
    ])
    crossings_before = client.get("/crossings", params={"device_id": "dev1"}).json()
    assert len(crossings_before) == 1

    # 扩大边界（门线仍在有效区域内）-> 200，门线与路线原样保留
    updated = update_fence(client, fence["id"], EXPANDED_FENCE)
    assert updated["geometry"] == EXPANDED_FENCE
    gates = client.get(f"/fences/{fence['id']}/gates").json()
    assert {g["id"] for g in gates} == {gate["id"], second_gate["id"]}
    routes = client.get(f"/fences/{fence['id']}/inspection-routes").json()
    assert [rt["id"] for rt in routes] == [route_id]

    # 穿越记录按新几何整体重算：方向、时刻、交点保持一致
    crossings_after = client.get("/crossings", params={"device_id": "dev1"}).json()
    assert len(crossings_after) == 1
    c = crossings_after[0]
    assert c["gate_id"] == gate["id"] and c["direction"] == "forward"
    assert datetime.fromisoformat(c["ts"]) == BASE + timedelta(seconds=10)

    # 巡检报告仍可基于该门线查询
    rep = client.get(
        f"/fences/{fence['id']}/inspection-routes/{route_id}/report",
        params={"device_id": "dev1", "start": iso(0), "end": iso(300)},
    )
    assert rep.status_code == 200, rep.text
    assert rep.json()["results"][0]["status"] == "passed"


def test_gate_on_new_exterior_boundary_allowed(client):
    fence = create_fence(client)
    create_gate(client, fence["id"])
    # 新外环恰好把门线两个端点放在边界上（y=0.005 介于 0.004~0.006）
    boundary_polygon = {
        "type": "Polygon",
        "coordinates": [[
            [0.0025, 0.004], [0.0075, 0.004], [0.0075, 0.006],
            [0.0025, 0.006], [0.0025, 0.004],
        ]],
    }
    r = update_fence(client, fence["id"], boundary_polygon)
    assert r["geometry"] == boundary_polygon


def test_other_fence_results_unchanged(client):
    fence1 = create_fence(client, name="围栏一")
    fence2 = create_fence(
        client,
        geometry={"type": "Polygon", "coordinates": [
            [[0.1, 0.1], [0.11, 0.1], [0.11, 0.11], [0.1, 0.11], [0.1, 0.1]],
        ]},
        name="围栏二",
    )
    submit(client, [
        pt("dev1", 0, 0.005, 0.005),
        pt("dev1", 10, 0.105, 0.105),
        pt("dev1", 20, 0.02, 0.02),
    ])

    def snapshot(fid):
        return sorted(
            (e["device_id"], e["type"], e["ts"]) for e in get_events(client, fence_id=fid)
        )

    f2_before = snapshot(fence2["id"])
    f1_before = snapshot(fence1["id"])
    assert f1_before and f2_before

    update_fence(client, fence1["id"], EXPANDED_FENCE)

    # 围栏二事件完全不变
    assert snapshot(fence2["id"]) == f2_before
    # 围栏一事件按新边界重算（(0.02,0.02) 变为内部点）
    assert snapshot(fence1["id"]) != f1_before

    # 两个围栏的当前状态都在
    statuses = {s["fence_id"]: s["state"]
                for s in client.get("/devices/dev1/status").json()["fences"]}
    assert statuses[fence1["id"]] == "inside"
    assert statuses[fence2["id"]] == "outside"


def test_update_with_no_track_points_works(client):
    fence = create_fence(client)
    create_gate(client, fence["id"])
    updated = update_fence(client, fence["id"], EXPANDED_FENCE)
    assert updated["geometry"] == EXPANDED_FENCE
    assert get_events(client, fence_id=fence["id"]) == []
    # 门线保留
    assert len(client.get(f"/fences/{fence['id']}/gates").json()) == 1


def test_capacity_and_clearance_reports_reflect_new_events(client):
    fence = create_fence(client)
    # 容量规则：窗口内上限 1
    cr = client.post(
        f"/fences/{fence['id']}/capacity-rules",
        json={"effective_from": iso(0), "max_occupancy": 1},
    )
    assert cr.status_code == 201
    # 清场任务
    task = client.post(
        f"/fences/{fence['id']}/clearance-tasks",
        json={"name": "清场", "start_at": iso(0), "deadline": iso(100)},
    )
    assert task.status_code == 201
    task_id = task.json()["id"]

    submit(client, [
        pt("dev1", 0, 0.005, 0.005),   # 围栏一内
        pt("dev2", 0, 0.02, 0.02),     # 原界外
        pt("dev2", 50, 0.02, 0.02),
    ])

    occ = client.get(
        f"/fences/{fence['id']}/occupancy",
        params={"start": iso(0), "end": iso(100)},
    ).json()
    assert all(s["count"] == 1 for s in occ["segments"])  # dev2 原在界外
    clearance = client.get(
        f"/fences/{fence['id']}/clearance-tasks/{task_id}/report"
    ).json()
    assert [d["device_id"] for d in clearance["devices"]] == ["dev1"]

    # 扩大围栏：dev2 全程在场，容量超限、清场起始名单新增 dev2（remaining）
    update_fence(client, fence["id"], EXPANDED_FENCE)

    occ = client.get(
        f"/fences/{fence['id']}/occupancy",
        params={"start": iso(0), "end": iso(100)},
    ).json()
    assert any(s["exceeded"] and s["count"] == 2 for s in occ["segments"])

    clearance = client.get(
        f"/fences/{fence['id']}/clearance-tasks/{task_id}/report"
    ).json()
    assert clearance["summary"] == {
        "start_count": 2, "cleared_count": 0, "remaining_count": 2,
    }
    assert {d["device_id"] for d in clearance["devices"]} == {"dev1", "dev2"}
    assert all(d["status"] == "remaining" for d in clearance["devices"])


def test_rendezvous_and_handover_reports_reflect_new_events(client):
    fence = create_fence(client)
    rz = client.post(
        f"/fences/{fence['id']}/rendezvous-tasks",
        json={
            "name": "协同",
            "members": ["dev1", "dev2"],
            "start_at": iso(0),
            "end_at": iso(100),
            "min_co_presence_s": 10,
        },
    )
    assert rz.status_code == 201
    rz_id = rz.json()["id"]
    ho = client.post(
        f"/fences/{fence['id']}/handover-tasks",
        json={
            "name": "交班",
            "members": ["dev1", "dev2"],
            "start_at": iso(0),
            "end_at": iso(100),
            "max_gap_s": 10,
            "min_overlap_s": 0,
        },
    )
    assert ho.status_code == 201
    ho_id = ho.json()["id"]

    submit(client, [
        pt("dev1", 0, 0.005, 0.005),   # 围栏内
        pt("dev1", 60, 0.005, 0.005),
        pt("dev2", 20, 0.02, 0.02),     # 原界外
        pt("dev2", 60, 0.02, 0.02),
    ])
    # 调整前：dev2 从未到场，协同不达标、交班首步之后 absent
    assert client.get(f"/fences/{fence['id']}/rendezvous-tasks/{rz_id}/report").json()["passed"] is False
    assert client.get(f"/fences/{fence['id']}/handover-tasks/{ho_id}/report").json()["passed"] is False

    update_fence(client, fence["id"], EXPANDED_FENCE)

    rz_rep = client.get(f"/fences/{fence['id']}/rendezvous-tasks/{rz_id}/report").json()
    assert rz_rep["passed"] is True
    assert len(rz_rep["segments"]) == 1
    ho_rep = client.get(f"/fences/{fence['id']}/handover-tasks/{ho_id}/report").json()
    assert ho_rep["passed"] is True
    assert ho_rep["passed_steps"] == 1


def test_isolation_report_reflects_new_events(client):
    # 围栏 A：小区；围栏 B：远处小区，初始与 A 无重叠
    fence_a = create_fence(
        client,
        geometry={"type": "Polygon", "coordinates": [
            [[0.20, 0.20], [0.21, 0.20], [0.21, 0.21], [0.20, 0.21], [0.20, 0.20]],
        ]},
        name="A",
    )
    fence_b = create_fence(
        client,
        geometry={"type": "Polygon", "coordinates": [
            [[0.30, 0.30], [0.31, 0.30], [0.31, 0.31], [0.30, 0.31], [0.30, 0.30]],
        ]},
        name="B",
    )
    iso_base = datetime(2026, 10, 4, 8, 0, 0, tzinfo=timezone.utc)
    task = client.post(
        "/isolation-tasks",
        json={
            "name": "互斥",
            "groups": [{
                "fence_ids": [fence_a["id"], fence_b["id"]],
                "start_at": iso(0, iso_base),
                "end_at": iso(3600, iso_base),
            }],
        },
    )
    # 隔离任务要求至少 2 组，补一个引用 A 的占位组
    task = client.post(
        "/isolation-tasks",
        json={
            "name": "互斥",
            "groups": [
                {
                    "fence_ids": [fence_a["id"], fence_b["id"]],
                    "start_at": iso(0, iso_base),
                    "end_at": iso(3600, iso_base),
                },
                {
                    "fence_ids": [fence_a["id"], fence_b["id"]],
                    "start_at": iso(3600, iso_base),
                    "end_at": iso(7200, iso_base),
                },
            ],
        },
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]

    submit(client, [
        pt("dev1", 10, 0.205, 0.205, base=iso_base),   # 在 A 内
    ])
    rep = client.get(f"/isolation-tasks/{task_id}/report").json()
    assert rep["passed"] is True and rep["devices"] == []

    # 调整围栏 B 使其覆盖 dev1 所在位置 -> 与 A 冲突
    new_b = {
        "type": "Polygon",
        "coordinates": [[
            [0.202, 0.202], [0.22, 0.202], [0.22, 0.22], [0.202, 0.22], [0.202, 0.202],
        ]],
    }
    update_fence(client, fence_b["id"], new_b)

    rep = client.get(f"/isolation-tasks/{task_id}/report").json()
    assert rep["passed"] is False
    assert len(rep["devices"]) == 1
    dev = rep["devices"][0]
    assert dev["device_id"] == "dev1"
    seg = dev["groups"][0]["segments"][0]
    assert seg["active_fence_ids"] == [fence_a["id"], fence_b["id"]]


def test_recompute_failure_rolls_back_definition_and_derivatives(client, monkeypatch):
    import app.main as main_mod

    fence = create_fence(client)
    create_gate(client, fence["id"])
    submit(client, [
        pt("dev1", 0, 0.005, 0.005),
        pt("dev1", 20, 0.02, 0.02),
    ])
    before = client.get(f"/fences/{fence['id']}").json()
    before_events = get_events(client, fence_id=fence["id"])
    assert before_events  # 调整前确有事件

    # 注入重算失败：模拟重放过程中抛错
    def boom(*args, **kwargs):
        raise RuntimeError("recompute boom")

    monkeypatch.setattr(main_mod, "compute_events", boom)

    # 重算异常向上抛出（端点内已先 rollback 再 raise）
    with pytest.raises(RuntimeError):
        client.put(
            f"/fences/{fence['id']}",
            json={"name": "扩大后的围栏", "geometry": EXPANDED_FENCE, "config": {}},
        )

    # 整体回滚：围栏定义与衍生记录都保持调整前状态
    after = client.get(f"/fences/{fence['id']}").json()
    assert after["geometry"] == before["geometry"] == PLAIN_FENCE
    assert after["name"] == "测试围栏"
    assert get_events(client, fence_id=fence["id"]) == before_events
    assert len(client.get(f"/fences/{fence['id']}/gates").json()) == 1

    # 回滚后同一会话工厂仍可正常服务（后续成功调整）
    monkeypatch.undo()
    updated = update_fence(client, fence["id"], EXPANDED_FENCE, name="扩大后的围栏")
    assert updated["geometry"] == EXPANDED_FENCE
