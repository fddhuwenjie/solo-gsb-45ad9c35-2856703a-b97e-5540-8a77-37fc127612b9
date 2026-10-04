from datetime import datetime, timedelta, timezone

from tests.test_api import (
    BASE,
    FENCE_WITH_HOLE,
    create_fence,
    get_events,
    pt,
    submit,
)


def correct(client, corrections):
    return client.post("/points/corrections", json={"corrections": corrections})


def corr(device, sec, lat, lon):
    return {
        "device_id": device,
        "ts": (BASE + timedelta(seconds=sec)).isoformat(),
        "lat": lat,
        "lon": lon,
    }


def iso(sec):
    return (BASE + timedelta(seconds=sec)).isoformat()


def test_correction_replays_events_state_and_crossings(client):
    fence_id = create_fence(client)
    # 三个点都在围栏外，无事件、状态 outside
    submit(client, [
        pt("dev1", 0, 0.02, 0.02),
        pt("dev1", 10, 0.021, 0.021),
        pt("dev1", 20, 0.02, 0.02),
    ])
    assert get_events(client, device_id="dev1") == []
    st = client.get("/devices/dev1/status").json()["fences"][0]
    assert st["state"] == "outside"

    # 把 t=10 的点更正到围栏内 -> 重放出 enter/leave
    r = correct(client, [corr("dev1", 10, 0.005, 0.005)])
    assert r.status_code == 200, r.text
    assert r.json() == {"corrected": 1, "devices_updated": 1}

    events = get_events(client, device_id="dev1")
    assert [e["type"] for e in events] == ["enter", "leave"]
    assert datetime.fromisoformat(events[0]["ts"]) == BASE + timedelta(seconds=10)
    assert datetime.fromisoformat(events[1]["ts"]) == BASE + timedelta(seconds=20)

    st = client.get("/devices/dev1/status").json()["fences"][0]
    assert st["state"] == "outside"
    assert datetime.fromisoformat(st["since"]) == BASE + timedelta(seconds=20)

    # 再把 t=20 的点也更正到围栏内 -> 设备最终 inside，旧 leave 消失
    r = correct(client, [corr("dev1", 20, 0.005, 0.005)])
    assert r.status_code == 200
    events = get_events(client, device_id="dev1")
    assert [e["type"] for e in events] == ["enter"]
    st = client.get("/devices/dev1/status").json()["fences"][0]
    assert st["state"] == "inside"
    assert datetime.fromisoformat(st["since"]) == BASE + timedelta(seconds=10)


def test_correction_refreshes_gate_crossings(client):
    fence_id = create_fence(client)
    # 门线：西→东，横在围栏中部
    r = client.post(
        f"/fences/{fence_id}/gates",
        json={
            "geometry": {
                "type": "LineString",
                "coordinates": [[0.002, 0.005], [0.008, 0.005]],
            },
            "forward_name": "南向北",
            "reverse_name": "北向南",
            "min_displacement_m": 0,
        },
    )
    assert r.status_code == 201, r.text

    # 原轨迹两点都在门线南侧 -> 无穿越
    submit(client, [
        pt("dev1", 0, 0.004, 0.005),
        pt("dev1", 10, 0.0041, 0.005),
    ])
    crossings = client.get(
        "/crossings", params={"device_id": "dev1", "start": iso(0), "end": iso(60)}
    ).json()
    assert crossings == []

    # 更正 t=10 点到门线北侧 -> 产生一次 forward 穿越
    r = correct(client, [corr("dev1", 10, 0.006, 0.005)])
    assert r.status_code == 200
    crossings = client.get(
        "/crossings", params={"device_id": "dev1", "start": iso(0), "end": iso(60)}
    ).json()
    assert len(crossings) == 1
    assert crossings[0]["direction"] == "forward"
    # 更正回去 -> 旧穿越记录不残留
    r = correct(client, [corr("dev1", 10, 0.0041, 0.005)])
    assert r.status_code == 200
    crossings = client.get(
        "/crossings", params={"device_id": "dev1", "start": iso(0), "end": iso(60)}
    ).json()
    assert crossings == []


def test_missing_record_rejects_whole_batch(client):
    create_fence(client)
    submit(client, [pt("dev1", 0, 0.005, 0.005)])
    before = get_events(client, device_id="dev1")

    # 一个存在、一个不存在 -> 整批拒绝
    r = correct(client, [
        corr("dev1", 0, 0.006, 0.006),
        corr("dev1", 99, 0.006, 0.006),
    ])
    assert r.status_code == 422
    assert "missing" in r.json()["detail"]
    # 存在的那个点坐标也未变（事件保持原样）
    assert get_events(client, device_id="dev1") == before

    # 设备根本不存在同样 422
    r = correct(client, [corr("ghost", 0, 0.005, 0.005)])
    assert r.status_code == 422


def test_duplicate_target_in_batch_rejects(client):
    create_fence(client)
    submit(client, [pt("dev1", 0, 0.005, 0.005), pt("dev1", 10, 0.005, 0.005)])
    before = get_events(client, device_id="dev1")

    r = correct(client, [
        corr("dev1", 0, 0.02, 0.02),
        corr("dev1", 0, 0.006, 0.006),
    ])
    assert r.status_code == 422
    assert get_events(client, device_id="dev1") == before


def test_illegal_coordinates_reject_batch(client):
    create_fence(client)
    submit(client, [pt("dev1", 0, 0.005, 0.005)])
    before = get_events(client, device_id="dev1")

    # lat 越界
    r = correct(client, [corr("dev1", 0, 91, 0.0)])
    assert r.status_code == 422
    # lon 越界
    r = correct(client, [corr("dev1", 0, 0.0, -180.1)])
    assert r.status_code == 422
    # NaN（FastAPI 默认 JSON 解析器接受 NaN 字面量）
    r = client.post(
        "/points/corrections",
        content='{"corrections": [{"device_id": "dev1", "ts": "%s", "lat": NaN, "lon": 0}]}'
        % iso(0),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert get_events(client, device_id="dev1") == before


def test_other_devices_unchanged(client):
    create_fence(client, geometry=FENCE_WITH_HOLE)
    submit(client, [
        pt("dev1", 0, 0.005, 0.005),
        pt("dev2", 0, 0.005, 0.005),
        pt("dev2", 10, 0.02, 0.02),
    ])
    dev2_before = get_events(client, device_id="dev2")

    r = correct(client, [corr("dev1", 0, 0.02, 0.02)])
    assert r.status_code == 200
    assert r.json()["devices_updated"] == 1
    assert get_events(client, device_id="dev2") == dev2_before


def test_resubmit_duplicate_point_is_not_correction(client):
    create_fence(client)
    submit(client, [pt("dev1", 0, 0.02, 0.02)])
    # 同一 (device_id, ts) 用新坐标重提 /points：仍按重复忽略，坐标不变
    r = submit(client, [pt("dev1", 0, 0.005, 0.005)])
    assert r == {"accepted": 0, "duplicates": 1, "devices_updated": 0}
    assert get_events(client, device_id="dev1") == []

    # 更正接口才会改坐标并产出事件
    r = correct(client, [corr("dev1", 0, 0.005, 0.005)])
    assert r.status_code == 200
    assert [e["type"] for e in get_events(client, device_id="dev1")] == ["enter"]


def test_correction_visible_in_occupancy_report(client):
    fence_id = create_fence(client)
    client.post(
        f"/fences/{fence_id}/capacity-rules",
        json={"effective_from": iso(0), "max_occupancy": 1},
    )
    submit(client, [
        pt("dev1", 0, 0.02, 0.02),
        pt("dev2", 0, 0.005, 0.005),
        pt("dev2", 30, 0.02, 0.02),
    ])
    occ = client.get(
        f"/fences/{fence_id}/occupancy", params={"start": iso(0), "end": iso(60)}
    ).json()
    assert not any(s["exceeded"] for s in occ["segments"])

    # 更正 dev1 的点进入围栏 -> 0～30s 两人在场，超限
    r = correct(client, [corr("dev1", 0, 0.005, 0.005)])
    assert r.status_code == 200
    occ = client.get(
        f"/fences/{fence_id}/occupancy", params={"start": iso(0), "end": iso(60)}
    ).json()
    exceeded = [s for s in occ["segments"] if s["exceeded"]]
    assert len(exceeded) == 1
    assert exceeded[0]["devices"] == ["dev1", "dev2"]


def test_correction_visible_in_clearance_report(client):
    fence_id = create_fence(client)
    task = client.post(
        f"/fences/{fence_id}/clearance-tasks",
        json={"name": "清场", "start_at": iso(0), "deadline": iso(60)},
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]

    # 原轨迹在围栏外：起始名单为空
    submit(client, [pt("dev1", 0, 0.02, 0.02), pt("dev1", 30, 0.02, 0.02)])
    report = client.get(f"/fences/{fence_id}/clearance-tasks/{task_id}/report").json()
    assert report["summary"] == {"start_count": 0, "cleared_count": 0, "remaining_count": 0}

    # 更正为窗口内在场且未离开 -> 起始名单 1 人且 remaining
    r = correct(client, [
        corr("dev1", 0, 0.005, 0.005),
        corr("dev1", 30, 0.005, 0.005),
    ])
    assert r.status_code == 200
    report = client.get(f"/fences/{fence_id}/clearance-tasks/{task_id}/report").json()
    assert report["summary"] == {"start_count": 1, "cleared_count": 0, "remaining_count": 1}


def test_failed_recompute_rolls_back_coordinates(client, monkeypatch):
    create_fence(client)
    submit(client, [pt("dev1", 0, 0.02, 0.02), pt("dev1", 10, 0.02, 0.02)])
    before = get_events(client, device_id="dev1")

    import app.main as main_mod

    def boom(db, device_id, commit=True):
        raise RuntimeError("recompute failed")

    monkeypatch.setattr(main_mod, "recompute_device", boom)
    from fastapi.testclient import TestClient

    with TestClient(main_mod.app, raise_server_exceptions=False) as raw_client:
        r = raw_client.post(
            "/points/corrections",
            json={"corrections": [corr("dev1", 10, 0.005, 0.005)]},
        )
    assert r.status_code == 500
    monkeypatch.undo()

    # 重算失败后原坐标仍在：事件与重放结果保持原样（不会新坐标配旧事件）
    assert get_events(client, device_id="dev1") == before
    # 且接口恢复后再次更正可以正常生效
    r = correct(client, [corr("dev1", 10, 0.005, 0.005)])
    assert r.status_code == 200
    assert [e["type"] for e in get_events(client, device_id="dev1")] == ["enter"]


def test_correction_serializes_after_ingestion_order(client):
    # 更正与摄入共用串行锁；功能层面验证混用两类写操作后结果一致
    fence_id = create_fence(client)
    submit(client, [pt("dev1", 0, 0.02, 0.02)])
    correct(client, [corr("dev1", 0, 0.005, 0.005)])
    submit(client, [pt("dev1", 10, 0.02, 0.02)])
    events = get_events(client, device_id="dev1")
    assert [e["type"] for e in events] == ["enter", "leave"]
    assert datetime.fromisoformat(events[1]["ts"]) == BASE + timedelta(seconds=10)
