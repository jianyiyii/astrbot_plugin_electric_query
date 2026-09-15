# -*- coding: utf-8 -*-
"""
断电预测：简单可靠的 线性回归 + 移动平均。
输入: [(created_at 字符串, 剩余电量), ...]（升序，来自 database.query_history）
输出: {"rate","hours","outage_iso","reason"}
"""

from datetime import datetime, timedelta

TS_FMT = "%Y-%m-%d %H:%M:%S"


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, TS_FMT)


def _linear_rate(points: list) -> float:
    """对 (datetime, 剩余电量) 做最小二乘线性拟合，返回每小时耗电度数（>0）。
    样本 <2 或时间无变化时返回 None。时间跨度过短（<30 分钟）时拟合不可信，
    返回 None 交给移动平均兜底。"""
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
    """移动平均：对相邻样本间「剩余下降」的每小时消耗取均值。
    自动忽略充值导致的上升。用于线性拟合失败的兜底。"""
    rates = []
    for (t1, r1), (t2, r2) in zip(points, points[1:]):
        span_h = (t2 - t1).total_seconds() / 3600.0
        drop = r1 - r2
        if drop > 0 and span_h >= 0.25:  # 忽略间隔过短与充值跳变
            rates.append(drop / span_h)
    if not rates:
        return None
    rates.sort()
    n = len(rates)
    mid = rates[n // 2] if n % 2 else (rates[n // 2 - 1] + rates[n // 2]) / 2
    return mid if mid > 0 else None


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


def predict(points: list, remaining: float = None, now: datetime = None) -> dict:
    """
    预测断电时间。
    points: [(iso_str, remaining)] 升序，可为空。
    remaining: 当前剩余电量，缺省用最新样本。
    返回:
      rate      每小时耗电（度/小时），无法计算为 None
      hours     预计还能使用的小时数，无法计算为 None
      outage_iso 预计断电时间 ISO 字符串
      reason    说明（数据不足 / 耗电极慢 / 已耗尽等）
    """
    now = now or datetime.now()
    if len(points) < 2:
        return {"rate": None, "hours": None, "outage_iso": None,
                "reason": "数据不足（至少需要 2 个采样点，建议积累数小时）"}
    parsed = [(parse_ts(ts), r) for ts, r in points]

    rate = None
    # 优先：最后一次充值之后的段做线性拟合（最贴近当前用电习惯）
    for ep in reversed(split_epochs(parsed)):
        if len(ep) >= 2:
            rate = _linear_rate(ep)
            if rate:
                break
    # 回退1：全部样本线性拟合
    if not rate:
        rate = _linear_rate(parsed)
    # 回退2：移动平均
    if not rate:
        rate = _moving_avg_rate(parsed)
    if not rate:
        return {"rate": None, "hours": None, "outage_iso": None,
                "reason": "近期耗电极少或波动过大，暂无法预测"}

    remaining = remaining if remaining is not None else parsed[-1][1]
    if remaining <= 0:
        return {"rate": rate, "hours": 0.0,
                "outage_iso": now.strftime(TS_FMT), "reason": "电量已耗尽"}
    hours = remaining / rate
    outage = now + timedelta(hours=hours)
    return {"rate": rate, "hours": hours,
            "outage_iso": outage.strftime(TS_FMT), "reason": ""}
