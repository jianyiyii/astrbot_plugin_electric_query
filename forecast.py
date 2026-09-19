# -*- coding: utf-8 -*-
"""
断电预测：分时速率 Profile + 逐小时模拟耗尽（主），
段内线性回归 / 移动平均（兜底）。
=====================================================================
输入: [(created_at 字符串, 剩余电量), ...]（升序，来自 database.query_history）
输出: {"rate","hours","outage_iso","reason","method","hours24"}

算法（v4.3.0）：
  1) 分时 Profile：用最近 PROFILE_DAYS 天的样本，把相邻采样间的"剩余下降"
     按小时边界切分摊入 (工作/周末 × 24) 个速率桶；每桶 = 消耗/覆盖天数。
     桶覆盖不足的用整体均值兜底；整体覆盖不足则放弃 Profile。
  2) 模拟耗尽：从"现在"起逐小时按该时刻（周几类型 × 小时）速率扣减剩余，
     直到 0 得到断电时刻；30 天内未耗尽视为"电量充足"。
  3) 保守修正：模拟结果 × CONSERVATIVE_FACTOR（默认 0.85），宁可早提醒。
  4) 兜底：Profile 不可用或模拟失败时，沿用最后一次充值后段线性回归 →
     全部样本回归 → 移动平均中位数。
"""

from datetime import datetime, timedelta

TS_FMT = "%Y-%m-%d %H:%M:%S"

# ---- Profile 参数 ----
PROFILE_DAYS = 14              # 分时 Profile 的采样窗口（滑动跟随季节）
RECENT_WINDOW_DAYS = 7         # 近窗口天数（权重 1）
OLDER_WEIGHT = 0.5             # 前窗权重（季节过渡平滑，0.5:1）
MIN_COVER_DAYS = 2             # 单个桶至少覆盖的天数才算有效（周末一周仅 2 天）
MIN_PROFILE_BUCKETS = 6        # 有效桶数下限，低于则放弃 Profile 模式
MIN_SEG_SECONDS = 900          # 采样间隔 <15 分钟忽略（防近重复样本抖动）
SIM_CAP_DAYS = 30              # 模拟耗尽上限
CONSERVATIVE_FACTOR = 0.85     # 保守修正系数

WORKDAY = 0
WEEKEND = 1


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, TS_FMT)


def _weekday_class(dt: datetime) -> int:
    return WEEKEND if dt.weekday() >= 5 else WORKDAY


# ---------------------------------------------------------------------------
# 1) 分时速率 Profile
# ---------------------------------------------------------------------------
def build_hour_profile(points: list, days: int = PROFILE_DAYS,
                       min_cover_days: int = MIN_COVER_DAYS):
    """
    从历史采样构建分时速率曲线（双窗口平滑）。
    返回 {"rates": {0:[24],1:[24]}, "days": 覆盖总天数, "recent_days": 近窗天数,
          "avg_rate": 整体均值}
    或 None（数据不足以构成 Profile）。
    近 RECENT_WINDOW_DAYS 天权重 1，更早窗口权重 OLDER_WEIGHT（0.5），
    季节 / 作息变化时曲线过渡更平滑；rates 中无效桶已用整体均值填充。
    """
    now = datetime.now()
    start = now - timedelta(days=days)
    parsed = []
    for ts, r in points:
        try:
            dt = parse_ts(ts)
            rv = float(r)
        except (ValueError, TypeError):
            continue  # 跳过异常历史数据（坏时间戳/非数字电量），避免单条脏数据让整个预测失败
        if dt >= start:
            parsed.append((dt, rv))
    parsed.sort(key=lambda x: x[0])
    if len(parsed) < 2:
        return None

    split_dt = now - timedelta(days=RECENT_WINDOW_DAYS)
    cons = {"recent": {0: [0.0] * 24, 1: [0.0] * 24},
            "older": {0: [0.0] * 24, 1: [0.0] * 24}}
    day_sets = {"recent": {0: [set() for _ in range(24)], 1: [set() for _ in range(24)]},
                "older": {0: [set() for _ in range(24)], 1: [set() for _ in range(24)]}}
    total_cons = {"recent": 0.0, "older": 0.0}
    total_days = {"recent": set(), "older": set()}

    for (t1, r1), (t2, r2) in zip(parsed, parsed[1:]):
        drop = r1 - r2
        if drop <= 0:
            continue
        span = (t2 - t1).total_seconds()
        if span < MIN_SEG_SECONDS:
            continue
        cur = t1
        while cur < t2:
            seg_end = (cur + timedelta(hours=1)).replace(minute=0, second=0,
                                                          microsecond=0)
            if seg_end > t2:
                seg_end = t2
            seg = (seg_end - cur).total_seconds()
            if seg <= 0:
                break
            w = "recent" if cur >= split_dt else "older"
            cls = _weekday_class(cur)
            h = cur.hour
            seg_cons = drop * (seg / span)
            cons[w][cls][h] += seg_cons
            day_sets[w][cls][h].add(cur.date())
            total_cons[w] += seg_cons
            total_days[w].add(cur.date())
            cur = seg_end

    # 每窗口速率：桶覆盖达标才算有效，未达标先留空（最后整体均值兜底）
    rates = {"recent": {0: [None] * 24, 1: [None] * 24},
             "older": {0: [None] * 24, 1: [None] * 24}}
    for w in ("recent", "older"):
        for cls in (WORKDAY, WEEKEND):
            for h in range(24):
                d = len(day_sets[w][cls][h])
                if d >= min_cover_days:
                    rates[w][cls][h] = cons[w][cls][h] / d

    def _avg(w):
        d = len(total_days[w])
        return (total_cons[w] / (d * 24.0)) if d else 0.0

    # 合并：近窗优先；两窗均有效则加权 (1:0.5)
    merged = {0: [None] * 24, 1: [None] * 24}
    valid = 0
    for cls in (WORKDAY, WEEKEND):
        for h in range(24):
            r_ = rates["recent"][cls][h]
            o_ = rates["older"][cls][h]
            if r_ is not None and o_ is not None:
                merged[cls][h] = (r_ + o_ * OLDER_WEIGHT) / (1 + OLDER_WEIGHT)
                valid += 1
            elif r_ is not None:
                merged[cls][h] = r_
                valid += 1
            elif o_ is not None:
                merged[cls][h] = o_
                valid += 1
    if valid < MIN_PROFILE_BUCKETS:
        return None

    # 整体均值（双窗加权）兜底无效桶
    w_denom = (len(total_days["recent"]) + OLDER_WEIGHT * len(total_days["older"]))
    avg_rate = ((total_cons["recent"] + OLDER_WEIGHT * total_cons["older"])
                / (w_denom * 24.0)) if w_denom else 0.0
    for cls in (WORKDAY, WEEKEND):
        for h in range(24):
            if merged[cls][h] is None:
                merged[cls][h] = avg_rate
    return {"rates": merged,
            "days": len(total_days["recent"]) + len(total_days["older"]),
            "recent_days": len(total_days["recent"]),
            "avg_rate": avg_rate}


# ---------------------------------------------------------------------------
# 2) 逐小时模拟耗尽 + 窗口用量
# ---------------------------------------------------------------------------
def _next_hour_boundary(dt: datetime) -> datetime:
    return (dt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def sim_deplete(profile: dict, remaining: float, now: datetime = None,
                cap_days: int = SIM_CAP_DAYS):
    """
    按分时速率逐小时扣减剩余电量。
    返回 (hours, outage_iso)；cap 内未耗尽返回 (None, None)。
    """
    now = now or datetime.now()
    if remaining <= 0:
        return 0.0, now.strftime(TS_FMT)
    t = now
    left = float(remaining)
    cap = now + timedelta(days=cap_days)
    while t < cap:
        nxt = _next_hour_boundary(t)
        if nxt > cap:
            nxt = cap
        dt = (nxt - t).total_seconds() / 3600.0
        rate = profile["rates"][_weekday_class(t)][t.hour]
        if rate > 0:
            left -= rate * dt
            if left <= 0:
                # 在 dt 段内线性内插精确断电时刻
                overshoot = -left
                used_h = max(0.0, dt - overshoot / rate)
                exact = t + timedelta(hours=used_h)
                hours = (exact - now).total_seconds() / 3600.0
                return hours, exact.strftime(TS_FMT)
        t = nxt
    return None, None


def usage_between(profile: dict, t_start: datetime, t_end: datetime) -> float:
    """t_start 到 t_end 的预计消耗（度），按分时速率逐段累加。"""
    total = 0.0
    t = t_start
    while t < t_end:
        nxt = _next_hour_boundary(t)
        if nxt > t_end:
            nxt = t_end
        dt = (nxt - t).total_seconds() / 3600.0
        total += profile["rates"][_weekday_class(t)][t.hour] * dt
        t = nxt
    return total


def peak_hours(profile: dict, now: datetime = None,
               horizon_hours: int = 24, top: int = 2) -> list:
    """未来 horizon_hours 内消耗速率最高的 top 个整点时段。
    返回 [("20:00-21:00", 0.80), ...]（速率 >0 且按速率降序）。"""
    now = now or datetime.now()
    slots = []
    t = now
    end = now + timedelta(hours=horizon_hours)
    while t < end:
        rate = profile["rates"][_weekday_class(t)][t.hour]
        if rate > 0:
            slots.append((f"{t.strftime('%H:%M')}-{(t + timedelta(hours=1)).strftime('%H:%M')}",
                          rate))
        t += timedelta(hours=1)
    slots.sort(key=lambda x: -x[1])
    return slots[:top]


# ---------------------------------------------------------------------------
# 3) 兜底：线性回归 / 移动平均
# ---------------------------------------------------------------------------
def _linear_rate(points: list) -> float:
    """对 (datetime, 剩余电量) 做最小二乘线性拟合，返回每小时耗电度数（>0）。
    样本 <2、跨度 <30 分钟时返回 None（拟合不可信）。"""
    if len(points) < 2:
        return None
    span_h = (points[-1][0] - points[0][0]).total_seconds() / 3600.0
    if span_h < 0.5:
        return None
    xs = [p[0].timestamp() / 3600.0 for p in points]
    ys = [p[1] for p in points]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return None
    slope = cov / var  # 度/小时（剩余电量随时间下降，斜率为负）
    rate = -slope
    return rate if rate > 0 else None


def _moving_avg_rate(points: list) -> float:
    """移动平均：对相邻样本间『剩余下降』的每小时消耗取中位数。
    自动忽略充值上升与间隔过短的对。"""
    rates = []
    for (t1, r1), (t2, r2) in zip(points, points[1:]):
        span_h = (t2 - t1).total_seconds() / 3600.0
        drop = r1 - r2
        if drop > 0 and span_h >= 0.25:
            rates.append(drop / span_h)
    if not rates:
        return None
    rates.sort()
    n = len(rates)
    return rates[n // 2] if n % 2 else (rates[n // 2 - 1] + rates[n // 2]) / 2


def split_epochs(points: list) -> list:
    """按充值跳变分段：剩余电量较前一点显著增大视为一次充值，从该处切断。"""
    epochs = []
    cur = []
    prev = None
    for p in points:
        if prev is not None and p[1] > prev[1] + max(0.5, prev[1] * 0.05):
            if cur:
                epochs.append(cur)
            cur = []
        cur.append(p)
        prev = p
    if cur:
        epochs.append(cur)
    return epochs


# ---------------------------------------------------------------------------
# 4) 统一入口
# ---------------------------------------------------------------------------
def predict(points: list, remaining: float = None, now: datetime = None) -> dict:
    """
    预测断电时间。
    points: [(iso_str, remaining)] 升序，可为空。
    remaining: 当前剩余电量，缺省用最新样本。
    返回:
      rate       每小时耗电（度/小时，Profile 模式为整体均值）
      hours      预计还能使用的小时数（保守修正后），无法计算为 None
      outage_iso 预计断电时间 ISO 字符串
      reason     说明
      method     "profile"（分时模拟）/ "regression"（回归兜底）/ "none"
      hours24    未来 24 小时预计消耗（仅 profile 模式）
    """
    now = now or datetime.now()
    empty = {"rate": None, "hours": None, "outage_iso": None,
             "reason": "数据不足（至少需要 2 个采样点，建议积累数小时）",
             "method": "none", "hours24": None, "peaks": [], "profile_days": None}
    if len(points) < 2:
        return empty
    parsed = []
    for ts, r in points:
        try:
            parsed.append((parse_ts(ts), float(r)))
        except (ValueError, TypeError):
            continue  # 容错：跳过异常历史数据
    if len(parsed) < 2:
        return empty
    remaining = remaining if remaining is not None else parsed[-1][1]
    if remaining <= 0:
        return {"rate": None, "hours": 0.0,
                "outage_iso": now.strftime(TS_FMT), "reason": "电量已耗尽",
                "method": "none", "hours24": None, "peaks": [], "profile_days": None}

    # ---- 主路径：分时 Profile 模拟耗尽 ----
    profile = build_hour_profile(points)
    if profile:
        sim_hours, sim_outage = sim_deplete(profile, remaining, now)
        if sim_hours is not None:
            conservative = sim_hours * CONSERVATIVE_FACTOR
            outage_c = now + timedelta(hours=conservative)
            h24 = usage_between(profile, now, now + timedelta(hours=24))
            peaks = peak_hours(profile, now, 24, 2)
            return {"rate": round(profile["avg_rate"], 4),
                    "hours": round(conservative, 2),
                    "outage_iso": outage_c.strftime(TS_FMT),
                    "reason": "",
                    "method": "profile",
                    "hours24": round(h24, 2),
                    "peaks": peaks,
                    "profile_days": profile["days"]}

    # ---- 兜底：段回归 → 整体回归 → 移动平均 ----
    rate = None
    for ep in reversed(split_epochs(parsed)):
        if len(ep) >= 2:
            rate = _linear_rate(ep)
            if rate:
                break
    if not rate:
        rate = _linear_rate(parsed)
    if not rate:
        rate = _moving_avg_rate(parsed)
    if not rate:
        return {"rate": None, "hours": None, "outage_iso": None,
                "reason": "近期耗电极少或波动过大，暂无法预测",
                "method": "none", "hours24": None, "peaks": [], "profile_days": None}
    hours = remaining / rate
    outage = now + timedelta(hours=hours)
    return {"rate": round(rate, 4), "hours": round(hours, 2),
            "outage_iso": outage.strftime(TS_FMT), "reason": "",
            "method": "regression", "hours24": None, "peaks": [], "profile_days": None}