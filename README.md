# 轨迹围栏事件 API（Geofence Trajectory Events）

基于 **FastAPI + SQLite + Shapely** 的地理围栏事件服务：

- 创建**带孔多边形**围栏（GeoJSON Polygon，支持 holes），每围栏可配置判定参数
- **调整已有围栏**：整体替换 GeoJSON Polygon 与完整判定参数，新边界与去抖规则对存量轨迹重放生效；现有门线越出新区域或穿过新孔洞时整次拒绝并列出冲突门线 ID，定义与衍生记录一次提交、失败整体回滚
- 批量提交轨迹点（设备 ID + 时间戳 + 经纬度），**乱序点按设备时间自动重排**
- 边界抖动去抖：可配置的**进入/离开缓冲距离**（迟滞带）+ **停留时长**（dwell）
- 产出 `enter` / `leave` / `stay` 事件；**重复提交轨迹点不会重复产出事件**（幂等）
- **批量坐标更正**：对已入库轨迹点按设备 ID + 原时间戳定位并提交新经纬度；缺失记录、同批重复目标或非法坐标整批拒绝，更正与衍生结果一次提交，重放失败自动回滚
- **围栏容量核算**：按生效时刻维护正整数容量上限规则，查询任意 `[start, end)` 窗口的占用分段与超限明细
- **定向门线穿越核算**：围栏内保存简单 GeoJSON LineString 门线（正/反向名称 + 最小位移），只承认相邻轨迹点对门线的**单点横穿**，穿越时刻按交点比例插值
- **有序门线巡检路线核验**：路线保存 2～8 个按序排列的门线检查点（门线 + 方向 + 与上一点的最长间隔，首点无间隔）；核验设备在 `[start, end)` 窗口内是否按序、按方向且不超间隔地逐点通过，报告实时由当前穿越记录重算
- **应急清场核验**：为围栏创建含名称、开始时刻、截止时刻的清场任务；报告从 enter/leave 事件实时还原起始在场名单并逐机核验 cleared/remaining
- **协同到场核验**：为围栏创建 2～8 台设备组成的协同任务（有效窗口 + 最短共同在场时长，设备不得重复）；报告从 enter/leave 事件实时还原各设备半开在场区间，求全体成员共同在场的连续片段，只有窗口内持续达到时长的片段才算通过
- **设备接力交班核验**：为围栏创建 2～8 台**按交班顺序排列且不重复**的设备（有效窗口 + 相邻交班允许的最大空档 + 最短交叠时长）；报告从 enter/leave 事件实时还原每台设备的半开在场区间，选择通过相邻交班步数最多的连续交班链，返回每步实际区间、状态与首个违规时刻
- **隔离冲突核验**：任务配置 2～8 个**互斥围栏组**（每组 2～4 个已存在且不重复的围栏，各带有效窗口，围栏可跨组复用）；同一设备在组内同时处于两个及以上围栏即冲突，各组独立判定。报告合并各围栏当前 enter/leave 事件按半开区间重建设备状态，按设备与围栏组返回连续冲突片段、起止时刻、冲突时活跃围栏与首个冲突时刻
- 按时间范围/设备/围栏/类型查询事件，查询设备当前状态

## 快速开始（Docker Compose 一键启动）

```bash
docker compose up --build
```

- 服务端口：**8000**（http://localhost:8000）
- 交互式 API 文档：http://localhost:8000/docs
- SQLite 数据持久化在名为 `geofence-data` 的卷中

停止并清理：`docker compose down`（加 `-v` 同时删除数据卷）

## 判定逻辑说明

1. **投影**：以围栏质心为原点做等距圆柱投影，把经纬度转为米制坐标后用 Shapely 计算点到多边形的距离 `d`（点在多边形内 `d=0`；**孔洞内 `d>0`，天然视为外部**）。
2. **缓冲迟滞**：
   - `d <= enter_buffer_m` → 判定为内部（可略提前触发进入）
   - `d > exit_buffer_m` → 判定为外部
   - 两者之间为迟滞灰区 → **维持上一状态**，抑制边界来回抖动
3. **停留去抖（dwell）**：状态迁移条件首次满足时开始计时，持续满足 `enter_dwell_s` / `exit_dwell_s` 才确认迁移并产出事件；期间条件被打破则作废。事件时间取**条件首次满足的轨迹时刻**。
4. **stay**：进入围栏后连续停留达到 `stay_min_s` 产出一次 `stay`（每次访问最多一次）。
5. **乱序与幂等**：轨迹点按 `(device_id, ts)` 唯一约束去重；每次摄入后按设备全量轨迹（时间升序）重算事件，因此乱序到达与按序到达结果一致，重复提交结果不变。

## 门线穿越判定逻辑

1. **门线几何**：门线是围栏内的简单（不自交、无重复点）GeoJSON `LineString`，**整条线必须位于围栏有效区域内**——不得越出外环（落在外边界上允许），也不得穿入孔洞或贴着孔洞边界。创建时按围栏投影做 `covers` + 孔洞边界相交校验，不满足返回 `422`。
2. **方向定义**：方向按门线坐标顺序判定。沿坐标顺序看，**右 → 左为正向（forward）**，左 → 右为反向（reverse），查询结果分别以门线的 `forward_name` / `reverse_name` 表示。
3. **横穿条件**：轨迹按设备时间升序重排，逐对相邻点形成的线段与门线检测，**只承认单点横穿（proper crossing）**——交点严格位于轨迹线段内部，且轨迹两端分居门线两侧。交点严格落在某段内部时按一次横穿；交点恰好落在门线**内部折点**（非首尾端点）时，在整条门线层面归并共享该折点的两侧邻接段：
   - 折点两侧邻接段**分居轨迹两侧**（方向判断一致）→ 归并为**一次**横穿（避免逐段统计时漏记或重复）；
   - 两侧邻接段在轨迹**同侧** → 凸角擦碰，忽略。
   以下情况一律忽略：
   - **沿线**：轨迹线段与门线段共线（含重合/部分重叠）；
   - **仅接触端点**：轨迹止于/始于门线，或交点恰好是门线首尾端点；轨迹端点恰落在内部折点同样忽略；
   - **同侧擦碰**：两端在门线同一侧，或穿内部折点时折点两侧邻接段同处轨迹一侧；
   - **多交点歧义**：同一轨迹段与门线产生多于一个有效交点（如同时横穿两段）；
   - **位移不足**：相邻点直线位移 `< min_displacement_m`。
4. **穿越时刻**：取交点在轨迹线段上的长度/参数比例 `t∈(0,1)`，在两端点时间戳间线性插值 `ts = t0 + t·(t1 - t0)`；交点经纬度随结果一并返回。
5. **多门线排序**：同一轨迹段命中多条门线时，结果按 **(插值时刻, 门线 ID)** 升序排列。
6. **乱序重算与幂等**：穿越记录与事件一样在每次摄入后按设备全量重算并整体替换；补录乱序点（包括更早的点）后结果自动更新，**不残留旧记录、不产生重复**。新建门线也会对存量轨迹补算。
7. **与围栏状态机解耦**：穿越是纯几何事件，不受 enter/leave 缓冲迟滞、dwell 与孔洞状态判定影响（例如门线贴在外环上时，围栏外的点横穿门线同样核算）。

## 示例请求

### 1. 创建带孔围栏

```bash
curl -X POST http://localhost:8000/fences \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "园区A（中央湖泊为孔洞）",
    "geometry": {
      "type": "Polygon",
      "coordinates": [
        [[116.30, 39.90], [116.32, 39.90], [116.32, 39.92], [116.30, 39.92], [116.30, 39.90]],
        [[116.308, 39.908], [116.312, 39.908], [116.312, 39.912], [116.308, 39.912], [116.308, 39.908]]
      ]
    },
    "config": {
      "enter_buffer_m": 0,
      "exit_buffer_m": 50,
      "enter_dwell_s": 10,
      "exit_dwell_s": 10,
      "stay_min_s": 600
    }
  }'
```

返回 `201` 及围栏对象（含 `id`，下文假设为 `1`）。

### 2. 提交轨迹点（乱序、含重复）

```bash
curl -X POST http://localhost:8000/points \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:20Z", "lat": 39.910, "lon": 116.305},
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:00Z", "lat": 39.900, "lon": 116.300},
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:10Z", "lat": 39.905, "lon": 116.302},
      {"device_id": "dev-001", "ts": "2026-09-22T08:00:20Z", "lat": 39.910, "lon": 116.305}
    ]
  }'
```

返回：`{"accepted": 3, "duplicates": 1, "devices_updated": 1}`

### 3. 按时间范围查询事件

```bash
curl 'http://localhost:8000/events?device_id=dev-001&start=2026-09-22T00:00:00Z&end=2026-09-23T00:00:00Z'
# 可选过滤：fence_id=1&type=enter&limit=100&offset=0
```

### 4. 查询设备当前状态

```bash
curl http://localhost:8000/devices/dev-001/status
```

返回示例：

```json
{
  "device_id": "dev-001",
  "fences": [
    {"fence_id": 1, "fence_name": "园区A（中央湖泊为孔洞）", "state": "inside", "since": "2026-09-22T08:00:10Z"}
  ]
}
```

### 5. 新增容量规则

```bash
curl -X POST http://localhost:8000/fences/1/capacity-rules \
  -H 'Content-Type: application/json' \
  -d '{"effective_from": "2026-09-22T08:00:00Z", "max_occupancy": 2}'
```

- `max_occupancy` 必须是**正整数**（否则 `422`）
- 同一围栏同一 `effective_from` 重复提交返回 `409`
- 某时刻生效的上限 = 生效时刻 ≤ 该时刻的最近一条规则；此前无规则则该时段不限容（`limit` 为 `null`）
- 管理接口：`GET /fences/1/capacity-rules`（按生效时刻升序）、`DELETE /fences/1/capacity-rules/{rule_id}`

### 6. 查询占用分段（容量核算）

```bash
curl 'http://localhost:8000/fences/1/occupancy?start=2026-09-22T08:00:00Z&end=2026-09-22T09:00:00Z'
```

返回示例：

```json
{
  "fence_id": 1,
  "start": "2026-09-22T08:00:00Z",
  "end": "2026-09-22T09:00:00Z",
  "segments": [
    {"start": "2026-09-22T08:00:00Z", "end": "2026-09-22T08:10:00Z", "count": 1, "limit": 2, "exceeded": false, "excess": null, "devices": null},
    {"start": "2026-09-22T08:10:00Z", "end": "2026-09-22T08:20:00Z", "count": 3, "limit": 2, "exceeded": true, "excess": 1, "devices": ["dev-001", "dev-002", "dev-003"]},
    {"start": "2026-09-22T08:20:00Z", "end": "2026-09-22T09:00:00Z", "count": 2, "limit": 2, "exceeded": false, "excess": null, "devices": null}
  ]
}
```

**语义说明**：

1. **在场区间还原**：由该围栏的 `enter`/`leave` 事件重放出每台设备的半开区间 `[enter, leave)`；查询开始前已进入、查询结束仍未离开的区间被正确截断到 `[start, end)`。同一设备同刻离开又进入时按**离开先、进入后**配对（旧区间止端不含该刻、新区间起端含该刻，得到首尾相接的两段），与清场、协同、交班报告共用同一套半开边界，不会丢失新一段在场区间。
2. **原子边界**：同一时刻的设备进/出与容量规则切换合并为一个边界，分段在该时刻一次性切换，不产生中间态或零长分段。
3. **恒定状态分段**：分段内在场设备集合与生效上限均不变；相邻同状态分段已合并，返回尽可能长的分段。
4. **超限明细**：`exceeded=true` 的分段给出 `limit`（上限）、`count`（实际人数）、`excess`（超额数）与按 ID 排序的 `devices`；其余分段 `excess`/`devices` 为 `null`。
5. **重算一致**：占用在查询时由事件现算；补录乱序轨迹点触发事件重算后，再次查询即反映最新结果，不残留旧的占用或超限片段。

### 7. 新增定向门线

```bash
curl -X POST http://localhost:8000/fences/1/gates \
  -H 'Content-Type: application/json' \
  -d '{
    "geometry": {
      "type": "LineString",
      "coordinates": [[116.305, 39.910], [116.315, 39.910]]
    },
    "forward_name": "南向北（入园）",
    "reverse_name": "北向南（出园）",
    "min_displacement_m": 5
  }'
```

- 门线坐标顺序为 **西 → 东** 时，门线右侧为南、左侧为北：南→北穿入即 `forward`（入园），北→南即 `reverse`（出园）。
- 整条线越出围栏有效区域、穿过/接触孔洞、非 `LineString`、单点、重复点或自相交，均返回 `422`；围栏不存在返回 `404`。
- 管理接口：`GET /fences/1/gates`、`DELETE /fences/1/gates/{gate_id}`（删除门线同时清除其穿越记录）。

### 8. 查询门线穿越

提交一段南北往返轨迹后：

```bash
curl -X POST http://localhost:8000/points -H 'Content-Type: application/json' \
  -d '{"points": [
    {"device_id": "dev-001", "ts": "2026-09-22T08:00:00Z", "lat": 39.909, "lon": 116.310},
    {"device_id": "dev-001", "ts": "2026-09-22T08:00:20Z", "lat": 39.911, "lon": 116.310},
    {"device_id": "dev-001", "ts": "2026-09-22T08:01:00Z", "lat": 39.909, "lon": 116.310}
  ]}'

curl 'http://localhost:8000/crossings?device_id=dev-001&start=2026-09-22T08:00:00Z&end=2026-09-22T09:00:00Z'
```

返回（穿越时刻分别插值到 `08:00:10` 与 `08:00:40`）：

```json
[
  {"id": 1, "device_id": "dev-001", "fence_id": 1, "gate_id": 1,
   "direction": "forward", "direction_name": "南向北（入园）",
   "ts": "2026-09-22T08:00:10Z", "lat": 39.91, "lon": 116.31},
  {"id": 2, "device_id": "dev-001", "fence_id": 1, "gate_id": 1,
   "direction": "reverse", "direction_name": "北向南（出园）",
   "ts": "2026-09-22T08:00:40Z", "lat": 39.91, "lon": 116.31}
]
```

- 查询参数：`device_id`、`gate_id`、`fence_id`、`direction=forward|reverse`、`start`/`end`（半开 `[start,end)`，必须同时给出，否则 `422`）、`limit`、`offset`。
- 结果固定按 `(ts, gate_id, id)` 升序返回，便于同一轨迹段命中多门线时还原顺序。

### 9. 边界情形示例（均不产生 crossing）

门线取 `LineString [(lon=116.305,lat=39.910), (116.315,39.910)]`（西→东），表中点按 `(纬度, 经度)` 书写：

| 情形 | 轨迹相邻点 | 结果 |
| --- | --- | --- |
| 沿线 | `(39.910,116.308)` → `(39.910,116.312)` | 共线，忽略 |
| 仅接触端点 | `(39.909,116.305)` → `(39.910,116.305)` | 交点是门线端点，忽略 |
| 跨端点穿过 | `(39.909,116.305)` → `(39.911,116.305)` | 交点仍在门线端点，忽略 |
| 同侧擦碰 | `(39.909,116.308)` → `(39.909,116.312)` | 两端同在南侧，忽略 |
| 端点落在线上 | `(39.910,116.310)` → `(39.909,116.311)` | 一端在线上未穿越，忽略 |
| 位移不足 | 南→北跨门线但位移 `< min_displacement_m` | 忽略 |
| 正向横穿 | `(39.909,116.310)` → `(39.911,116.310)` | **forward，时刻插值在中点** |
| 反向横穿 | `(39.911,116.310)` → `(39.909,116.310)` | **reverse，时刻插值在中点** |

含折点门线（在示例门线东端 `(39.910,116.315)` 续接一段南北向门线，折点即该端点）的补充情形（点仍按 `(纬度, 经度)` 书写）：

| 情形 | 轨迹相邻点 | 结果 |
| --- | --- | --- |
| 穿内部折点横穿 | `(39.909,116.316)` → `(39.911,116.314)` | 折点两侧邻接段分居轨迹两侧，**归并一次 crossing，交点取折点、时刻按比例插值** |
| 折点同侧擦碰 | `(39.909,116.314)` → `(39.911,116.316)` | 两侧邻接段同在轨迹一侧，忽略 |
| 轨迹端点在折点 | `(39.910,116.315)` → `(39.911,116.314)` | 交点恰在轨迹端点，忽略 |
| 沿门线段穿折点 | `(39.910,116.312)` → `(39.910,116.317)` | 与西→东门线段共线（沿线），忽略 |

### 10. 应急清场核验

为围栏创建清场任务（名称 + 开始时刻 + 截止时刻，开始必须严格早于截止）：

```bash
curl -X POST http://localhost:8000/fences/1/clearance-tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "台风应急清场",
    "start_at": "2026-09-30T12:02:00Z",
    "deadline": "2026-09-30T12:06:00Z"
  }'
```

返回 `201` 及任务对象（含 `id`，下文假设为 `1`）。开始时刻不早于截止时刻返回 `422`，围栏不存在返回 `404`。
管理接口：`GET /fences/1/clearance-tasks`（按 id 升序）、`DELETE /fences/1/clearance-tasks/{task_id}`；**删除围栏时其清场任务一并删除**。

提交一段示例轨迹（围栏配置了 50m 离开缓冲与 10s 停留确认，故界外点取缓冲带外，且每次状态变化都补了确认点）：

```bash
curl -X POST http://localhost:8000/points -H 'Content-Type: application/json' \
  -d '{"points": [
    {"device_id": "dev-001", "ts": "2026-09-30T12:01:40Z", "lat": 39.905, "lon": 116.305},
    {"device_id": "dev-001", "ts": "2026-09-30T12:01:55Z", "lat": 39.905, "lon": 116.305},
    {"device_id": "dev-001", "ts": "2026-09-30T12:02:50Z", "lat": 39.894, "lon": 116.294},
    {"device_id": "dev-001", "ts": "2026-09-30T12:03:05Z", "lat": 39.894, "lon": 116.294},
    {"device_id": "dev-002", "ts": "2026-09-30T12:02:00Z", "lat": 39.905, "lon": 116.305},
    {"device_id": "dev-002", "ts": "2026-09-30T12:02:15Z", "lat": 39.905, "lon": 116.305},
    {"device_id": "dev-002", "ts": "2026-09-30T12:03:00Z", "lat": 39.894, "lon": 116.294},
    {"device_id": "dev-002", "ts": "2026-09-30T12:03:30Z", "lat": 39.894, "lon": 116.294},
    {"device_id": "dev-002", "ts": "2026-09-30T12:03:40Z", "lat": 39.905, "lon": 116.305},
    {"device_id": "dev-002", "ts": "2026-09-30T12:04:00Z", "lat": 39.905, "lon": 116.305},
    {"device_id": "dev-003", "ts": "2026-09-30T12:02:30Z", "lat": 39.905, "lon": 116.305},
    {"device_id": "dev-003", "ts": "2026-09-30T12:04:00Z", "lat": 39.905, "lon": 116.305}
  ]}'

curl http://localhost:8000/fences/1/clearance-tasks/1/report
```

返回示例：

```json
{
  "id": 1,
  "fence_id": 1,
  "name": "台风应急清场",
  "start_at": "2026-09-30T12:02:00Z",
  "deadline": "2026-09-30T12:06:00Z",
  "created_at": "2026-09-30T11:50:00Z",
  "summary": {"start_count": 2, "cleared_count": 1, "remaining_count": 1},
  "devices": [
    {"device_id": "dev-001", "status": "cleared", "last_leave_at": "2026-09-30T12:02:50Z"},
    {"device_id": "dev-002", "status": "remaining", "last_leave_at": null}
  ]
}
```

**语义说明**：

1. **起始名单还原**：报告由该围栏当前的 `enter`/`leave` 事件重放，在场区间一律按半开区间 **`[enter, leave)`** 处理。设备在开始时刻处于某个开着的区间内才进入起始名单——**恰在开始时刻 `enter` 的计入，恰在开始时刻 `leave` 的排除**。上例 `dev-002` 12:02:00 的轨迹点在开始时刻满足进入（事件时间取该时刻，计入名单），`dev-001` 开始前已在场（计入）；`dev-003` 12:02:30 才进入，开始时刻尚在场外，不在起始名单内，即使截止时仍在场也不核对。
2. **半开边界贯穿截止**：截止时刻沿用同一边界——恰在截止时刻 `leave` 的设备已在外，计为 `cleared`；恰在截止时刻 `enter` 的设备在场，计为 `remaining`。
3. **只核对起始名单**：窗口内首次进入的设备不参与汇总。
4. **折返规则**：名单内设备在窗口内离开后又进入的，**必须再次离开并持续在外至截止**才算按时清场（`cleared`）。上例 `dev-002` 12:03:00 离开、12:03:40 又进入且未再离开，故仍为 `remaining`；若此后补录两个 12:04:30 之后的界外点使其再次离开并持续在外至截止，再查报告即变为 `cleared`，`last_leave_at` 取**最后一次有效离开时刻**（12:04:30）。截止时仍在场（含折返未走）的设备 `last_leave_at` 为 `null`。
5. **汇总与排序**：`summary` 给出 `start_count`（起始人数）、`cleared_count`（按时清场）、`remaining_count`（未清场）；`devices` 按设备 ID 升序。
6. **实时重建、无快照**：报告**每次查询都从当前事件重建**，不落库任何结果。补录乱序轨迹点触发事件重算后，再次查询得到的起始名单与 cleared/remaining 结果同步更新，不保留旧快照。

### 11. 有序门线巡检路线核验

在围栏内创建有序门线巡检路线：2～8 个**按顺序排列**的门线检查点，每项指定一条**已有门线**、要求的穿越方向（`forward`/`reverse`）以及与上一检查点通过时刻的**最长间隔秒数**；首点不设间隔。

```bash
curl -X POST http://localhost:8000/fences/1/inspection-routes \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "南门入园巡检",
    "checkpoints": [
      {"gate_id": 1, "direction": "forward"},
      {"gate_id": 2, "direction": "forward", "max_interval_s": 120},
      {"gate_id": 3, "direction": "reverse", "max_interval_s": 180}
    ]
  }'
```

- 创建时校验：检查点数量必须为 **2～8**；门线必须**全部存在且属于该围栏**（否则 `422`）；路线内**门线不得重复**（否则 `422`）；**首点**不得给 `max_interval_s`、其余各点必须给出非负间隔（否则 `422`）；围栏不存在返回 `404`。
- **删除保护**：被任一巡检路线引用的门线不得删除（返回 `409`，请先删除引用它的路线）；删除围栏时其路线与检查点一并删除。
- 管理接口：`GET /fences/1/inspection-routes`（按 id 升序，检查点按 `position` 升序）、`DELETE /fences/1/inspection-routes/{route_id}`。

提交设备轨迹后核验给定设备与半开时间窗 `[start, end)`：

```bash
curl 'http://localhost:8000/fences/1/inspection-routes/1/report?device_id=dev-001&start=2026-10-01T08:00:00Z&end=2026-10-01T09:00:00Z'
```

返回示例（全部通过）：

```json
{
  "id": 1,
  "fence_id": 1,
  "name": "南门入园巡检",
  "checkpoints": [
    {"position": 0, "gate_id": 1, "direction": "forward", "direction_name": "南向北（入园）", "max_interval_s": null},
    {"position": 1, "gate_id": 2, "direction": "forward", "direction_name": "南向北（二道岗）", "max_interval_s": 120.0},
    {"position": 2, "gate_id": 3, "direction": "reverse", "direction_name": "北向南（折返）", "max_interval_s": 180.0}
  ],
  "created_at": "2026-10-01T07:30:00Z",
  "device_id": "dev-001",
  "start": "2026-10-01T08:00:00Z",
  "end": "2026-10-01T09:00:00Z",
  "passed": true,
  "first_violation_at": null,
  "results": [
    {"position": 0, "gate_id": 1, "direction": "forward", "direction_name": "南向北（入园）",
     "status": "passed", "passed_at": "2026-10-01T08:00:10Z", "failed_at": null},
    {"position": 1, "gate_id": 2, "direction": "forward", "direction_name": "南向北（二道岗）",
     "status": "passed", "passed_at": "2026-10-01T08:01:40Z", "failed_at": null},
    {"position": 2, "gate_id": 3, "direction": "reverse", "direction_name": "北向南（折返）",
     "status": "passed", "passed_at": "2026-10-01T08:03:30Z", "failed_at": null}
  ]
}
```

失败示例（检查点 1 超时）：

```json
{
  "passed": false,
  "first_violation_at": "2026-10-01T08:02:10Z",
  "results": [
    {"position": 0, "gate_id": 1, "direction": "forward", "direction_name": "南向北（入园）",
     "status": "passed", "passed_at": "2026-10-01T08:00:10Z", "failed_at": null},
    {"position": 1, "gate_id": 2, "direction": "forward", "direction_name": "南向北（二道岗）",
     "status": "timeout", "passed_at": null, "failed_at": "2026-10-01T08:02:10Z"},
    {"position": 2, "gate_id": 3, "direction": "reverse", "direction_name": "北向南（折返）",
     "status": "not_reached", "passed_at": null, "failed_at": null}
  ]
}
```

**核验语义**：

1. **按当前穿越记录时间重放**：取窗口内（`start <= ts < end`）该设备在路线门线上的穿越，按 `(ts, gate_id)` 升序，从检查点 0 开始逐个匹配。穿越时刻沿用门线核算的交点插值时刻。
2. **推进规则**：只有穿越**下一个待匹配检查点**的门线**且方向一致**，才推进到下一检查点并记录该穿越时刻为通过时刻。
3. **顺序违规（`sequence_violation`）**，立即终止重放，首个违规时刻即该穿越时刻：
   - 当前检查点门线的**反向穿越**（违规记在当前检查点）；
   - **任意后续检查点**门线的穿越（无论正反，说明先穿了后面的门；违规记在被穿的那个后续检查点）。
4. **超时（`timeout`）**：自上一检查点通过时刻（首点为窗口 `start`）起，穿越发生时已**严格超过** `max_interval_s`（间隔**等于上限允许**），立即在**间隔截止时刻**标记超时——即使之后补穿匹配门线也不恢复；窗口结束仍未匹配且间隔已超限的，同样在间隔截止时刻标记超时。处理任一条穿越前先判超时，故超时优先于同一时刻之后的顺序违规。
5. **忽略**：非路线门线的穿越、已通过检查点门线的再次穿越、窗口外的穿越一律忽略。
6. **未到达（`not_reached`）**：此前已失败的剩余检查点，以及窗口结束时仍未匹配且尚未超间隔的检查点，均为 `not_reached`，无通过/失败时刻。
7. **结果字段**：`passed` 仅当全部检查点均为 `passed`；`first_violation_at` 为首个 `timeout`/`sequence_violation` 的 `failed_at`（全部通过为 `null`）；`results` 按检查点顺序给出每点的 `status`、`passed_at` 与 `failed_at`。
8. **实时重建、无快照**：报告**每次查询都从当前穿越记录重放**，不落库任何结果。补录乱序轨迹点（包括更早的点）触发穿越全量重算后，再次查询即按最新穿越结果重算通过时刻与违规判定，不保留旧快照。
9. `start >= end` 返回 `422`；围栏/路线不存在或路线不属于该围栏返回 `404`。

### 12. 协同到场核验

为围栏创建协同到场任务：**2～8 台**协同设备（设备 ID **不得重复**）、半开有效窗口 `[start_at, end_at)` 与**最短共同在场时长**（秒，正数；持续时长**等于**门槛即算达标）。

```bash
curl -X POST http://localhost:8000/fences/1/rendezvous-tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "班前集合点名",
    "members": ["dev-001", "dev-002", "dev-003"],
    "start_at": "2026-09-30T12:00:00Z",
    "end_at": "2026-09-30T12:10:00Z",
    "min_co_presence_s": 120
  }'
```

- 创建校验：成员数量必须为 **2～8**；**设备 ID 不得重复、不得为空**（否则 `422`）；窗口必须有效（`start_at < end_at`，否则 `422`）；`min_co_presence_s` 必须为正数（否则 `422`）；围栏不存在返回 `404`。
- 管理接口：`GET /fences/1/rendezvous-tasks`（按 id 升序，成员按提交顺序）、`DELETE /fences/1/rendezvous-tasks/{task_id}`；**删除围栏时其协同任务与成员一并删除**。

提交三台设备的轨迹（沿用第 1 节围栏，默认配置无缓冲/dwell；`dev-002` 在 12:03 离开、12:04 才返回，把共同在场切成 `12:01–12:03` 与 `12:04–12:05` 两个候选）：

```bash
curl -X POST http://localhost:8000/points -H 'Content-Type: application/json' \
  -d '{"points": [
    {"device_id": "dev-001", "ts": "2026-09-30T11:59:00Z", "lat": 39.910, "lon": 116.310},
    {"device_id": "dev-001", "ts": "2026-09-30T12:06:00Z", "lat": 39.894, "lon": 116.294},
    {"device_id": "dev-002", "ts": "2026-09-30T11:59:30Z", "lat": 39.910, "lon": 116.310},
    {"device_id": "dev-002", "ts": "2026-09-30T12:03:00Z", "lat": 39.894, "lon": 116.294},
    {"device_id": "dev-002", "ts": "2026-09-30T12:04:00Z", "lat": 39.910, "lon": 116.310},
    {"device_id": "dev-002", "ts": "2026-09-30T12:06:30Z", "lat": 39.894, "lon": 116.294},
    {"device_id": "dev-003", "ts": "2026-09-30T12:01:00Z", "lat": 39.910, "lon": 116.310},
    {"device_id": "dev-003", "ts": "2026-09-30T12:05:00Z", "lat": 39.894, "lon": 116.294}
  ]}'

curl http://localhost:8000/fences/1/rendezvous-tasks/1/report
```

返回示例（`dev-002` 中途短暂离开一次，形成两个候选片段，仅达标的第一个保留）：

```json
{
  "id": 1,
  "fence_id": 1,
  "name": "班前集合点名",
  "members": ["dev-001", "dev-002", "dev-003"],
  "start_at": "2026-09-30T12:00:00Z",
  "end_at": "2026-09-30T12:10:00Z",
  "min_co_presence_s": 120.0,
  "created_at": "2026-09-30T11:50:00Z",
  "passed": true,
  "segments": [
    {
      "start": "2026-09-30T12:01:00Z",
      "end": "2026-09-30T12:03:00Z",
      "duration_s": 120.0,
      "members": ["dev-001", "dev-002", "dev-003"]
    }
  ]
}
```

**核验语义**：

1. **半开在场区间还原**：报告由该围栏当前的 `enter`/`leave` 事件重放，各设备在场时间一律为半开区间 **`[enter, leave)`**，并截断到有效窗口 `[start_at, end_at)`；窗口前已进入、窗口结束仍未离开的区间正确截断。
2. **同刻 enter/leave 按半开边界处理**：扫描线在每个时刻先一次性应用该时刻的全部进出（离开先、进入后），再判定下一微小区间——**恰在 `t` 离开的成员不计入 `[t, next)`，恰在 `t` 进入的成员计入**；同一成员同刻离开又进入时，旧区间止端不含 `t`、新区间起端含 `t`，共同在场视为连续不断点。窗口端点沿用同一边界：`start_at` 处的 enter 计入，`end_at` 处的 leave 不算提前离场（片段可取到窗口结束）。
3. **全体成员 + 反复进出**：片段必须覆盖任务的**全部**成员（任一成员无轨迹或窗口内未到场即无片段）；成员反复进出会形成**多个候选片段**，各自独立计时。
4. **达标判定**：只有窗口内持续时长 **`>= min_co_presence_s`（含等于）** 的片段才保留，按起止时刻升序返回，每项给出 `start`、`end`、`duration_s`（秒）与 `members`（全体成员，按 ID 排序）；`passed` 仅当存在至少一个达标片段，无达标片段时为 `false` 且 `segments` 为空数组。
5. **实时重建、无快照**：报告**每次查询都从当前事件重建**，不落库任何结果。补录乱序轨迹点（包括更早的点）触发事件重算后，再次查询即按最新事件重算片段，旧片段不残留。
6. 围栏/任务不存在或任务不属于该围栏返回 `404`。

### 13. 设备接力交班核验

为围栏创建接力交班任务：**2～8 台**按交班顺序排列的设备（设备 ID **不得重复、不得为空**）、半开有效窗口 `[start_at, end_at)`、相邻交班两段区间**分离时允许的最大空档** `max_gap_s`（秒，非负）与两段**相交时要求的最短交叠** `min_overlap_s`（秒，非负；`0` 表示只要两段真正相交即算达标）。

```bash
curl -X POST http://localhost:8000/fences/1/handover-tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "昼夜班接力",
    "members": ["dev-A", "dev-B", "dev-C"],
    "start_at": "2026-10-02T08:00:00Z",
    "end_at": "2026-10-02T09:00:00Z",
    "max_gap_s": 60,
    "min_overlap_s": 30
  }'
```

- 创建校验：设备数量必须为 **2～8**；**设备 ID 不得重复、不得为空**（否则 `422`）；窗口必须有效（`start_at < end_at`，否则 `422`）；`max_gap_s` 与 `min_overlap_s` 均必须非负（否则 `422`）；围栏不存在返回 `404`。
- 管理接口：`GET /fences/1/handover-tasks`（按 id 升序，设备按交班顺序）、`DELETE /fences/1/handover-tasks/{task_id}`；**删除围栏时其交班任务与交班设备一并删除**。

三台设备按序交班（沿用第 1 节围栏，默认配置无缓冲/dwell；`dev-A` 窗口前已在场，`dev-C` 交班后未离开）：

```bash
curl -X POST http://localhost:8000/points -H 'Content-Type: application/json' \
  -d '{"points": [
    {"device_id": "dev-A", "ts": "2026-10-02T07:55:00Z", "lat": 39.910, "lon": 116.310},
    {"device_id": "dev-A", "ts": "2026-10-02T08:10:00Z", "lat": 39.894, "lon": 116.294},
    {"device_id": "dev-B", "ts": "2026-10-02T08:09:00Z", "lat": 39.910, "lon": 116.310},
    {"device_id": "dev-B", "ts": "2026-10-02T08:31:00Z", "lat": 39.894, "lon": 116.294},
    {"device_id": "dev-C", "ts": "2026-10-02T08:30:00Z", "lat": 39.910, "lon": 116.310}
  ]}'

curl http://localhost:8000/fences/1/handover-tasks/1/report
```

返回示例（A→B 交叠 60s、B→C 交叠 60s，均 ≥ 30s，两步全部通过）：

```json
{
  "id": 1,
  "fence_id": 1,
  "name": "昼夜班接力",
  "members": ["dev-A", "dev-B", "dev-C"],
  "start_at": "2026-10-02T08:00:00Z",
  "end_at": "2026-10-02T09:00:00Z",
  "max_gap_s": 60.0,
  "min_overlap_s": 30.0,
  "created_at": "2026-10-02T07:50:00Z",
  "passed": true,
  "passed_steps": 2,
  "first_violation_at": null,
  "steps": [
    {"position": 0, "device_id": "dev-A",
     "interval_start": "2026-10-02T08:00:00Z", "interval_end": "2026-10-02T08:10:00Z",
     "status": "passed", "takeover_at": "2026-10-02T08:00:00Z", "violation_at": null},
    {"position": 1, "device_id": "dev-B",
     "interval_start": "2026-10-02T08:09:00Z", "interval_end": "2026-10-02T08:31:00Z",
     "status": "passed", "takeover_at": "2026-10-02T08:09:00Z", "violation_at": null},
    {"position": 2, "device_id": "dev-C",
     "interval_start": "2026-10-02T08:30:00Z", "interval_end": "2026-10-02T09:00:00Z",
     "status": "passed", "takeover_at": "2026-10-02T08:30:00Z", "violation_at": null}
  ]
}
```

**核验语义**：

1. **半开在场区间还原**：报告由该围栏当前的 `enter`/`leave` 事件重放，各设备在场时间一律为半开区间 **`[enter, leave)`**，并截断到有效窗口 `[start_at, end_at)`；窗口前已进入、窗口结束仍未离开的区间正确截断（`dev-A` 起端截到 `08:00`、`dev-C` 止端截到 `09:00`）。同一设备同刻离开又进入时按离开先配对（旧区间止端不含该刻、新区间起端含该刻），与协同到场的半开边界一致。
2. **交班约束**：对交班设备区间 `I` 与接班设备区间 `J`（接班起端不得早于交班设备的接班时刻，即多次进出严格**按时间推进**）：
   - 两段**相交**（`J` 起端 < `I` 止端）：交叠时长 `min(I.end, J.end) - J.start` **必须 ≥ `min_overlap_s`**（含等于）；
   - 两段**分离**（`J` 起端 ≥ `I` 止端，含首尾相接空档 0）：空档 `J.start - I.end` **必须 ≤ `max_gap_s`**（含等于）。
3. **最长链 + 字典序并列**：一台设备多次进出会产生多个候选区间。以每个区间为节点、相邻序号间满足上述约束为边，选择**通过的相邻交班步数最多**的连续交班链；步数并列时，按各步**接班时刻**（接班区间起端组成的元组）**字典序最早**取链；首步仍并列时取最早到岗区间。多个前缀汇聚到同一后续区间时，因后续能否延伸只取决于该区间本身，汇聚节点始终保留**完整的全局最早接班序列**（不会按较晚接班元组丢掉仍可延伸的更早链）。
4. **失败与停止**：链无法继续延伸的第一个接班序号标记失败，**其后所有序号为 `not_reached`，不再交班**：
   - `absent`：窗口内无满足时间推进的在场区间（首台设备窗口内从无在场区间也算缺席）；
   - `gap_exceeded`：最早候选区间可接但与上一段分离且空档超限，首个违规时刻为**上一段止端 + `max_gap_s`**（空档超限时刻）；
   - `overlap_insufficient`：最早候选区间可接但交叠不足，首个违规时刻为**交叠结束时刻** `min(I.end, J.end)`；
   - `absent` 的违规时刻取窗口结束（首台缺席取窗口开始）。
5. **结果字段**：`passed_steps` 为通过的相邻交班步数（链上设备数 − 1）；`passed` 仅当全部序号均为 `passed`；`first_violation_at` 为链上首个违规时刻（全部通过为 `null`）；`steps` 按交班顺序给出每台设备实际采用的 `interval_start`/`interval_end`、`status`、`takeover_at`（接班时刻）与 `violation_at`。
6. **实时重建、无快照**：报告**每次查询都从当前事件重建**，不落库任何结果。补录乱序轨迹点（包括更早的点）触发事件重算后，再次查询即按最新事件重算交班链，旧链与旧违规不残留。
7. 围栏/任务不存在或任务不属于该围栏返回 `404`。

### 14. 隔离冲突核验

创建隔离核验任务：**2～8 个互斥围栏组**，每组含 **2～4 个已存在且不重复**的围栏并设定半开有效窗口 `[start_at, end_at)`。各组独立判定（同一围栏可以出现在不同组中），同一设备在组内窗口中同时处于两个及以上围栏即为冲突。

```bash
curl -X POST http://localhost:8000/isolation-tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "分区互斥巡检",
    "groups": [
      {"fence_ids": [1, 2], "start_at": "2026-10-04T08:00:00Z", "end_at": "2026-10-04T09:00:00Z"},
      {"fence_ids": [3, 4, 5], "start_at": "2026-10-04T08:30:00Z", "end_at": "2026-10-04T09:30:00Z"}
    ]
  }'
```

- 创建校验：围栏组数量必须为 **2～8**；每组围栏数量必须为 **2～4**；组内围栏必须**全部存在**且**不得重复**（否则 `422`）；每组窗口必须有效（`start_at < end_at`，否则 `422`）。围栏按 ID 排序后保存返回，围栏组按提交顺序排列。
- 管理接口：`GET /isolation-tasks`（按 id 升序）、`GET /isolation-tasks/{id}`、`DELETE /isolation-tasks/{id}`；**删除任一组内围栏时，引用它的隔离任务（连同其围栏组与组-围栏关联）一并删除**。

设围栏 1 为 `[116.30,39.90]–[116.308,39.908]`、围栏 2 为 `[116.304,39.904]–[116.312,39.912]`（两者在 `[116.304–116.308, 39.904–39.908]` 区域重叠），可用如下请求创建：

```bash
curl -X POST http://localhost:8000/fences -H 'Content-Type: application/json' \
  -d '{"name":"分区A","geometry":{"type":"Polygon","coordinates":[
    [[116.30,39.90],[116.308,39.90],[116.308,39.908],[116.30,39.908],[116.30,39.90]]]}}'
curl -X POST http://localhost:8000/fences -H 'Content-Type: application/json' \
  -d '{"name":"分区B","geometry":{"type":"Polygon","coordinates":[
    [[116.304,39.904],[116.312,39.904],[116.312,39.912],[116.304,39.912],[116.304,39.904]]]}}'
```

提交一台设备先进入围栏 1、再进入两围栏重叠区、随后只停留在围栏 2、最后离开的轨迹后查询报告（其余围栏 3/4/5 任意创建即可，本例设备不会进入）：

```bash
curl -X POST http://localhost:8000/points -H 'Content-Type: application/json' \
  -d '{"points": [
    {"device_id": "dev-001", "ts": "2026-10-04T08:00:10Z", "lat": 39.901, "lon": 116.301},
    {"device_id": "dev-001", "ts": "2026-10-04T08:00:30Z", "lat": 39.905, "lon": 116.305},
    {"device_id": "dev-001", "ts": "2026-10-04T08:01:00Z", "lat": 39.909, "lon": 116.309},
    {"device_id": "dev-001", "ts": "2026-10-04T08:02:00Z", "lat": 39.894, "lon": 116.294}
  ]}'

curl http://localhost:8000/isolation-tasks/1/report
```

返回示例（设备在 `08:00:30` 同时处于围栏 1、2，`08:01:00` 离开围栏 1，冲突片段 `[08:00:30, 08:01:00)`）：

```json
{
  "id": 1,
  "name": "分区互斥巡检",
  "groups": [
    {"position": 0, "fence_ids": [1, 2],
     "start_at": "2026-10-04T08:00:00Z", "end_at": "2026-10-04T09:00:00Z"},
    {"position": 1, "fence_ids": [3, 4, 5],
     "start_at": "2026-10-04T08:30:00Z", "end_at": "2026-10-04T09:30:00Z"}
  ],
  "created_at": "2026-10-04T07:50:00Z",
  "passed": false,
  "devices": [
    {
      "device_id": "dev-001",
      "first_conflict_at": "2026-10-04T08:00:30Z",
      "groups": [
        {
          "position": 0,
          "start_at": "2026-10-04T08:00:00Z",
          "end_at": "2026-10-04T09:00:00Z",
          "first_conflict_at": "2026-10-04T08:00:30Z",
          "segments": [
            {
              "start": "2026-10-04T08:00:30Z",
              "end": "2026-10-04T08:01:00Z",
              "duration_s": 30.0,
              "active_fence_ids": [1, 2]
            }
          ]
        }
      ]
    }
  ]
}
```

**核验语义**：

1. **半开在场区间还原**：报告合并该任务各组围栏当前的 `enter`/`leave` 事件，按 **(设备, 围栏)** 重放半开区间 `[enter, leave)`，并分别截断到每个围栏组各自的窗口 `[start_at, end_at)`；窗口前已进入、窗口结束仍未离开的区间正确截断。
2. **同刻 leave 先于 enter**：扫描线在每个时刻先应用离开、再应用进入——恰在 `t` 离开的围栏不计入 `[t, next)`，恰在 `t` 进入的围栏计入。同一设备对同一围栏同刻离开又进入时旧区间止端不含 `t`、新区间起端含 `t`，冲突视为连续不断点；窗口端点沿用同一套半开边界（`start_at` 处的 enter 计入，`end_at` 处的 leave 不算提前脱离冲突，片段可取到窗口结束）。
3. **各组独立判定**：同一设备在组内活跃围栏数 **≥ 2** 即为冲突；不同组之间互不影响，一个组冲突不改变另一个组的判定。
4. **活跃集合变化即切分**：组内连续冲突时段按活跃围栏集合切分——集合变化（进入第三个围栏、退出其中一个、离开后又进入另一个等）就在边界产生新片段；片段给出 `start`、`end`、`duration_s` 与冲突时**按 ID 排序的活跃围栏** `active_fence_ids`（至少 2 个），并按起止时刻升序返回。
5. **结果与排序**：仅返回发生过冲突的设备（**设备 ID 稳定升序**），每台设备下列出有冲突的围栏组（**按组序号 position 升序**），组级与设备级（取各组最早）分别给出 `first_conflict_at`。`passed` 仅当全部设备在所有组内都未同时处于两个及以上围栏；无冲突时 `devices` 为空数组。
6. **实时重建、无快照**：报告**每次查询都从当前事件重建**，不落库任何结果。设备离开后又进入、或补录乱序点改变事件后，再次查询即按最新事件重算片段，旧片段不残留。
7. 任务不存在返回 `404`。

### 15. 已入库轨迹点批量坐标更正

对已入库的轨迹点按 **设备 ID + 原时间戳** 精确定位，提交**新经纬度**完成更正。更正不是重新摄入：原有 `POST /points` 对重复点仍按原规则忽略，重提交同时间戳的点不会触发更正。

**更正前**：先正常摄入一批轨迹点（设备 `dev-001` 在 08:00:00 与 08:00:20 都在围栏外，无 enter 事件）。

```bash
curl -X POST http://localhost:8000/points \
  -H 'Content-Type: application/json' \
  -d '{"points": [
    {"device_id": "dev-001", "ts": "2026-10-05T08:00:00Z", "lat": 39.894, "lon": 116.294},
    {"device_id": "dev-001", "ts": "2026-10-05T08:00:20Z", "lat": 39.894, "lon": 116.294}
  ]}'
# {"accepted": 2, "duplicates": 0, "devices_updated": 1}

curl 'http://localhost:8000/events?device_id=dev-001'
# []  更正前没有任何围栏事件
```

**更正**：把 08:00:20 的点更正到围栏内（沿用上文围栏，内部点取 `39.910,116.310`）。

```bash
curl -X POST http://localhost:8000/points/corrections \
  -H 'Content-Type: application/json' \
  -d '{"corrections": [
    {"device_id": "dev-001", "ts": "2026-10-05T08:00:20Z", "lat": 39.910, "lon": 116.310}
  ]}'
```

返回 `200`：

```json
{"corrected": 1, "devices_updated": 1}
```

**更正后**：按受影响设备重放完整轨迹，围栏事件、当前状态与门线穿越记录统一刷新，容量、清场、巡检、协同、交班和隔离报告再次查询即反映新结果。

```bash
curl 'http://localhost:8000/events?device_id=dev-001'
# 更正后该设备在 08:00:20 进入围栏
# [{"id": ..., "device_id": "dev-001", "fence_id": 1, "type": "enter",
#   "ts": "2026-10-05T08:00:20Z"}]

curl http://localhost:8000/devices/dev-001/status
# 当前状态同步变为 inside，since=2026-10-05T08:00:20Z
```

**整批校验与失败语义**（任一不满足则整批拒绝、HTTP `422`，原数据保持原样）：

1. **缺失记录**：任一条 `(device_id, ts)` 在库中不存在即拒绝，响应 `detail.missing` 列出全部缺失目标；
2. **同批重复目标**：同一批次内出现两条相同 `(device_id, ts)` 即拒绝；
3. **非法坐标**：纬度超出 `[-90, 90]`、经度超出 `[-180, 180]` 或为 `NaN`/`Infinity` 即拒绝。

**一致性保证**：

- 更正与轨迹点摄入共用同一把串行锁，按同一顺序串行执行，互不穿插；
- 整批坐标更新与受影响设备的事件/状态/门线穿越重放在**同一事务内一次提交**；重算失败整体回滚，**不会留下新坐标配旧事件**的中间态；
- 只重放本批受影响设备的完整轨迹，**其他设备的记录不变**；各类核验报告都由事件/穿越实时重建，更正后再次查询即为新结果；
- 一次最多 10000 条更正，批次不能为空。

### 16. 调整已有围栏（边界与去抖参数）

对已创建的围栏发起 `PUT /fences/{id}`，**整体替换**名称、GeoJSON Polygon 与完整判定参数（请求体与 `POST /fences` 相同）。新边界与去抖规则立即对**全部存量轨迹**重放生效：该围栏所有设备的 `enter`/`leave`/`stay` 事件、当前状态与门线穿越统一重算。

**成功示例**：把第 1 节的围栏从 `[116.30,39.90]–[116.32,39.92]` 向东扩展到 `116.33`，同时把离开缓冲调整为 100m：

```bash
curl -X PUT http://localhost:8000/fences/1 \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "园区A（东区扩建）",
    "geometry": {
      "type": "Polygon",
      "coordinates": [
        [[116.30, 39.90], [116.33, 39.90], [116.33, 39.92], [116.30, 39.92], [116.30, 39.90]],
        [[116.308, 39.908], [116.312, 39.908], [116.312, 39.912], [116.308, 39.912], [116.308, 39.908]]
      ]
    },
    "config": {
      "enter_buffer_m": 0,
      "exit_buffer_m": 100,
      "enter_dwell_s": 10,
      "exit_dwell_s": 10,
      "stay_min_s": 600
    }
  }'
```

返回 `200` 及更新后的围栏对象（`id` 不变）。此后：

- 再次查询 `/events`、`/devices/{id}/status` 即按新边界与新去抖参数给出结果；
- **原门线与巡检路线原样保留、继续可用**（巡检报告由按新投影重算后的穿越记录重放）；
- 容量、清场、协同、交班及隔离报告均实时由事件重建，再次查询即反映新事件；
- 其他围栏的事件与报告结果不变。

**冲突示例**：围栏 1 现有门线 `2`、`5`，其中门线 2 越出了缩小后的新外环、门线 5 穿过了新增孔洞：

```bash
curl -X PUT http://localhost:8000/fences/1 \
  -H 'Content-Type: application/json' \
  -d '{"name": "园区A", "geometry": {"type": "Polygon", "coordinates": [
    [[116.30, 39.90], [116.31, 39.90], [116.31, 39.91], [116.30, 39.91], [116.30, 39.90]]]},
    "config": {}}'
```

返回 `422`：

```json
{
  "detail": {
    "message": "现有门线越出新围栏有效区域或穿过新孔洞，调整已拒绝",
    "conflicting_gate_ids": [2, 5]
  }
}
```

**校验与一致性语义**：

1. **沿用创建校验**：几何必须是非空、有效的 GeoJSON Polygon（自相交、孔洞越出外环等返回 `422`）；参数约束同创建（非负、`enter_buffer_m <= exit_buffer_m` 等，字段校验失败同样 `422`）。
2. **围栏不存在**返回 `404`。
3. **门线冲突预检**：调整前按与创建门线相同的几何要求（多边形 `covers` + 不与任何孔洞边界相交）逐条校验该围栏的**现有门线**；只要有门线越出新有效区域（含落在边界外）或穿过/触碰新孔洞，即返回 `422` 并在 `detail.conflicting_gate_ids` 中按 ID 升序列出全部冲突门线。**原围栏定义、门线、事件、状态、穿越及一切衍生记录均不改变。**
4. **串行与原子提交**：调整与轨迹摄入、坐标更正共用同一把进程内串行锁，互不穿插；围栏定义更新与所有存量设备的事件/当前状态/门线穿越重算在**同一事务内一次提交**，任一步重算失败整体回滚，不会留下新边界配旧事件的中间态。其他围栏的结果保持不变。
5. **衍生记录保留**：容量规则、清场任务、协同任务、交班任务、巡检路线与隔离任务等定义均原样保留；其报告本就由事件/穿越实时重建，调整后再次查询即为新结果。注意调整不改变容量规则本身——若新边界导致占用变化，容量超限分段会随之改变；如旧规则不再适用请另行增删规则。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/fences` | 创建围栏（GeoJSON Polygon，支持孔洞，附判定配置） |
| PUT | `/fences/{id}` | 调整围栏（整体替换几何与判定参数；存量轨迹重放，门线冲突 422 列出冲突 ID，原子提交） |
| GET | `/fences` / `/fences/{id}` | 围栏列表 / 详情 |
| DELETE | `/fences/{id}` | 删除围栏及其事件、状态、容量规则、门线、清场任务、协同任务、交班任务、引用它的隔离任务 |
| POST | `/points` | 批量提交轨迹点（乱序重排、重复幂等） |
| POST | `/points/corrections` | 批量坐标更正（按设备 ID + 原时间戳定位；缺失/同批重复/非法坐标整批 422 拒绝，原子重放） |
| GET | `/events` | 事件查询（device_id / fence_id / type / start / end / limit / offset） |
| GET | `/devices/{device_id}/status` | 设备当前相对各围栏的状态 |
| POST | `/fences/{id}/capacity-rules` | 新增容量规则（生效时刻 + 正整数上限，同时刻唯一） |
| GET | `/fences/{id}/capacity-rules` | 容量规则列表（按生效时刻升序） |
| DELETE | `/fences/{id}/capacity-rules/{rule_id}` | 删除容量规则 |
| GET | `/fences/{id}/occupancy` | 查询 `[start, end)` 占用分段与超限明细（必填 start/end） |
| POST | `/fences/{id}/gates` | 新增定向门线（围栏内简单 LineString + 正/反向名称 + 最小位移） |
| GET | `/fences/{id}/gates` | 门线列表（按 id 升序） |
| DELETE | `/fences/{id}/gates/{gate_id}` | 删除门线及其穿越记录（被巡检路线引用时 409 拒绝） |
| GET | `/crossings` | 门线穿越查询（device_id / gate_id / fence_id / direction / start / end，半开窗口） |
| POST | `/fences/{id}/inspection-routes` | 新增有序门线巡检路线（2～8 个检查点：门线 + 方向 + 与上一点最长间隔） |
| GET | `/fences/{id}/inspection-routes` | 巡检路线列表（按 id 升序，检查点按顺序） |
| DELETE | `/fences/{id}/inspection-routes/{route_id}` | 删除巡检路线及其检查点（删除围栏时一并删除） |
| GET | `/fences/{id}/inspection-routes/{route_id}/report` | 巡检核验报告（按设备 + `[start,end)` 由当前穿越记录重放：通过/超时/顺序违规/未到达 + 首个违规时刻） |
| POST | `/fences/{id}/clearance-tasks` | 新增应急清场任务（名称 + 开始/截止时刻，开始须早于截止） |
| GET | `/fences/{id}/clearance-tasks` | 清场任务列表（按 id 升序） |
| DELETE | `/fences/{id}/clearance-tasks/{task_id}` | 删除清场任务（删除围栏时一并删除） |
| GET | `/fences/{id}/clearance-tasks/{task_id}/report` | 清场核验报告（实时由事件重建：起始名单 + cleared/remaining + 汇总） |
| POST | `/fences/{id}/rendezvous-tasks` | 新增协同到场任务（2～8 台不重复设备 + 有效窗口 + 最短共同在场时长） |
| GET | `/fences/{id}/rendezvous-tasks` | 协同任务列表（按 id 升序，成员按提交顺序） |
| DELETE | `/fences/{id}/rendezvous-tasks/{task_id}` | 删除协同任务及其成员（删除围栏时一并删除） |
| GET | `/fences/{id}/rendezvous-tasks/{task_id}/report` | 协同到场报告（实时由事件重建：全员共同在场达标片段起止/秒数/成员 + passed） |
| POST | `/fences/{id}/handover-tasks` | 新建设备接力交班任务（2～8 台按序不重复设备 + 有效窗口 + 最大空档 + 最短交叠） |
| GET | `/fences/{id}/handover-tasks` | 交班任务列表（按 id 升序，设备按交班顺序） |
| DELETE | `/fences/{id}/handover-tasks/{task_id}` | 删除交班任务及其交班设备（删除围栏时一并删除） |
| GET | `/fences/{id}/handover-tasks/{task_id}/report` | 接力交班报告（实时由事件重建：最长连续交班链 + 每步实际区间/状态/接班时刻/首个违规时刻） |
| POST | `/isolation-tasks` | 新增隔离冲突核验任务（2～8 个互斥围栏组，每组 2～4 个已存在且不重复的围栏 + 各自有效窗口） |
| GET | `/isolation-tasks` / `/isolation-tasks/{id}` | 隔离任务列表（按 id 升序）/ 详情 |
| DELETE | `/isolation-tasks/{id}` | 删除隔离任务及其围栏组（删除任一组内围栏时一并删除） |
| GET | `/isolation-tasks/{id}/report` | 隔离冲突报告（实时由事件重建：按设备/围栏组的连续冲突片段 + 活跃围栏 + 首个冲突时刻 + passed） |
| GET | `/health` | 健康检查 |

## 围栏配置参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `enter_buffer_m` | 0 | 进入缓冲距离（米），距多边形 ≤ 该值即视为内部 |
| `exit_buffer_m` | 0 | 离开缓冲距离（米），距多边形 > 该值才视为外部；须 ≥ `enter_buffer_m` |
| `enter_dwell_s` | 0 | 进入确认所需持续时长（秒） |
| `exit_dwell_s` | 0 | 离开确认所需持续时长（秒） |
| `stay_min_s` | 300 | 产生 `stay` 事件的最小连续停留时长（秒） |

## 本地开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

pytest                      # 运行测试
uvicorn app.main:app --reload --port 8000   # 本地启动
```

数据库位置由环境变量 `DATABASE_URL` 控制，默认 `sqlite:///./geofence.db`。

## 项目结构

```
app/
  main.py      # FastAPI 路由与摄入/重算流程、已入库轨迹点批量坐标更正、围栏边界与参数调整
  engine.py    # 投影、缓冲迟滞 + dwell 状态机（事件判定核心）、容量占用分段、门线穿越核算、应急清场核验、有序门线巡检路线核验、协同到场核验、设备接力交班核验、隔离冲突核验
  models.py    # SQLAlchemy 模型（fences / points / events / device_state / capacity_rules / gate_lines / crossings / clearance_tasks / inspection_routes / inspection_checkpoints / rendezvous_tasks / rendezvous_members / handover_tasks / handover_members / isolation_tasks / isolation_groups / isolation_group_fences）
  schemas.py   # Pydantic 请求/响应模型
  database.py  # SQLite 连接与会话
tests/         # pytest 端到端测试（孔洞、乱序、幂等、抖动、停留、查询、容量核算、门线穿越、清场核验、巡检路线核验、协同到场核验、接力交班核验、隔离冲突核验、批量坐标更正、围栏调整）
Dockerfile / docker-compose.yml
```

## 说明与取舍

- 事件按设备全量轨迹重算并整体替换，保证乱序/重复提交下结果一致；事件 `id` 可能随重算变化，请以 `(device_id, fence_id, type, ts)` 识别事件。门线穿越同理整体重算，请以 `(device_id, gate_id, direction, ts)` 识别。
- 同一设备同一时间戳的轨迹点视为重复（保留首条）；需要修改已入库点的坐标时使用 `POST /points/corrections`，更正按整批原子提交并重放受影响设备的完整轨迹，`/points` 的重复忽略规则不受影响。
- 距离计算采用以围栏质心为原点的局部等距圆柱投影近似，围栏尺度（公里级）下误差可忽略。
- 门线方向以**坐标书写顺序**为准（右→左为正向）；同一扇门反向画一遍会交换 forward/reverse 的语义，命名时请注意坐标顺序。
