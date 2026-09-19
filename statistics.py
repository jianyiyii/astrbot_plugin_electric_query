# -*- coding: utf-8 -*-
"""
用电统计：窗口消耗、日均、峰值时段、今日/昨日趋势。
输入为 database.query_history 的返回（[(created_at, remaining), ...]，升序）。
"""

from datetime import datetime, timedelta

try:
    from .forecast import parse_ts
except ImportError:  # 允许以顶层脚本方式直接运行
    from forecast import parse_ts  # type: ignore


def consumption_window(points: list, start_dt: datetime, end_dt: datetime):
    """
    窗口内消耗度数。只累计相邻样本间的「剩余下降量」，自动忽略充值跳变。
    返回 (消耗度数 or None(样本不足2), 跨度小时, 样本数)。
    """
    rows = [(parse_ts(ts), r) for ts, r in points
            if start_dt <= parse_ts(ts) <= end_dt]
    if len(rows) < 2:
        return (None, 0.0, len(rows))
    rows.sort(key=lambda x: x[0])
    total = 0.0
    for (t1, r1), (t2, r2) in zip(rows, rows[1:]):
        drop = r1 - r2
        if drop > 0:
            total += drop
    span = (rows[-1][0] - rows[0][0]).total_seconds() / 3600.0
    return (round(total, 2), span, len(rows))


def recent_24h(points: list):
    """最近 24 小时（滚动）消耗。返回 度 or None。"""
    now = datetime.now()
    total, _, n = consumption_window(points, now - timedelta(hours=24), now)
    return total


def avg_daily_consumption(points: list, days: int = 7):
    """最近 days 天平均每天消耗（按实际跨度折算）。返回 度 or None。"""
    now = datetime.now()
    total, span, _ = consumption_window(points, now - timedelta(days=days), now)
    if total is None or span <= 0:
        return None
    return round(total * 24.0 / span, 2)


def peak_window(points: list, days: int = 7):
    """最近 days 天中消耗最快的相邻采样间隔。
    返回 (开始HH:MM, 结束HH:MM, 消耗度数) or None。"""
    now = datetime.now()
    rows = [(parse_ts(ts), r) for ts, r in points
            if now - timedelta(days=days) <= parse_ts(ts) <= now]
    rows.sort(key=lambda x: x[0])
    best = None
    for (t1, r1), (t2, r2) in zip(rows, rows[1:]):
        drop = r1 - r2
        if drop > 0 and (best is None or drop > best[2]):
            best = (t1, t2, drop)
    if not best:
        return None
    return (best[0].strftime("%H:%M"), best[1].strftime("%H:%M"), round(best[2], 2))


def today_vs_yesterday(points: list):
    """返回 (今日消耗, 昨日消耗)，可能为 None。"""
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yest_start = today_start - timedelta(days=1)
    today, _, _ = consumption_window(points, today_start, now)
    yest, _, _ = consumption_window(points, yest_start, today_start)
    return today, yest


def same_period_today_vs_yesterday(points: list):
    """
    同期对比：今日 0 点至「现在」的消耗 vs 昨日 0 点至「昨日的同一相对时刻」。
    早上查询时今日消耗会与昨日同时刻对齐，避免把「今天还没过完」误判为暴跌。
    返回 (今日同期, 昨日同期)，可能为 None。
    """
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = now - today_start
    yest_start = today_start - timedelta(days=1)
    yest_cut = yest_start + elapsed
    today, _, _ = consumption_window(points, today_start, now)
    yest, _, _ = consumption_window(points, yest_start, yest_cut)
    return today, yest


def day_partition(points: list, days: int = 7, day_start: int = 8,
                  night_start: int = 22):
    """
    近 days 天白天（8-22 点）/ 夜间（22-8 点）消耗及占比。
    返回 {"day": 度, "night": 度, "day_pct": %, "night_pct": %}（无数据 pct 为 None）。
    """
    now = datetime.now()
    start = now - timedelta(days=days)
    rows = sorted((parse_ts(ts), r) for ts, r in points
                  if start <= parse_ts(ts) <= now)
    day = night = 0.0
    for (t1, r1), (t2, r2) in zip(rows, rows[1:]):
        drop = r1 - r2
        if drop <= 0:
            continue
        span = (t2 - t1).total_seconds()
        cur = t1
        while cur < t2:
            nxt = (cur + timedelta(hours=1)).replace(minute=0, second=0,
                                                      microsecond=0)
            if nxt > t2:
                nxt = t2
            seg = (nxt - cur).total_seconds()
            if seg <= 0:
                break
            part = "day" if day_start <= cur.hour < night_start else "night"
            amount = drop * (seg / span)
            if part == "day":
                day += amount
            else:
                night += amount
            cur = nxt
    total = day + night
    return {"day": round(day, 2), "night": round(night, 2),
            "day_pct": round(day / total * 100, 1) if total > 0 else None,
            "night_pct": round(night / total * 100, 1) if total > 0 else None}


def daily_consumptions(points: list, days: int = 7):
    """
    近 days 天每天消耗。返回 [(日期 MM-DD, 消耗 or None), ...]（升序）。
    消耗为 None 表示该日无足够样本。
    """
    now = datetime.now()
    res = []
    for i in range(days - 1, -1, -1):
        day_start = (now - timedelta(days=i)).replace(hour=0, minute=0,
                                                      second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        total, _, n = consumption_window(points, day_start, day_end)
        res.append((day_start.strftime("%m-%d"), total))
    return res
