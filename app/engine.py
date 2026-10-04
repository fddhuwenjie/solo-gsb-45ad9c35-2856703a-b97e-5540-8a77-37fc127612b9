"""围栏事件判定引擎。

将经纬度投影到以围栏质心为原点的局部平面坐标系（等距圆柱近似），
在米制坐标下用 Shapely 做距离计算，配合进入/离开缓冲距离形成迟滞区间，
再叠加停留时长（dwell）做抖动去抖，产出 enter / leave / stay 事件。

另含容量核算：由 enter/leave 事件还原设备半开在场区间，结合容量规则
把查询窗口切分为状态恒定的占用分段并标注超限明细。

应急清场核验：为围栏定义清场窗口 [start, deadline)，由 enter/leave 事件
还原起始时刻的在场名单（半开边界：start 处的 enter 计入、leave 排除），
仅核对这份名单；设备离开后又进入必须再次离开并持续在外至 deadline 才算
按时清场。

门线穿越核算：在围栏有效区域内定义定向 GeoJSON LineString 门线，按设备
时间重排轨迹，只承认线段对门线的单点横穿（proper crossing），穿越时刻按
交点在线段上的时间比例插值。门线含三个及以上折点时，在整条门线层面归并
共享内部折点：轨迹恰好穿过折点且折点两侧邻接段分居轨迹两侧时按一次横穿
核算，邻接段同侧时按擦碰忽略。

有序门线巡检路线核验：路线是 2～8 个按序排列的门线检查点（门线 + 要求
方向 + 与上一点的最长间隔，首点无间隔）。在给定设备与 [start, end) 窗口
上按当前穿越记录时间重放：只有匹配下一个检查点的穿越才推进；当前门线的
反向穿越或后续门线的穿越立即判顺序违规；超过间隔判超时；其他门线忽略。

协同到场核验：任务指定 2～8 台成员设备、有效窗口 [start, end) 与最短
共同在场时长。由 enter/leave 事件还原各成员的半开在场区间 [enter, leave)
并截断到窗口，在合并后的时间边界上扫描全体成员同时在场的连续片段；
成员反复进出会形成多个候选片段，只有窗口内持续时长达到（含）门槛的片段
才算通过，任一达标即整体通过，无达标片段即失败。

设备接力交班核验：任务指定 2～8 台按交班顺序排列且不重复的设备、有效
窗口 [start, end)、相邻交班允许的最大空档与最短交叠时长。由 enter/leave
事件还原每台设备截断到窗口的半开在场区间（同刻 leave+enter 离开先配对），
在“区间节点 + 相邻序号连边”的有向图上动态规划，选择通过的相邻交班步数
最多的连续交班链；多个前缀汇聚到同一节点时只保留完整的全局最早接班
序列，并列时按各步接班时刻组成的元组字典序最早。两段相交要求交叠达到
最短时长（允许 0，即只要真正相交），分离要求空档不超过最大值；无法满足
时按缺席 / 空档超限 / 交叠不足标记并停止后续交班。

隔离冲突核验：任务配置 2～8 个互斥围栏组（每组 2～4 个不重复围栏，各自
带有效窗口 [start, end)，各组独立判定，围栏可跨组复用）。由 enter/leave
事件按 (设备, 围栏) 还原半开在场区间并截断到组窗口，逐组扫描同一设备同时
处于两个及以上围栏的时段；同刻 leave 先于 enter，活跃围栏集合变化即切分，
得到带起止时刻、活跃围栏（按 ID 排序）与首个冲突时刻的连续冲突片段。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import Point, Polygon

EARTH_RADIUS_M = 6_371_000.0

EVENT_ENTER = "enter"
EVENT_LEAVE = "leave"
EVENT_STAY = "stay"

DIRECTION_FORWARD = "forward"    # 由门线右侧穿到左侧（沿门线坐标顺序看）
DIRECTION_REVERSE = "reverse"    # 由门线左侧穿到右侧

# 交点与线段端点重合判定的绝对几何容差（米制投影坐标下）。
# 用距离而非参数比例，避免折点恰好命中时浮点误差让两侧邻接段给出不一致的分类。
_CROSS_EPS_M = 1e-7


def _project(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """经纬度 -> 以 (lat0, lon0) 为原点的局部米制坐标（等距圆柱近似）。"""
    x = math.radians(lon - lon0) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return (x, y)


@dataclass
class FenceConfig:
    enter_buffer_m: float = 0.0   # 距多边形 <= 该距离即视为“进入”（提前触发）
    exit_buffer_m: float = 0.0    # 距多边形 > 该距离才视为“离开”（迟滞带）
    enter_dwell_s: float = 0.0    # 持续在内部这么久才确认 enter
    exit_dwell_s: float = 0.0     # 持续在外部这么久才确认 leave
    stay_min_s: float = 300.0     # 单次访问内连续停留达到该时长产生一次 stay


@dataclass
class TrackPoint:
    ts: float   # epoch 秒
    lat: float
    lon: float


@dataclass
class FenceEvent:
    fence_id: int
    type: str   # enter | leave | stay
    ts: float   # 事件发生的轨迹时间（非确认时间）


def _unproject(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    """局部米制坐标 -> 经纬度，_project 的逆变换。"""
    lat = lat0 + math.degrees(y / EARTH_RADIUS_M)
    lon = lon0 + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(lat0))))
    return (lat, lon)


def project_points(
    coords: list[tuple[float, float]], lat0: float, lon0: float
) -> list[tuple[float, float]]:
    """把 (lon, lat) 坐标序列投影到以 (lat0, lon0) 为原点的局部米制坐标。"""
    return [_project(lat, lon, lat0, lon0) for lon, lat in coords]


class ProjectedFence:
    """投影到局部米制坐标系的围栏多边形，支持带孔多边形。"""

    def __init__(self, fence_id: int, polygon: Polygon, config: FenceConfig):
        self.fence_id = fence_id
        self.config = config
        centroid = polygon.centroid
        self._lat0, self._lon0 = centroid.y, centroid.x
        shell = [_project(lat, lon, self._lat0, self._lon0)
                 for lon, lat in polygon.exterior.coords]
        holes = [
            [_project(lat, lon, self._lat0, self._lon0) for lon, lat in ring.coords]
            for ring in polygon.interiors
        ]
        self._poly = Polygon(shell, holes)

    @property
    def origin(self) -> tuple[float, float]:
        """投影原点 (lat0, lon0)。"""
        return (self._lat0, self._lon0)

    def classify(self, lat: float, lon: float, prev_inside: bool) -> bool:
        """判定点是否在围栏内。

        点到多边形的距离 d（内部为 0，孔洞内 > 0，因此孔洞天然视为外部）：
        - d <= enter_buffer_m  -> 内部
        - d >  exit_buffer_m   -> 外部
        - 两者之间为迟滞灰区   -> 维持上一状态，抑制边界抖动
        """
        x, y = _project(lat, lon, self._lat0, self._lon0)
        d = self._poly.distance(Point(x, y))
        if d <= self.config.enter_buffer_m:
            return True
        if d > self.config.exit_buffer_m:
            return False
        return prev_inside


def compute_events(
    fence: ProjectedFence, points: list[TrackPoint]
) -> tuple[list[FenceEvent], bool, float | None]:
    """对单个 (设备, 围栏) 按时间有序轨迹计算事件。

    points 必须已按 ts 升序排列。返回 (事件列表, 最终是否在围栏内, 当前状态起始时间)。
    状态机：
    - 分类结果与当前状态一致 -> 取消未确认的待定迁移；
    - 分类结果不同 -> 记录待定迁移起点，持续满足 dwell 时长后才确认迁移并产出事件，
      事件时间取条件首次满足的时刻；期间条件被打破则待定迁移作废（去抖）。
    - 进入围栏后连续停留达到 stay_min_s，产出一次 stay（每次访问最多一次）。
    """
    cfg = fence.config
    events: list[FenceEvent] = []
    inside = False
    state_since: float | None = None
    pending_since: float | None = None
    stay_emitted = False

    for pt in points:
        now_inside = fence.classify(pt.lat, pt.lon, inside)
        if now_inside == inside:
            pending_since = None
        else:
            if pending_since is None:
                pending_since = pt.ts
            dwell = cfg.enter_dwell_s if now_inside else cfg.exit_dwell_s
            if pt.ts - pending_since >= dwell:
                inside = now_inside
                state_since = pending_since
                events.append(FenceEvent(
                    fence.fence_id,
                    EVENT_ENTER if inside else EVENT_LEAVE,
                    pending_since,
                ))
                pending_since = None
                stay_emitted = False
        if inside and not stay_emitted and pt.ts - state_since >= cfg.stay_min_s:
            events.append(FenceEvent(fence.fence_id, EVENT_STAY, state_since + cfg.stay_min_s))
            stay_emitted = True

    return events, inside, state_since


# ---------- 容量核算 ----------


def pair_intervals(evs: list[tuple[float, str]]) -> list[tuple[float, float]]:
    """由单台设备（单个围栏）的 (ts, 类型) 事件配对出半开在场区间 [enter, leave)。

    事件按 (ts, 类型) 排序，使 leave 先于 enter，与半开语义一致——旧区间
    止端不含 t、新区间起端含 t，因此同刻离开又进入会得到首尾相接的两段，
    而不是丢掉新一段。未配对的 enter（最终仍未离开）止端取 +inf，
    交由调用方按窗口截断。
    """
    ordered = sorted(evs, key=lambda x: (x[0], 0 if x[1] == EVENT_LEAVE else 1))
    intervals: list[tuple[float, float]] = []
    open_since: float | None = None
    for ts, etype in ordered:
        if etype == EVENT_LEAVE:
            if open_since is not None:
                intervals.append((open_since, ts))
                open_since = None
        elif open_since is None:
            open_since = ts
    if open_since is not None:
        intervals.append((open_since, math.inf))
    return intervals


def presence_intervals(events: list[tuple[str, str, float]]) -> list[tuple[str, float, float]]:
    """由 enter/leave 事件重放每台设备的半开在场区间 [enter, leave)。

    输入为 (device_id, event_type, ts)。同一设备同刻的 leave/enter 按
    leave 先配对，因此同刻离开又进入的设备会得到首尾相接的两段区间，
    而不是丢掉新一段。
    """
    by_device: dict[str, list[tuple[float, str]]] = {}
    for device_id, etype, ts in events:
        if etype in (EVENT_ENTER, EVENT_LEAVE):
            by_device.setdefault(device_id, []).append((ts, etype))

    intervals: list[tuple[str, float, float]] = []  # (设备, 起, 止)
    for device_id, evs in by_device.items():
        for s, e in pair_intervals(evs):
            intervals.append((device_id, s, e))
    return intervals


@dataclass
class OccupancySegment:
    """[start, end) 内状态恒定（在场设备集合与生效上限均不变）的占用分段。"""

    start: float
    end: float
    devices: tuple[str, ...]   # 在场设备 ID，已排序
    limit: int | None          # 该分段生效的容量上限；无规则时为 None

    @property
    def count(self) -> int:
        return len(self.devices)

    @property
    def exceeded(self) -> bool:
        return self.limit is not None and self.count > self.limit

    @property
    def excess(self) -> int | None:
        return self.count - self.limit if self.exceeded else None


def compute_occupancy_segments(
    events: list[tuple[str, str, float]],
    rules: list[tuple[float, int]],
    start: float,
    end: float,
) -> list[OccupancySegment]:
    """由 enter/leave 事件与容量规则还原 [start, end) 内的占用分段。

    - 重放事件流，把每台设备的在场时间还原为半开区间 [enter, leave)；
      查询前已进入、查询结束仍未离开的区间被截断到查询窗口。
    - 边界取窗口端点、区间端点与窗口内的规则生效时刻；同一时刻的
      设备进出与规则切换在边界集合中天然合并，按一个原子边界处理。
    - 相邻状态（在场设备集合 + 生效上限）相同的分段被合并，保证分段尽可能长。
    """
    # 1) 还原每台设备的半开在场区间 [enter, leave)
    intervals = presence_intervals(events)

    # 2) 截断到查询窗口 [start, end)，丢弃窗口外的零长区间
    clipped = [
        (dev, max(s, start), min(e, end))
        for dev, s, e in intervals
        if max(s, start) < min(e, end)
    ]

    # 3) 边界集合：同一时刻的进出与规则切换合并为一个原子边界
    boundaries = {start, end}
    for _dev, cs, ce in clipped:
        boundaries.add(cs)
        boundaries.add(ce)
    ordered_rules = sorted(rules)
    for effective_from, _ in ordered_rules:
        if start < effective_from < end:
            boundaries.add(effective_from)
    ordered = sorted(boundaries)

    # 4) 逐段求恒定状态，相邻同状态分段合并
    segments: list[OccupancySegment] = []
    for a, b in zip(ordered, ordered[1:]):
        devices = tuple(sorted(dev for dev, cs, ce in clipped if cs <= a < ce))
        limit: int | None = None
        for effective_from, cap in ordered_rules:
            if effective_from > a:
                break
            limit = cap
        if segments and segments[-1].devices == devices and segments[-1].limit == limit:
            segments[-1].end = b
        else:
            segments.append(OccupancySegment(a, b, devices, limit))
    return segments


# ---------- 门线穿越核算 ----------


@dataclass
class GateLine:
    """定向门线（已投影到所属围栏的局部米制坐标系）。

    coords 为 LineString 折点序列；方向按坐标顺序判定：
    沿坐标顺序看，由右侧穿到左侧为正向（forward），反之为反向（reverse）。
    """

    gate_id: int
    coords: list[tuple[float, float]]
    forward_name: str
    reverse_name: str
    min_displacement_m: float

    def crossing(
        self, a: tuple[float, float], b: tuple[float, float]
    ) -> tuple[float, str, tuple[float, float]] | None:
        """求轨迹线段 a->b 对门线的单点横穿。

        返回 (交点在线段 a->b 上的比例 t, 方向, 交点米制坐标)；
        不构成横穿时返回 None。

        检测在整条门线层面进行，逐段分类后再归并共享顶点：
        - 严格内部横穿（交点严格位于门线段与轨迹段内部）；
        - 内部折点穿过：轨迹恰好经过门线的内部折点（折点严格位于轨迹段
          内部），其两侧邻接门线段对该折点各报一次端点命中。折点两侧邻接
          段分居轨迹两侧（方向判断一致）时归并为一次横穿；位于轨迹同侧时
          按擦碰忽略，与任一侧共线（沿线）同样忽略。
        - 首尾端点穿过、轨迹端点止于门线、沿线（共线/重合/重叠）、轨迹与
          门线产生多个交点（多交点歧义）一律返回 None。
        """
        # 分类结果：
        #   ("proper", t, 方向, 交点)  门线段严格内部横穿
        #   ("vertex", 折点下标, t)    门线段在其端点（折点）处命中轨迹内部
        hits: list[tuple] = []
        for idx, (p, q) in enumerate(zip(self.coords, self.coords[1:])):
            kind, *payload = _segment_classify(p, q, a, b)
            if kind == "collinear":
                return None  # 沿线（共线/重合/重叠）
            if kind == "proper":
                hits.append(("proper", *payload))
            elif kind == "vertex":
                # 端点 q（下标 idx+1）或 p（下标 idx）命中
                vidx = idx + 1 if payload[0] == "q" else idx
                hits.append(("vertex", vidx))

        # 归并内部折点：同一折点被两侧邻接段各报一次，在折点层面整体判定
        vertex_hits: dict[int, int] = {}
        merged: list[tuple[float, str, tuple[float, float]]] = []
        for kind, *payload in hits:
            if kind == "proper":
                merged.append(payload)  # type: ignore[arg-type]
                continue
            vidx = payload[0]  # type: ignore[misc]
            if vidx == 0 or vidx == len(self.coords) - 1:
                continue  # 仅接触门线首尾端点
            vertex_hits[vidx] = vertex_hits.get(vidx, 0) + 1

        sx, sy = b[0] - a[0], b[1] - a[1]
        for vidx, count in vertex_hits.items():
            if count != 2:
                continue  # 仅一侧邻接段命中（另一侧不相交/共线/被排除）：不算横穿
            hit = self._vertex_crossing(vidx, a, (sx, sy))
            if hit is not None:
                merged.append(hit)

        if len(merged) != 1:
            return None  # 多交点歧义，或没有横穿
        return merged[0]

    def _vertex_crossing(
        self,
        vidx: int,
        a: tuple[float, float],
        s: tuple[float, float],
    ) -> tuple[float, str, tuple[float, float]] | None:
        """判定轨迹穿过内部折点 coords[vidx] 是否构成一次横穿。

        要求折点两侧邻接门线段分居轨迹两侧，且两侧给出的方向判断一致。
        s 为轨迹位移向量；折点在轨迹段 a->b 上的参数比例由折点坐标直接求得。
        """
        v = self.coords[vidx]
        sx, sy = s
        slen = math.hypot(sx, sy)
        if slen == 0.0:
            return None
        # 折点到轨迹起点沿位移方向的参数比例（取位移主轴计算，几何容差判端点）
        if abs(sx) >= abs(sy):
            t = (v[0] - a[0]) / sx
            margin = _CROSS_EPS_M / slen
        else:
            t = (v[1] - a[1]) / sy
            margin = _CROSS_EPS_M / slen
        if not (margin < t < 1.0 - margin):
            return None  # 折点落在轨迹端点（或延长线）上

        # 两侧邻接臂（自折点指向相邻折点）到轨迹 a->b 的带符号距离：
        # cross(arm, s)/|s|，> 0 在轨迹左侧，< 0 在右侧。
        arm_prev = (self.coords[vidx - 1][0] - v[0], self.coords[vidx - 1][1] - v[1])
        arm_next = (self.coords[vidx + 1][0] - v[0], self.coords[vidx + 1][1] - v[1])
        d_prev = (arm_prev[0] * sy - arm_prev[1] * sx) / slen
        d_next = (arm_next[0] * sy - arm_next[1] * sx) / slen
        if abs(d_prev) <= _CROSS_EPS_M or abs(d_next) <= _CROSS_EPS_M:
            return None  # 邻接臂与轨迹共线：沿线
        if d_prev * d_next > 0.0:
            return None  # 同侧擦碰

        # 轨迹起点到两侧定向门线段支撑线的带符号距离（方向按坐标顺序）
        r_prev = (-arm_prev[0], -arm_prev[1])
        r_next = arm_next
        qpx, qpy = a[0] - v[0], a[1] - v[1]
        da_prev = (qpx * r_prev[1] - qpy * r_prev[0]) / math.hypot(*r_prev)
        da_next = (qpx * r_next[1] - qpy * r_next[0]) / math.hypot(*r_next)
        if abs(da_prev) <= _CROSS_EPS_M or abs(da_next) <= _CROSS_EPS_M:
            return None  # 轨迹端点落在邻接门线段（含其延长线共线）上：沿线/歧义
        if (da_prev > 0) != (da_next > 0):
            return None  # 两侧方向判断不一致：多交点歧义，防御处理

        direction = DIRECTION_FORWARD if da_prev > 0 else DIRECTION_REVERSE
        return (t, direction, v)


def _segment_classify(
    p: tuple[float, float],
    q: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> tuple:
    """分类门线段 p->q 与轨迹段 a->b 的关系。

    返回：
    - ("collinear",)                  共线且有公共点（沿线，整条门线忽略）
    - ("none",)                       不相交
    - ("proper", t, 方向, 交点)       交点严格位于两条线段内部的横穿
    - ("vertex", "p"|"q", t)          门线段端点恰好落在轨迹段内部（t 严格在 (0,1) 内）
    """
    rx, ry = q[0] - p[0], q[1] - p[1]
    sx, sy = b[0] - a[0], b[1] - a[1]
    denom = rx * sy - ry * sx
    if denom == 0.0:
        # 平行：仅共线且有公共点才算沿线；平行不相交无需处理
        qpx, qpy = a[0] - p[0], a[1] - p[1]
        if qpx * ry - qpy * rx == 0.0:
            # 共线：沿门线方向参数 u 检查两线段投影是否重叠
            r2 = rx * rx + ry * ry
            u0 = (qpx * rx + qpy * ry) / r2
            u1 = ((b[0] - p[0]) * rx + (b[1] - p[1]) * ry) / r2
            if max(min(u0, u1), 0.0) <= min(max(u0, u1), 1.0):
                return ("collinear",)
        return ("none",)
    qpx, qpy = a[0] - p[0], a[1] - p[1]
    u = (qpx * sy - qpy * sx) / denom
    t = (qpx * ry - qpy * rx) / denom
    # 用绝对几何容差放宽参数边界：交点恰在端点时浮点误差可能让参数略微越界
    rlen = math.hypot(rx, ry)
    slen = math.hypot(sx, sy)
    um = _CROSS_EPS_M / rlen
    tm = _CROSS_EPS_M / slen
    if not (-um <= u <= 1.0 + um and -tm <= t <= 1.0 + tm):
        return ("none",)  # 交点落在延长线上
    if not (tm < t < 1.0 - tm):
        return ("none",)  # 轨迹端点止于门线：端点接触，整条门线不据此横穿

    # 交点离门线段哪个端点更近；近于容差即按折点命中处理，
    # 避免折点恰好命中时被两侧邻接段分别报成“端点”与“严格内部”
    ix, iy = a[0] + t * sx, a[1] + t * sy
    dp = math.hypot(ix - p[0], iy - p[1])
    dq = math.hypot(ix - q[0], iy - q[1])
    if dp <= _CROSS_EPS_M:
        return ("vertex", "p", t)
    if dq <= _CROSS_EPS_M:
        return ("vertex", "q", t)

    # 严格内部交点；两端须分居门线两侧（正常几何横穿必满足，此处防御）
    side_a = qpx * ry - qpy * rx
    side_b = (qpx + sx) * ry - (qpy + sy) * rx
    if side_a * side_b >= 0.0:
        return ("none",)  # 同侧擦碰或端点恰在门线上

    # 沿门线坐标顺序看：右 -> 左为正向，左 -> 右为反向
    direction = DIRECTION_FORWARD if side_a > 0 else DIRECTION_REVERSE
    point = (a[0] + t * sx, a[1] + t * sy)
    return ("proper", t, direction, point)


@dataclass
class GateCrossing:
    gate_id: int
    direction: str   # forward | reverse
    ts: float        # 按交点在轨迹线段上的比例插值得到的穿越时刻
    lat: float
    lon: float


def compute_crossings(
    gates: list[GateLine],
    points: list[TrackPoint],
    lat0: float,
    lon0: float,
) -> list[GateCrossing]:
    """对单个设备在某围栏内的门线集合计算穿越记录。

    points 必须已按 ts 升序排列。对每对相邻点形成的轨迹线段逐一检测：
    - 与门线单点横穿、两端分居两侧才承认（GateLine.crossing 内判定，
      含内部折点穿过的整线归并）；
    - 相邻点位移不足门线 min_displacement_m 时忽略；
    - 穿越时刻按交点在线段上的比例在两端点时间戳间线性插值。
    同一轨迹段命中多条门线时，结果按 (插值时刻, gate_id) 排序。
    """
    crossings: list[GateCrossing] = []
    for prev, cur in zip(points, points[1:]):
        ax, ay = _project(prev.lat, prev.lon, lat0, lon0)
        bx, by = _project(cur.lat, cur.lon, lat0, lon0)
        dx, dy = bx - ax, by - ay
        segment_len = math.hypot(dx, dy)
        dt = cur.ts - prev.ts
        a, b = (ax, ay), (bx, by)
        for gate in gates:
            if segment_len < gate.min_displacement_m:
                continue  # 位移不足
            hit = gate.crossing(a, b)
            if hit is None:
                continue
            t, direction, (ix, iy) = hit
            ilat, ilon = _unproject(ix, iy, lat0, lon0)
            crossings.append(GateCrossing(
                gate_id=gate.gate_id,
                direction=direction,
                ts=prev.ts + t * dt,
                lat=ilat,
                lon=ilon,
            ))
    crossings.sort(key=lambda c: (c.ts, c.gate_id))
    return crossings


# ---------- 应急清场核验 ----------


CLEARED = "cleared"
REMAINING = "remaining"


@dataclass
class ClearanceDeviceResult:
    """单台起始名单设备的清场核验结果。"""

    device_id: str
    status: str               # cleared | remaining
    last_leave_ts: float | None  # cleared 时为最后有效离开时刻；remaining 为 None


@dataclass
class ClearanceReport:
    """清场核验报告：仅覆盖起始名单设备，按设备 ID 排序。"""

    start: float
    deadline: float
    results: list[ClearanceDeviceResult]

    @property
    def start_count(self) -> int:
        return len(self.results)

    @property
    def cleared_count(self) -> int:
        return sum(1 for r in self.results if r.status == CLEARED)

    @property
    def remaining_count(self) -> int:
        return sum(1 for r in self.results if r.status == REMAINING)


def compute_clearance(
    events: list[tuple[str, str, float]], start: float, deadline: float
) -> ClearanceReport:
    """由 enter/leave 事件还原 [start, deadline) 的应急清场核验报告。

    在场区间一律按半开区间 [enter, leave) 处理：
    - 起始名单 = 起始时刻在场的设备，即存在区间满足 enter <= start < leave。
      start 处的 enter 计入（区间含 enter），start 处的 leave 排除
      （区间不含 leave）；截止时刻沿用同一套半开边界。
    - 只核对这份起始名单，窗口内才首次进入的设备不参与核验。
    - cleared：设备在窗口内离开后持续在外至 deadline（存在 leave <= deadline
      且之后没有任何区间在 deadline 仍开着）。离开后又进入的设备必须再次
      离开并持续在外至 deadline，否则仍为 remaining。
    - last_leave_ts 取截止前最后一次有效离开时刻（开着区间的最后一个止端）；
      仍在场的设备该值为 None。
    """
    by_device: dict[str, list[tuple[float, float]]] = {}
    for device_id, s, e in presence_intervals(events):
        by_device.setdefault(device_id, []).append((s, e))

    results: list[ClearanceDeviceResult] = []
    for device_id, intervals in by_device.items():
        # 起始名单：[enter, leave) 在 start 处开着
        if not any(s <= start < e for s, e in intervals):
            continue
        # 截止状态：deadline 处仍有开着的区间即未清场；leave == deadline
        # 属于区间止端，设备已在外，按半开边界计为 cleared。
        open_at_deadline = [e for s, e in intervals if s <= deadline < e]
        if open_at_deadline:
            results.append(ClearanceDeviceResult(device_id, REMAINING, None))
        else:
            last_leave = max(e for s, e in intervals if e <= deadline)
            results.append(ClearanceDeviceResult(device_id, CLEARED, last_leave))

    results.sort(key=lambda r: r.device_id)
    return ClearanceReport(start=start, deadline=deadline, results=results)


# ---------- 有序门线巡检路线核验 ----------


CHECKPOINT_PASSED = "passed"                  # 按序、按方向、未超间隔通过
CHECKPOINT_TIMEOUT = "timeout"               # 与上一检查点的间隔超过上限
CHECKPOINT_SEQUENCE_VIOLATION = "sequence_violation"  # 反向穿越或后续门线先被穿越
CHECKPOINT_NOT_REACHED = "not_reached"       # 此前已失败或窗口结束仍未到达


@dataclass
class InspectionCheckpoint:
    """巡检路线上的一个有序门线检查点。"""

    position: int                 # 路线中的序号（从 0 起）
    gate_id: int
    direction: str                # 要求的穿越方向 forward | reverse
    max_interval_s: float | None  # 与上一检查点通过时刻的最长间隔；首点为 None


@dataclass
class InspectionCheckpointResult:
    """单个检查点的核验结果。"""

    position: int
    gate_id: int
    direction: str
    status: str
    passed_ts: float | None = None    # status == passed 时的通过时刻
    failed_ts: float | None = None    # timeout 的间隔截止时刻 / 顺序违规时刻


@dataclass
class InspectionReport:
    """巡检路线在给定设备与 [start, end) 窗口上的核验报告。"""

    route_id: int
    device_id: str
    start: float
    end: float
    results: list[InspectionCheckpointResult]

    @property
    def passed(self) -> bool:
        return len(self.results) > 0 and all(
            r.status == CHECKPOINT_PASSED for r in self.results
        )

    @property
    def first_violation_ts(self) -> float | None:
        for r in self.results:
            if r.failed_ts is not None:
                return r.failed_ts
        return None


def compute_inspection_report(
    route_id: int,
    device_id: str,
    checkpoints: list[InspectionCheckpoint],
    crossings: list[tuple[int, str, float]],
    start: float,
    end: float,
) -> InspectionReport:
    """按当前穿越记录重放有序门线巡检路线，核验设备在 [start, end) 内是否按序通过。

    crossings 为 (gate_id, direction, ts)，重放前按 (ts, gate_id) 升序排列。
    从检查点 0 开始逐一看待窗口内（start <= ts < end）的穿越：

    - 当前等待检查点的门线、方向都匹配 -> 通过并推进到下一检查点，记录穿越时刻；
    - 当前检查点门线的**反向**穿越 -> 立即顺序违规（记在该检查点，时刻为穿越时刻）；
    - **后续检查点**门线的**任意方向**穿越 -> 立即顺序违规（记在该后续检查点，
      “先穿了后面的门”，无论正反都说明路线顺序被破坏）；
    - 已通过检查点门线与其他门线的穿越一律忽略。

    超时：自上一检查点通过（首点为窗口 start）起，超过该检查点 max_interval_s
    仍未匹配，即在间隔截止时刻标记超时——即使在截止之后才补上匹配穿越。
    一旦发生任何违规，重放终止，其余检查点标记 not_reached；窗口结束时尚未
    到达且间隔已超限的检查点同样标记超时。
    """
    results = [
        InspectionCheckpointResult(
            position=cp.position, gate_id=cp.gate_id, direction=cp.direction,
            status=CHECKPOINT_NOT_REACHED,
        )
        for cp in checkpoints
    ]
    gate_to_position = {cp.gate_id: cp.position for cp in checkpoints}

    current = 0  # 下一个待匹配检查点序号
    last_pass_ts = start
    violated = False

    def deadline_of(pos: int) -> float | None:
        interval = checkpoints[pos].max_interval_s
        return None if interval is None else last_pass_ts + interval

    ordered = sorted(crossings, key=lambda c: (c[2], c[0]))
    for gate_id, direction, ts in ordered:
        if violated or ts < start or ts >= end:
            continue
        # 先判超时：间隔截止时刻已到（含等于，间隔允许等于上限）
        limit_ts = deadline_of(current)
        if limit_ts is not None and ts > limit_ts:
            results[current].status = CHECKPOINT_TIMEOUT
            results[current].failed_ts = limit_ts
            violated = True
            break

        pos = gate_to_position.get(gate_id)
        if pos is None:
            continue  # 其他门线忽略
        if pos == current:
            if direction == checkpoints[current].direction:
                results[current].status = CHECKPOINT_PASSED
                results[current].passed_ts = ts
                last_pass_ts = ts
                current += 1
                if current == len(checkpoints):
                    break
            else:
                # 当前检查点门线的反向穿越：立即顺序违规
                results[current].status = CHECKPOINT_SEQUENCE_VIOLATION
                results[current].failed_ts = ts
                violated = True
        elif pos > current:
            # 后续检查点门线：任意方向穿越都说明先穿了后面的门 -> 顺序违规
            results[pos].status = CHECKPOINT_SEQUENCE_VIOLATION
            results[pos].failed_ts = ts
            violated = True
        # pos < current 的门线已通过，再次穿越（任一方向）忽略

    if not violated and current < len(checkpoints):
        # 窗口结束仍未匹配：间隔已超限按超时，否则保持未到达
        limit_ts = deadline_of(current)
        if limit_ts is not None and end > limit_ts:
            results[current].status = CHECKPOINT_TIMEOUT
            results[current].failed_ts = limit_ts
        # 其余检查点保持 not_reached；current 本身未超限也保持 not_reached

    return InspectionReport(
        route_id=route_id,
        device_id=device_id,
        start=start,
        end=end,
        results=results,
    )


# ---------- 协同到场核验 ----------


@dataclass
class RendezvousSegment:
    """[start, end) 内全体成员共同在场的一个连续片段。"""

    start: float
    end: float
    members: tuple[str, ...]  # 全体成员（任务成员按 ID 排序）

    @property
    def duration_s(self) -> float:
        return self.end - self.start


@dataclass
class RendezvousReport:
    """协同到场核验报告：members 为任务成员（按 ID 排序），segments 为达标片段。"""

    start: float
    end: float
    min_co_presence_s: float
    members: tuple[str, ...]
    segments: list[RendezvousSegment]

    @property
    def passed(self) -> bool:
        return len(self.segments) > 0


def compute_rendezvous_report(
    events: list[tuple[str, str, float]],
    members: list[str],
    start: float,
    end: float,
    min_co_presence_s: float,
) -> RendezvousReport:
    """由 enter/leave 事件还原 [start, end) 内全体成员共同在场的连续片段。

    - 直接在事件流上做扫描线，不依赖区间配对：起始状态按
      #enter(ts<=start) - #leave(ts<=start) > 0 判定（enter@start 计入、
      leave@start 不计），窗口内每个事件时刻先一次性应用该时刻的全部进出
      （离开先、进入后），再判定下一微小区间 [t, next_t) 的在场集合。
    - 因此同刻 enter/leave 严格按半开边界处理——恰在 t 离开的成员不计入
      [t, next_t)，恰在 t 进入的成员计入；同刻 leave+enter 的成员在
      [t, next_t) 仍在场（旧区间止端不含 t，新区间起端含 t）。成员反复
      进出会在窗口边界与事件时刻上切出多个候选片段。
    - 全体成员同时在场的极大连续片段中，窗口内持续时长 >= min_co_presence_s
      （等于门槛算达标）的片段才保留，按起止时刻升序返回。无达标片段即失败。
    """
    member_set = set(members)
    ordered_members = tuple(sorted(member_set))

    per_dev_events: dict[str, list[tuple[float, str]]] = {}
    for dev, etype, ts in events:
        if dev in member_set and etype in (EVENT_ENTER, EVENT_LEAVE):
            per_dev_events.setdefault(dev, []).append((ts, etype))

    # 起始状态与窗口内边界：同刻离开先于进入应用（离开计数为负）
    inside: set[str] = set()
    boundaries = {start, end}
    toggles: dict[float, dict[str, int]] = {}
    for dev, evs in per_dev_events.items():
        enters = sorted(ts for ts, t in evs if t == EVENT_ENTER)
        leaves = sorted(ts for ts, t in evs if t == EVENT_LEAVE)
        if sum(1 for ts in enters if ts <= start) - sum(1 for ts in leaves if ts <= start) > 0:
            inside.add(dev)
        for ts in enters:
            if start < ts < end:
                toggles.setdefault(ts, {}).setdefault(dev, 0)
                toggles[ts][dev] += 1
                boundaries.add(ts)
        for ts in leaves:
            if start < ts <= end:
                toggles.setdefault(ts, {}).setdefault(dev, 0)
                toggles[ts][dev] -= 1
                boundaries.add(ts)

    segments: list[RendezvousSegment] = []
    seg_start: float | None = start if member_set.issubset(inside) else None
    for t in sorted(boundaries):
        if t == start:
            continue  # 起始状态已就位
        if t == end:
            if seg_start is not None:
                if end - seg_start >= min_co_presence_s:
                    segments.append(RendezvousSegment(seg_start, end, ordered_members))
            break
        delta = toggles[t]
        # 离开优先于进入，严格半开边界
        for dev, d in delta.items():
            if d < 0:
                inside.discard(dev)
        for dev, d in delta.items():
            if d > 0:
                inside.add(dev)
        all_present = member_set.issubset(inside)
        if all_present and seg_start is None:
            seg_start = t
        elif not all_present and seg_start is not None:
            if t - seg_start >= min_co_presence_s:
                segments.append(RendezvousSegment(seg_start, t, ordered_members))
            seg_start = None

    return RendezvousReport(
        start=start,
        end=end,
        min_co_presence_s=min_co_presence_s,
        members=ordered_members,
        segments=segments,
    )


# ---------- 设备接力交班核验 ----------


HANDOVER_PASSED = "passed"                        # 链上交班通过
HANDOVER_ABSENT = "absent"                        # 窗口内无满足时间推进的在场区间
HANDOVER_GAP_EXCEEDED = "gap_exceeded"            # 两段分离且空档超过最大值
HANDOVER_OVERLAP_INSUFFICIENT = "overlap_insufficient"  # 两段相交但交叠不足最短时长
HANDOVER_NOT_REACHED = "not_reached"              # 交班在此前失败后停止


@dataclass
class HandoverStep:
    """交班链上一个序号（一台设备）的核验结果。"""

    position: int
    device_id: str
    status: str
    interval_start: float | None = None   # 实际采用区间（截断到窗口）起端；无区间为 None
    interval_end: float | None = None     # 实际采用区间止端；未离开取窗口结束
    takeover_ts: float | None = None      # 接班时刻 = 区间起端；首台即到岗时刻
    violation_ts: float | None = None     # 首个违规时刻


@dataclass
class HandoverReport:
    """接力交班核验报告：steps 按交班顺序排列。"""

    start: float
    end: float
    max_gap_s: float
    min_overlap_s: float
    members: tuple[str, ...]
    steps: list[HandoverStep]

    @property
    def passed(self) -> bool:
        return len(self.steps) > 0 and all(
            s.status == HANDOVER_PASSED for s in self.steps
        )

    @property
    def passed_steps(self) -> int:
        """通过的相邻交班步数（链上设备数 - 1，全失败为 0）。"""
        on_chain = sum(1 for s in self.steps if s.status == HANDOVER_PASSED)
        return max(0, on_chain - 1)

    @property
    def first_violation_ts(self) -> float | None:
        for s in self.steps:
            if s.violation_ts is not None:
                return s.violation_ts
        return None


def compute_handover_report(
    events: list[tuple[str, str, float]],
    members: list[str],
    start: float,
    end: float,
    max_gap_s: float,
    min_overlap_s: float,
) -> HandoverReport:
    """由 enter/leave 事件还原接力交班核验报告。

    各设备在场区间一律为半开区间 [enter, leave)，截断到有效窗口
    [start, end)（窗口前已进入、窗口结束仍未离开的区间正确截断）。

    交班链选择（动态规划）：把每台设备的每个窗口内区间视为一个节点，
    相邻序号 i -> i+1 的两个区间在时间推进（接班区间起端不早于接班时刻）
    且满足交班约束时连边——两段相交（接班起端 < 交班止端）要求交叠时长
    >= min_overlap_s（含等于，min_overlap_s=0 时只要真正相交即达标）；
    两段分离（接班起端 >= 交班止端）要求空档 <= max_gap_s（含等于，首尾
    相接空档为 0）。选择通过的相邻交班步数最多的链；步数并列时按各步接班
    时刻组成的元组字典序最早，再并列取首台设备最早到岗区间。多个前缀汇聚
    到同一后续区间时其可延伸性完全相同（只取决于该区间），故每个节点保留
    完整的全局最早接班序列即可，避免按较晚接班元组丢链。

    链一旦无法延伸即停止：接班设备无满足时间推进的区间记 absent；
    有区间但最早候选交叠不足记 overlap_insufficient、空档超限记
    gap_exceeded（违规时刻分别为交叠结束时刻与空档超限时刻），
    其余序号记 not_reached。
    """
    per_device: dict[str, list[tuple[float, float]]] = {}
    for device_id, s, e in presence_intervals(events):
        per_device.setdefault(device_id, []).append((s, e))

    # 每台设备截断到窗口 [start, end) 的非空区间，按起端升序
    clipped: list[list[tuple[float, float]]] = []
    for dev in members:
        ivs = []
        for s, e in per_device.get(dev, []):
            cs, ce = max(s, start), min(e, end)
            if cs < ce:
                ivs.append((cs, ce))
        ivs.sort()
        clipped.append(ivs)

    def valid_edge(a: tuple[float, float], b: tuple[float, float]) -> bool:
        """交班区间 a -> 接班区间 b 是否满足约束（已隐含时间推进 b.s >= a.s）。"""
        if b[0] < a[1]:  # 两段相交：交叠 = min(止端) - 接班起端
            return min(a[1], b[1]) - b[0] >= min_overlap_s
        return b[0] - a[1] <= max_gap_s  # 分离（含首尾相接）：空档 <= 上限

    def better(cand: tuple, other: tuple | None) -> bool:
        """候选链是否更优：通过步数最多；并列时接班时刻元组字典序最早；
        再并列取首台设备最早到岗区间（完整的全局最早接班序列）。"""
        if other is None:
            return True
        if cand[0] != other[0]:
            return cand[0] > other[0]
        if cand[1] != other[1]:
            return cand[1] < other[1]
        return cand[2] < other[2]

    # best[position][j] = 以 clipped[position][j] 结尾的最优前缀
    # 取值 (通过步数, 接班时刻元组, 首步区间下标, 前驱 position, 前驱 j)。
    # 多个前缀汇聚到同一区间时可延伸性完全相同（只取决于该区间），因此每个
    # 节点只保留字典序最早的接班序列即可，丢掉的较晚序列不可能延伸出更早链。
    best: list[list[tuple | None]] = []
    best.append([(0, (), j, -1, -1) for j in range(len(clipped[0]))])
    for pos in range(1, len(members)):
        cur: list[tuple | None] = []
        for js, je in clipped[pos]:
            choice = None
            for k, (ps, pe) in enumerate(clipped[pos - 1]):
                prev = best[pos - 1][k]
                if prev is None or js < ps:
                    continue  # 接班区间起端不得早于前一步接班时刻
                if not valid_edge((ps, pe), (js, je)):
                    continue
                candidate = (prev[0] + 1, prev[1] + (js,), prev[2], pos - 1, k)
                if better(candidate, choice):
                    choice = candidate
            cur.append(choice)
        best.append(cur)

    # 全局最优：通过步数最多；并列按各步接班时刻元组字典序最早；
    # 再并列取首台设备最早到岗区间（确定性）
    winner: tuple | None = None
    winner_pos = winner_j = -1
    for pos in range(len(members)):
        for j, cand in enumerate(best[pos]):
            if cand is not None and better(cand, winner):
                winner, winner_pos, winner_j = cand, pos, j

    steps: list[HandoverStep] = []
    if winner is None:
        # 首台设备窗口内无任何在场区间：缺席并停止后续交班
        for pos, dev in enumerate(members):
            steps.append(HandoverStep(
                position=pos,
                device_id=dev,
                status=HANDOVER_ABSENT if pos == 0 else HANDOVER_NOT_REACHED,
                violation_ts=start if pos == 0 else None,
            ))
        return HandoverReport(
            start=start, end=end, max_gap_s=max_gap_s,
            min_overlap_s=min_overlap_s, members=tuple(members), steps=steps,
        )

    # 回溯选出的链
    chain_nodes: list[tuple[float, float] | None] = [None] * len(members)
    pos, j = winner_pos, winner_j
    while pos >= 0:
        chain_nodes[pos] = clipped[pos][j]
        _, _, _, ppos, pj = best[pos][j]
        pos, j = ppos, pj

    for pos, dev in enumerate(members):
        iv = chain_nodes[pos]
        if iv is not None:
            steps.append(HandoverStep(
                position=pos,
                device_id=dev,
                status=HANDOVER_PASSED,
                interval_start=iv[0],
                interval_end=iv[1],
                takeover_ts=iv[0],
            ))
            continue

        if pos == winner_pos + 1:
            # 交班失败的接班序号：依据最早满足时间推进的候选判定原因
            prev_iv = chain_nodes[pos - 1]
            assert prev_iv is not None
            candidates = [iv for iv in clipped[pos] if iv[0] >= prev_iv[0]]
            if not candidates:
                steps.append(HandoverStep(
                    position=pos, device_id=dev, status=HANDOVER_ABSENT,
                    violation_ts=end,
                ))
            else:
                cs, ce = candidates[0]
                if cs < prev_iv[1]:
                    status = HANDOVER_OVERLAP_INSUFFICIENT
                    violation_ts = min(prev_iv[1], ce)
                else:
                    status = HANDOVER_GAP_EXCEEDED
                    violation_ts = prev_iv[1] + max_gap_s
                steps.append(HandoverStep(
                    position=pos, device_id=dev, status=status,
                    interval_start=cs, interval_end=ce, takeover_ts=cs,
                    violation_ts=violation_ts,
                ))
        else:
            # 交班已在此前停止
            steps.append(HandoverStep(
                position=pos, device_id=dev, status=HANDOVER_NOT_REACHED,
            ))

    return HandoverReport(
        start=start,
        end=end,
        max_gap_s=max_gap_s,
        min_overlap_s=min_overlap_s,
        members=tuple(members),
        steps=steps,
    )


# ---------- 隔离冲突核验 ----------


@dataclass
class IsolationGroup:
    """隔离任务中的一个互斥围栏组（有效窗口各自独立）。"""

    position: int                       # 围栏组在任务中的序号（从 0 起）
    fence_ids: tuple[int, ...]          # 组内围栏 ID（已排序、不重复）
    start: float                        # 有效窗口开始（含）
    end: float                          # 有效窗口结束（不含）


@dataclass
class ConflictSegment:
    """单台设备在某个围栏组内的一个连续冲突片段。

    active 为该片段内设备同时所处的围栏集合（至少 2 个，按 ID 排序）；
    活跃集合一旦变化就在边界处切分，因此片段内活跃集合恒定。
    """

    start: float
    end: float
    active: tuple[int, ...]

    @property
    def duration_s(self) -> float:
        return self.end - self.start


@dataclass
class IsolationGroupReport:
    """单台设备在单个围栏组上的冲突核验结果（仅有冲突时出现）。"""

    position: int
    start: float
    end: float
    segments: list[ConflictSegment]

    @property
    def first_conflict_ts(self) -> float:
        return self.segments[0].start


@dataclass
class IsolationDeviceResult:
    """单台设备跨各围栏组的冲突结果，仅含至少发生过一次冲突的组。"""

    device_id: str
    groups: list[IsolationGroupReport]   # 按围栏组序号升序

    @property
    def first_conflict_ts(self) -> float:
        return min(g.first_conflict_ts for g in self.groups)


@dataclass
class IsolationReport:
    """隔离冲突核验报告：仅含有冲突的设备（按设备 ID 排序）。"""

    groups: tuple[IsolationGroup, ...]
    results: list[IsolationDeviceResult]

    @property
    def passed(self) -> bool:
        """无任何设备在任一组内同时处于两个及以上围栏即通过。"""
        return len(self.results) == 0


def _conflict_segments_for_group(
    intervals_by_fence: dict[int, list[tuple[float, float]]],
    group: IsolationGroup,
) -> list[ConflictSegment]:
    """还原一台设备在单个围栏组窗口内的连续冲突片段。

    每个围栏的半开在场区间 [enter, leave) 先截断到组窗口 [start, end)；
    随后在窗口端点与各截断区间端点组成的边界序列上扫描：

    - 边界处先按“同刻 leave 先于 enter”应用全部变化（区间半开配对天然
      保证：止端不含该刻、起端含该刻），再判定下一微小区间的活跃集合；
    - 活跃围栏 >= 2 即冲突，活跃集合变化就在边界切分（相邻同集合合并）。
    """
    # (边界时刻, 围栏 id, +1=起端/进入, -1=止端/离开)；起端排序在止端之后，
    # 与半开配对一致：同刻先应用离开再应用进入。
    events: list[tuple[float, int, int]] = []
    for fid in group.fence_ids:
        for s, e in intervals_by_fence.get(fid, []):
            cs, ce = max(s, group.start), min(e, group.end)
            if cs < ce:
                events.append((cs, fid, 1))
                events.append((ce, fid, -1))

    if not events:
        return []

    boundaries = {group.start, group.end}
    boundaries.update(t for t, _f, _d in events)
    ordered = sorted(boundaries)
    events.sort(key=lambda x: (x[0], x[2]))  # 同刻离开（-1）先于进入（+1）

    segments: list[ConflictSegment] = []
    active: set[int] = set()
    idx = 0
    seg_start: float | None = None
    seg_active: tuple[int, ...] = ()
    for a, b in zip(ordered, ordered[1:]):
        # 应用时刻 a 上的全部变化（离开先、进入后）；区间 [a, b) 内活跃集合恒定
        while idx < len(events) and events[idx][0] == a:
            _ts, fid, delta = events[idx]
            if delta < 0:
                active.discard(fid)
            else:
                active.add(fid)
            idx += 1
        cur = tuple(sorted(active))
        in_conflict = len(active) >= 2
        if in_conflict:
            if seg_start is None:
                seg_start, seg_active = a, cur
            elif cur != seg_active:
                # 活跃集合变化：在 a 处切分，新片段自 a 起
                segments.append(ConflictSegment(seg_start, a, seg_active))
                seg_start, seg_active = a, cur
        elif seg_start is not None:
            segments.append(ConflictSegment(seg_start, a, seg_active))
            seg_start, seg_active = None, ()

    if seg_start is not None:
        segments.append(ConflictSegment(seg_start, group.end, seg_active))
    return segments


def compute_isolation_report(
    events: list[tuple[str, int, str, float]],
    groups: list[IsolationGroup],
) -> IsolationReport:
    """由 enter/leave 事件实时重建隔离冲突核验报告。

    events 为 (device_id, fence_id, event_type, ts)。按 (设备, 围栏) 配对
    成半开在场区间 [enter, leave)，再逐组逐设备求冲突片段。

    各围栏组独立判定：同一设备在组内有效窗口 [start, end) 内同时处于两个
    及以上围栏即为冲突。在场时间一律按半开区间 [enter, leave) 还原并截断
    到组窗口；同刻 leave 先于 enter，跨窗口边界正确截断。

    返回的报告仅含至少发生过一次冲突的设备（设备 ID 升序），每台设备下列
    出有冲突的组（按组序号升序）及其连续冲突片段（按起止时刻升序，活跃
    围栏集合变化即切分，片段给出按 ID 排序的活跃围栏）。无任何冲突时
    passed 为 true、results 为空。
    """
    per: dict[tuple[str, int], list[tuple[float, str]]] = {}
    for device_id, fence_id, etype, ts in events:
        if etype in (EVENT_ENTER, EVENT_LEAVE):
            per.setdefault((device_id, fence_id), []).append((ts, etype))

    paired: dict[str, dict[int, list[tuple[float, float]]]] = {}
    for (device_id, fence_id), evs in per.items():
        paired.setdefault(device_id, {})[fence_id] = pair_intervals(evs)

    ordered_groups = sorted(groups, key=lambda g: g.position)
    results: list[IsolationDeviceResult] = []
    for device_id in sorted(paired):
        group_reports: list[IsolationGroupReport] = []
        for group in ordered_groups:
            segs = _conflict_segments_for_group(paired[device_id], group)
            if segs:
                group_reports.append(IsolationGroupReport(
                    position=group.position,
                    start=group.start,
                    end=group.end,
                    segments=segs,
                ))
        if group_reports:
            results.append(IsolationDeviceResult(device_id, group_reports))

    results.sort(key=lambda r: r.device_id)
    return IsolationReport(groups=tuple(ordered_groups), results=results)
