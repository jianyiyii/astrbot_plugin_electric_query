# -*- coding: utf-8 -*-
"""
山文宿舍电费查询 AstrBot 插件 —— 宿舍电量管家
=============================================
面向山东文化产业职业学院（山文）学生。

功能：
  · 实时查询剩余电量（电费 / 查询电费 / 电量 [房间号]）
  · 历史电量记录（SQLite: electric_history.db / power_history 表）
  · 用电统计（/用电统计）：最近 24h 消耗、7 天日均、最高耗电时段
  · 断电预测（/预测停电）：线性回归 + 移动平均，预计剩余时间与断电时间
  · 用电趋势（/用电趋势）：今日 vs 昨日消耗与变化
  · 智能提醒：按预计断电时间分级（>24h 不提醒 / 12-24h 普通 /
    <12h 强提醒 / <3h 紧急）；预测数据不足时回退固定阈值
  · 夜间断电保护：每天 22:00 检查，若预测凌晨 0-8 点断电则推送夜间风险

数据来源：山东文化产业职业学院校园一卡通 (card.sdcivc.edu.cn) 公开接口，
无需登录/token。充值仅提供官方入口链接，支付由学生手动完成。

=== 接口（不变，2026-09-14 实测）===
  网关基址: https://card.sdcivc.edu.cn/bc/gateway
  楼栋列表: POST {base}/ecu/api/roomInfo/query/1   body: {"loudongId": null}
  房间列表: POST {base}/ecu/api/roomInfo/query/2   body: {"loudongId": <楼栋id>}
  剩余电量 = allAmp - usedAmp（度）
"""

import asyncio
import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta

import aiohttp

try:
    # AstrBot >= v3.4 推荐导入路径
    from astrbot.api.star import Context, Star, register
    from astrbot.api.event import filter, AstrMessageEvent
    from astrbot.api import logger
except ImportError:  # 兼容旧版本 AstrBot
    from astrbot.core.star import Context, Star, register
    from astrbot.api import logger
    from astrbot.core.star.filter.event import EventType

    filter = None

from . import database
from . import forecast as forecast_mod
from . import statistics as stats

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
# v4.5.0 起持久化数据统一存到 AstrBot 约定目录 data/plugin_data/electric_query/
DATA_FILE = os.path.join(database.get_data_dir(), "data.json")

METER_TYPES = ("照明", "空调", "水表")
METER_ICONS = {"照明": "💡", "空调": "❄️", "水表": "🚰"}

DEFAULT_CONFIG = {
    # ---- 默认宿舍 ----
    "building": "2号公寓",
    "room": "319",
    # 展示的电表类型：照明/空调/水表；[] = 全部显示
    "meter_types": ["照明", "空调"],
    # ---- 网络 ----
    "query_url": "https://card.sdcivc.edu.cn/bc/gateway",
    "cache_ttl": 300,
    "timeout": 15,
    "retries": 2,
    # ---- 历史数据 ----
    "history_keep_days": 30,   # SQLite 保留天数，超期自动清理
    # ---- 定时监控 ----
    "check_enabled": True,
    "check_interval_hours": 6,   # 自动检查间隔（小时），24 = 每天一次
    # 固定阈值（预测数据不足时的回退）
    "warn_threshold": 20.0,
    "alert_threshold": 10.0,
    "warn_repeat_hours": 24,
    "alert_repeat_hours": 6,
    # 预测式提醒阈值（按预计断电剩余小时数）
    "warn_hours": 24.0,      # <=24 小时：普通提醒
    "alert_hours": 12.0,     # <=12 小时：强提醒
    "urgent_hours": 3.0,     # <=3 小时：紧急提醒
    # 提醒级别细分开关：两条通道（固定阈值 / 预测剩余时间）各级别可独立启停。
    # 某一级别被关闭时，同通道会自动降到下一已启用级别；全关则静默。
    "enable_degree_warn": True,     # 固定阈值-普通提醒（< warn_threshold 度）
    "enable_degree_alert": True,    # 固定阈值-强提醒（< alert_threshold 度）
    "enable_hour_warn": True,       # 预测式-普通提醒（预计 <= warn_hours 小时断电）
    "enable_hour_alert": True,      # 预测式-强提醒（预计 <= alert_hours 小时断电）
    "enable_hour_urgent": True,     # 预测式-紧急提醒（预计 <= urgent_hours 小时断电）
    # ---- 夜间断电保护 ----
    "night_check_enabled": True,
    "night_check_time": "22:00",
    "watch_rooms": [],           # 额外监控房间，如 ["5号公寓:206"]
    "notify_sessions": [],       # 推送目标；留空=自动记住使用过指令的会话
    # ---- 充值入口提示（只给链接，支付手动完成） ----
    "recharge_url": "https://card.sdcivc.edu.cn/#/pages/home/sdwhcyxy-card/electronic-pay"
}


def load_config() -> dict:
    """读取插件目录下的 config.json，缺失的键用默认值补齐。"""
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(PLUGIN_DIR, "config.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
    except Exception as e:  # 配置损坏时回落到默认值，不让插件崩溃
        logger.error(f"[electric_query] 读取 config.json 失败: {e}")
    if isinstance(cfg.get("meter_types"), str):
        cfg["meter_types"] = [t.strip() for t in
                              re.split(r"[,，、\s]+", cfg["meter_types"]) if t.strip()]
    cfg.setdefault("meter_types", [])
    return cfg


# ---------------------------------------------------------------------------
# 纯 API 客户端（不依赖 AstrBot，可单独复用 / 测试）
# ---------------------------------------------------------------------------
class ElectricApiClient:
    def __init__(self, base_url: str = "", timeout: int = 15, retries: int = 2):
        self.base = (base_url or "https://card.sdcivc.edu.cn/bc/gateway").rstrip("/")
        self.timeout = timeout
        self.retries = max(0, retries)
        self._building_cache = {}
        self._building_cache_at = 0.0

    @staticmethod
    def _headers() -> dict:
        return {"Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (AstrBot electric_query plugin)"}

    async def _post_json(self, session: aiohttp.ClientSession, url: str,
                         payload: dict) -> list:
        """带重试的 POST；校验返回必须是 JSON 数组，否则视为接口变更。"""
        last_err = None
        for attempt in range(self.retries + 1):
            try:
                async with session.post(url, json=payload, headers=self._headers(),
                                        timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"接口 HTTP {resp.status}（可能接口已变更）")
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        raise RuntimeError("接口返回非 JSON（可能接口已变更）")
                    if not isinstance(data, list):
                        raise RuntimeError(f"接口返回结构异常（期望数组）: {text[:120]}")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = e
                logger.warning(f"[electric_query] 请求失败(第{attempt + 1}次): {e}")
                if attempt < self.retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"网络请求失败（已重试{self.retries + 1}次）: {last_err}")

    async def get_buildings(self, session: aiohttp.ClientSession) -> dict:
        """{楼栋名: loudongId}，带 24h 进程内缓存。"""
        if self._building_cache and time.time() - self._building_cache_at < 86400:
            return self._building_cache
        data = await self._post_json(session,
                                     f"{self.base}/ecu/api/roomInfo/query/1",
                                     {"loudongId": None})
        buildings = {str(i.get("loudong", "")).strip(): i.get("loudongId")
                     for i in data if str(i.get("loudong", "")).strip()}
        if not buildings:
            raise RuntimeError("楼栋列表为空，接口可能已变更")
        self._building_cache, self._building_cache_at = buildings, time.time()
        return buildings

    @staticmethod
    def _match_building(buildings: dict, name: str):
        """支持 '2号公寓' / '2' / '2号楼' 等写法。"""
        if name in buildings:
            return name
        num = re.sub(r"[^\d]", "", name)
        for bname in buildings:
            if num and re.sub(r"[^\d]", "", bname) == num:
                return bname
        return None

    async def query_room(self, building: str, room: str, meter_types=None) -> list:
        """查询房间电表。返回 [{"loudong","room","roomId","allAmp","usedAmp","remain"}]"""
        room = re.sub(r"[^\d]", "", room)
        if not room:
            raise ValueError("房间号格式不正确，示例：319 或 2-319")
        async with aiohttp.ClientSession() as session:
            buildings = await self.get_buildings(session)
            bname = self._match_building(buildings, building.strip())
            if bname is None:
                raise RuntimeError(f"未找到楼栋「{building}」，可用楼栋：{'、'.join(buildings)}")
            data = await self._post_json(session,
                                         f"{self.base}/ecu/api/roomInfo/query/2",
                                         {"loudongId": buildings[bname]})
        meters = []
        for item in data:
            rname = str(item.get("room", "")).strip()
            m = re.match(r"^\d+-(\d+)(.*)$", rname)
            if not m or m.group(1) != room:
                continue
            suffix = m.group(2).strip()
            if meter_types and suffix and suffix not in meter_types:
                continue
            try:
                all_amp = float(item.get("allAmp") or 0)
                used_amp = float(item.get("usedAmp") or 0)
            except (TypeError, ValueError):
                continue
            meters.append({
                "loudong": str(item.get("loudong", bname)),
                "room": rname, "roomId": item.get("roomId"),
                "allAmp": all_amp, "usedAmp": used_amp,
                "remain": round(all_amp - used_amp, 2),
            })
        return meters


# ---------------------------------------------------------------------------
# AstrBot 插件主体
# ---------------------------------------------------------------------------
if filter is not None:
    @register("electric_query", "AstrBot User",
              "山文宿舍电费查询：电费查询/历史统计/断电预测/智能提醒（宿舍电量管家）", "4.6.0",
              "https://github.com/YourName/astrbot_plugin_electric_query")
    class ElectricQueryPlugin(Star):
        def __init__(self, context: Context, config: dict = None):
            super().__init__(context)
            # 配置来源：config.json 初始默认值；AstrBot WebUI（_conf_schema.json）优先。
            # WebUI 配置可能是 object 分组结构，这里平铺合并回扁平键。
            self.cfg = load_config()
            if config:
                try:
                    for k, v in dict(config).items():
                        if v is None:
                            continue
                        if isinstance(v, dict):
                            for kk, vv in v.items():
                                if vv is not None:
                                    self.cfg[kk] = vv
                        else:
                            self.cfg[k] = v
                except Exception as e:
                    logger.error(f"[electric_query] 读取 WebUI 配置失败: {e}")
            self.client = ElectricApiClient(
                base_url=self.cfg.get("query_url", ""),
                timeout=int(self.cfg.get("timeout", 15)),
                retries=int(self.cfg.get("retries", 2)),
            )
            self._cache = {}          # 电量缓存 key -> (时间戳, meters)
            self._sessions = set()    # 使用过指令的会话（用于定时提醒）
            self._alert_state = {}    # roomkey -> {"tier":1/2/3,"last_ts":...}
            self._night_sent = {}     # roomkey -> "2026-09-14"（夜间提醒已发送日期）
            self._scheduler = None
            try:
                database.init_db()
                database.prune_history(int(self.cfg.get("history_keep_days", 30)))
            except Exception as e:
                logger.error(f"[electric_query] 初始化数据库失败: {e}")
            self._load_data()
            self._start_scheduler()

        # ---------------- 持久化 ----------------
        def _load_data(self):
            # 旧版本 data.json 在插件目录，首次启动搬移到新目录
            legacy = os.path.join(PLUGIN_DIR, "data.json")
            if not os.path.exists(DATA_FILE) and os.path.exists(legacy):
                try:
                    shutil.copy2(legacy, DATA_FILE)
                except OSError:
                    pass
            try:
                if os.path.exists(DATA_FILE):
                    with open(DATA_FILE, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    self._sessions = set(d.get("sessions", []))
                    self._alert_state = d.get("alert_state", {})
                    self._night_sent = d.get("night_sent", {})
            except Exception as e:
                logger.error(f"[electric_query] 读取 data.json 失败: {e}")

        def _save_data(self):
            try:
                with open(DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump({"sessions": sorted(self._sessions),
                               "alert_state": self._alert_state,
                               "night_sent": self._night_sent},
                              f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"[electric_query] 写入 data.json 失败: {e}")

        # ---------------- 缓存与记录 ----------------
        def _cache_get(self, key):
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < int(self.cfg.get("cache_ttl", 300)):
                return hit[1]
            return None

        async def _fetch_meters(self, building: str, room: str) -> list:
            """查询房间电表（带缓存）；新鲜数据自动写入历史库。"""
            key = f"{building}:{room}"
            cached = self._cache_get(key)
            if cached is not None:
                return cached
            meter_types = self.cfg.get("meter_types") or None
            meters = await self.client.query_room(building, room, meter_types)
            if not meters:
                raise RuntimeError(f"查询结果为空：{building} 未找到 {room} 的电表"
                                   f"（请确认房间号，或检查 meter_types 配置）")
            self._cache[key] = (time.time(), meters)
            self._record_meters(building, room, meters)
            return meters

        def _record_meters(self, building: str, room: str, meters: list):
            """写入历史采样：room 存 '2号公寓319'，meter_type 存 '照明' 等。
            60 秒内已有同表采样则跳过（避免重载/连查产生近重复样本）。"""
            try:
                rkey = f"{building}{room}"
                now = datetime.now()
                for m in meters:
                    t = self._meter_type(m["room"])
                    latest = database.latest(rkey, t)
                    if latest:
                        try:
                            last_dt = datetime.strptime(latest[0], "%Y-%m-%d %H:%M:%S")
                            if (now - last_dt).total_seconds() < 60:
                                continue
                        except (ValueError, TypeError):
                            pass
                    database.record_power(rkey, t, m["remain"])
            except Exception as e:
                logger.error(f"[electric_query] 记录电量历史失败: {e}")

        def _history(self, building: str, room: str, meter_type: str) -> list:
            return database.query_history(f"{building}{room}", meter_type)

        @staticmethod
        def _meter_type(room_name: str) -> str:
            for t in METER_TYPES:
                if room_name.endswith(t):
                    return t
            return "其他"

        @staticmethod
        def _icon(meter_type: str) -> str:
            return METER_ICONS.get(meter_type, "⚡")

        # ---------------- 格式化 ----------------
        @staticmethod
        def _fmt_meters(meters: list) -> str:
            lines = []
            for m in meters:
                t = ElectricQueryPlugin._meter_type(m["room"])
                lines.append(f"{ElectricQueryPlugin._icon(t)} {m['room']}：{m['remain']:.2f} 度")
            return "\n".join(lines)

        def _recharge_hint(self) -> str:
            return (f"👉 充值入口：{self.cfg.get('recharge_url', '')}\n"
                    f"（微信打开后选楼栋→选房间→选金额，支付请手动确认）")

        # ---------------- 房间解析 ----------------
        @staticmethod
        def _parse_room_arg(arg: str):
            """'2-319' / '2:319' / '2号公寓-319' / '2号楼:319' -> (楼栋, 房间)。
            冒号写法用于 watch_rooms 配置，如 '5号公寓:206'。"""
            arg = arg.strip()
            m = re.match(r"^(\d+)[-－:：](\d+)$", arg)
            if m:
                return f"{m.group(1)}号公寓", m.group(2)
            m = re.match(r"^(\d+)[号栋]?(?:公寓|楼)[-－:：](\d+)$", arg)
            if m:
                return f"{m.group(1)}号公寓", m.group(2)
            return ("", arg if re.match(r"^\d+$", arg) else "")

        def _resolve_room(self, event):
            """从指令消息解析 (楼栋, 房间)，缺省为配置的宿舍。"""
            args = event.message_str.strip()
            for cmd in ("查询电费", "用电统计", "耗电统计", "预测停电",
                        "断电预测", "用电趋势", "耗电趋势", "电费", "电量"):
                if args.startswith(cmd):
                    args = args[len(cmd):].strip()
                    break
            building, room = self.cfg.get("building", ""), self.cfg.get("room", "")
            if args:
                b, r = self._parse_room_arg(args)
                building = b or building
                room = r or room
            return building, room

        def _remember(self, event):
            try:
                self._sessions.add(event.unified_msg_origin)
                self._save_data()
            except Exception:
                pass

        def _watch_list(self):
            rooms = []
            watch = self.cfg.get("watch_rooms") or []
            if isinstance(watch, str):
                watch = [watch]
            for item in watch:
                if isinstance(item, dict):
                    rooms.append((item.get("building", ""), str(item.get("room", ""))))
                else:
                    b, r = self._parse_room_arg(str(item))
                    rooms.append((b or self.cfg.get("building", ""), r))
            if self.cfg.get("room"):
                rooms.append((self.cfg.get("building", ""), str(self.cfg.get("room"))))
            seen, out = set(), []
            for b, r in rooms:
                if r and (b, r) not in seen:
                    seen.add((b, r))
                    out.append((b, r))
            return out

        # ---------------- 指令：电费查询 ----------------
        @filter.command("电费", alias={"查询电费", "电量"})
        async def query_cmd(self, event: AstrMessageEvent):
            """电费 [房间号]：查询宿舍剩余电量。房间号如 319 或 2-319。"""
            self._remember(event)
            building, room = self._resolve_room(event)
            if not room:
                yield event.plain_result(
                    "用法：电费 [房间号]\n例如：电费 319 或 电费 2-319\n"
                    "默认查询 config.json 中配置的宿舍。")
                return
            try:
                meters = await self._fetch_meters(building, room)
                now = datetime.now().strftime("%Y-%m-%d %H:%M")
                yield event.plain_result(
                    f"🏠 宿舍查询结果\n\n{self._fmt_meters(meters)}\n\n📅 查询时间：{now}")
            except ValueError as e:
                yield event.plain_result(f"❌ {e}")
            except Exception as e:
                logger.error(f"[electric_query] 查询失败: {e}")
                yield event.plain_result(f"❌ 查询失败：{e}")

        # ---------------- 指令：用电统计 ----------------
        @filter.command("用电统计", alias={"耗电统计"})
        async def stats_cmd(self, event: AstrMessageEvent):
            """用电统计 [房间号]：24h 消耗、7 天日均、最高耗电时段。"""
            self._remember(event)
            building, room = self._resolve_room(event)
            if not room:
                yield event.plain_result("用法：用电统计 [房间号]，如 用电统计 2-319")
                return
            try:
                meters = await self._fetch_meters(building, room)
                lines_24 = []
                has_24 = False
                seven_days = []
                peak = None
                for m in meters:
                    t = self._meter_type(m["room"])
                    hist = self._history(building, room, t)
                    c24 = stats.recent_24h(hist)
                    if c24 is not None:
                        has_24 = True
                        lines_24.append(f"{self._icon(t)} {t}：消耗 {c24:.2f} 度")
                    avg = stats.avg_daily_consumption(hist, 7)
                    if avg is not None:
                        seven_days.append(avg)
                    pk = stats.peak_window(hist, 7)
                    if pk and (peak is None or pk[2] > peak[2]):
                        peak = pk
                if not has_24:
                    lines_24 = ["（历史数据不足，请稍后再试）"]
                if seven_days:
                    avg_all = sum(seven_days) / len(seven_days)
                    avg_line = f"平均每天消耗：{avg_all:.2f} 度"
                else:
                    avg_line = "平均每天消耗：数据不足"
                if peak:
                    peak_line = f"最高耗电时间段：{peak[0]} - {peak[1]}（{peak[2]:.2f} 度）"
                else:
                    peak_line = "最高耗电时间段：数据不足"
                # 分时画像与近 7 天每日消耗（房间内各表聚合）
                dp_day = dp_night = 0.0
                dp_has = False
                daily = {}
                for m in meters:
                    t = self._meter_type(m["room"])
                    hist = self._history(building, room, t)
                    dpart = stats.day_partition(hist, 7)
                    if dpart["day_pct"] is not None:
                        dp_has = True
                        dp_day += dpart["day"]
                        dp_night += dpart["night"]
                    for d, c in stats.daily_consumptions(hist, 7):
                        if c is not None:
                            daily[d] = daily.get(d, 0.0) + c
                extra_lines = []
                if dp_has:
                    total_dp = dp_day + dp_night
                    d_pct = dp_day / total_dp * 100 if total_dp > 0 else 0
                    n_pct = dp_night / total_dp * 100 if total_dp > 0 else 0
                    extra_lines.append(
                        f"分时段画像（近7天）：🌞 白天8-22点 {dp_day:.1f} 度 ({d_pct:.0f}%) / "
                        f"🌙 夜间22-8点 {dp_night:.1f} 度 ({n_pct:.0f}%)")
                if daily:
                    extra_lines.append("近7天每日消耗：" + "  ".join(
                        f"{d} {c:.1f}" for d, c in sorted(daily.items())))
                text = (
                    f"📊 用电统计（{building} {room}）\n\n"
                    "最近24小时：\n" + "\n".join(lines_24) + "\n\n"
                    "最近7天：\n" + avg_line + "\n\n" + peak_line
                )
                if extra_lines:
                    text += "\n\n" + "\n".join(extra_lines)
                yield event.plain_result(text)
            except Exception as e:
                logger.error(f"[electric_query] 用电统计失败: {e}")
                yield event.plain_result(f"❌ 统计失败：{e}")

        # ---------------- 指令：断电预测 ----------------
        @filter.command("预测停电", alias={"断电预测"})
        async def forecast_cmd(self, event: AstrMessageEvent):
            """预测停电 [房间号]：按历史耗电速度预测断电时间。"""
            self._remember(event)
            building, room = self._resolve_room(event)
            if not room:
                yield event.plain_result("用法：预测停电 [房间号]，如 预测停电 2-319")
                return
            try:
                meters = await self._fetch_meters(building, room)
                blocks = []
                any_ok = False
                for m in meters:
                    t = self._meter_type(m["room"])
                    hist = self._history(building, room, t)
                    pred = forecast_mod.predict(hist, remaining=m["remain"])
                    icon = self._icon(t)
                    if pred["hours"] is not None:
                        any_ok = True
                        block = (
                            f"{icon} {t}：剩余 {m['remain']:.2f} 度\n"
                            f"每小时消耗 {pred['rate']:.2f} 度\n"
                            f"预计还能使用 {pred['hours']:.1f} 小时\n"
                            f"预计断电时间：{pred['outage_iso'][:16]}")
                        if pred.get("method") == "profile":
                            extras = []
                            h24 = pred.get("hours24")
                            if h24 is not None:
                                extras.append(
                                    f"未来24小时预计消耗：{h24:.2f} 度"
                                    f"（分时预测，保守系数 {forecast_mod.CONSERVATIVE_FACTOR}）")
                            peaks = pred.get("peaks") or []
                            if peaks:
                                extras.append(
                                    "未来24小时高峰时段：" +
                                    "、".join(f"{a}（约 {r:.2f} 度/时）"
                                              for a, r in peaks))
                            pd = pred.get("profile_days")
                            if pd is not None and pd < 7:
                                extras.append(
                                    f"分时数据积累中（覆盖 {pd} 天，数据越多越准）")
                            if extras:
                                block += "\n" + "\n".join(extras)
                        blocks.append(block)
                    else:
                        blocks.append(f"{icon} {t}：{pred['reason']}")
                head = f"⚡ 电量预测（{building} {room}）"
                if not any_ok:
                    head += "\n\n📉 历史数据不足，暂时无法预测。\n" \
                            "请多查询几次「电费」（建议间隔数小时），" \
                            "或等待定时检查积累数据。"
                yield event.plain_result(head + "\n\n" + "\n\n".join(blocks))
            except Exception as e:
                logger.error(f"[electric_query] 断电预测失败: {e}")
                yield event.plain_result(f"❌ 预测失败：{e}")

        # ---------------- 指令：用电趋势 ----------------
        @filter.command("用电趋势", alias={"耗电趋势"})
        async def trend_cmd(self, event: AstrMessageEvent):
            """用电趋势 [房间号]：今日 vs 昨日消耗与变化。"""
            self._remember(event)
            building, room = self._resolve_room(event)
            if not room:
                yield event.plain_result("用法：用电趋势 [房间号]，如 用电趋势 2-319")
                return
            try:
                meters = await self._fetch_meters(building, room)
                lines = []
                t_total = 0.0
                y_total = 0.0
                has = False
                for m in meters:
                    t = self._meter_type(m["room"])
                    hist = self._history(building, room, t)
                    today, yest = stats.same_period_today_vs_yesterday(hist)
                    if today is None and yest is None:
                        lines.append(f"{self._icon(t)} {t}：数据不足")
                        continue
                    has = True
                    today = today or 0.0
                    yest = yest or 0.0
                    t_total += today
                    y_total += yest
                    if yest > 0:
                        chg = (today - yest) / yest * 100
                        y_text = f"昨日同期 {yest:.2f} 度 / 变化 {chg:+.1f}%"
                    else:
                        y_text = "昨日同期无数据"
                    lines.append(f"{self._icon(t)} {t}：今日 {today:.2f} 度（截至此刻）"
                                 f" / {y_text}")
                if has and y_total > 0:
                    chg = (t_total - y_total) / y_total * 100
                    lines.append(f"合计：今日 {t_total:.2f} 度 / 昨日同期 {y_total:.2f} 度"
                                 f" / 变化 {chg:+.1f}%")
                yield event.plain_result(
                    f"📈 用电趋势（{building} {room}，同期对比）\n\n" + "\n".join(lines))
            except Exception as e:
                logger.error(f"[electric_query] 用电趋势失败: {e}")
                yield event.plain_result(f"❌ 趋势查询失败：{e}")

        # ---------------- 指令：帮助 ----------------
        @filter.command("电费帮助")
        async def help_cmd(self, event: AstrMessageEvent):
            try:
                check_h = float(self.cfg.get("check_interval_hours", 6))
                alert_h = float(self.cfg.get("alert_hours", 12))
                urgent_h = float(self.cfg.get("urgent_hours", 3))
                night_t = str(self.cfg.get("night_check_time", "22:00"))
            except (TypeError, ValueError):
                check_h, alert_h, urgent_h, night_t = 6, 12, 3, "22:00"
            yield event.plain_result(
                "📖 山文宿舍电费查询（宿舍电量管家）\n"
                "· 电费 [房间号]：查剩余电量（如 电费 2-319）\n"
                "· 用电统计 [房间号]：24h 消耗 / 7 天日均 / 最高耗电时段\n"
                "· 预测停电 [房间号]：预计断电时间\n"
                "· 用电趋势 [房间号]：今日 vs 昨日\n"
                f"· 自动监控：每 {check_h:g} 小时，"
                f"预计断电 <{alert_h:g}h 强提醒、"
                f"<{urgent_h:g}h 紧急提醒\n"
                f"· 夜间保护：每天 {night_t} 检查"
            )

        # ---------------- 定时监控：预测式分级提醒 ----------------
        def _start_scheduler(self):
            if not self.cfg.get("check_enabled", True):
                return
            try:
                from apscheduler.schedulers.asyncio import AsyncIOScheduler
            except ImportError:
                logger.warning("[electric_query] 未安装 apscheduler，定时监控不可用"
                               "（pip install apscheduler）")
                return
            hours = float(self.cfg.get("check_interval_hours", 6))
            if hours <= 0:
                logger.error("[electric_query] check_interval_hours 必须大于 0")
                return
            self._scheduler = AsyncIOScheduler()
            self._scheduler.add_job(self._scheduled_check, "interval", hours=hours,
                                    id="electric_monitor", max_instances=1,
                                    coalesce=True, misfire_grace_time=600)
            if self.cfg.get("night_check_enabled", True):
                m = re.match(r"^(\d{1,2}):(\d{2})$",
                             str(self.cfg.get("night_check_time", "22:00")))
                if m:
                    self._scheduler.add_job(self._night_check, "cron",
                                            hour=int(m.group(1)), minute=int(m.group(2)),
                                            id="electric_night", max_instances=1,
                                            coalesce=True, misfire_grace_time=1800)
            self._scheduler.start()
            logger.info(f"[electric_query] 电量监控已启动：每 {hours:g} 小时检查一次")

        async def _room_prediction(self, building: str, room: str) -> dict:
            """对房间各表做断电预测。
            返回 {"hours": 全房最早断电小时数 or None,
                  "outage": 对应断电时间 ISO,
                  "meters": [{"meter","type","hours","outage","reason",
                              "method","hours24","rate","peaks","profile_days"}]}
            """
            meters = await self._fetch_meters(building, room)
            per = []
            room_hours = None
            room_outage = None
            for m in meters:
                t = self._meter_type(m["room"])
                hist = self._history(building, room, t)
                pred = forecast_mod.predict(hist, remaining=m["remain"])
                per.append({"meter": m, "type": t, "hours": pred["hours"],
                            "outage": pred["outage_iso"], "reason": pred["reason"],
                            "method": pred["method"], "hours24": pred["hours24"],
                            "rate": pred["rate"], "peaks": pred.get("peaks", []),
                            "profile_days": pred.get("profile_days")})
                if pred["hours"] is not None:
                    if room_hours is None or pred["hours"] < room_hours:
                        room_hours = pred["hours"]
                        room_outage = pred["outage_iso"]
            return {"hours": room_hours, "outage": room_outage, "meters": per}

        def _should_notify(self, key: str, tier: int) -> bool:
            """分级防打扰：级别上升立即提醒；同级别按重复间隔提醒。"""
            if tier <= 0:
                self._alert_state.pop(key, None)
                return False
            now = time.time()
            prev = self._alert_state.get(key)
            if not prev:
                self._alert_state[key] = {"tier": tier, "last_ts": now}
                return True
            if tier > prev.get("tier", 0):
                self._alert_state[key] = {"tier": tier, "last_ts": now}
                return True
            repeat = (float(self.cfg.get("alert_repeat_hours", 6)) if tier >= 2
                      else float(self.cfg.get("warn_repeat_hours", 24))) * 3600
            if now - prev.get("last_ts", 0) >= repeat:
                self._alert_state[key] = {"tier": tier, "last_ts": now}
                return True
            return False

        @staticmethod
        def _outage_desc(outage_iso: str) -> str:
            """'2026-09-15 03:00' -> '今晚 3 点左右' / '明天 15:30 左右'"""
            try:
                dt = datetime.strptime(outage_iso[:16], "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                return "近期"
            now = datetime.now()
            day = (dt - now).days
            if dt.minute == 0:
                hm = f"{dt.hour} 点"
            else:
                hm = f"{dt.hour}:{dt.minute:02d}"
            if day <= 0:
                return f"今晚 {hm} 左右"
            if day == 1:
                return f"明天 {hm} 左右"
            return f"{dt.month}月{dt.day}日 {hm} 左右"

        async def _scheduled_check(self):
            targets = list(self.cfg.get("notify_sessions") or []) or list(self._sessions)
            if not targets:
                return
            warn_h = float(self.cfg.get("warn_hours", 24))
            alert_h = float(self.cfg.get("alert_hours", 12))
            urgent_h = float(self.cfg.get("urgent_hours", 3))
            warn_deg = float(self.cfg.get("warn_threshold", 20))
            alert_deg = float(self.cfg.get("alert_threshold", 10))
            hour_en = {1: self.cfg.get("enable_hour_warn", True),
                       2: self.cfg.get("enable_hour_alert", True),
                       3: self.cfg.get("enable_hour_urgent", True)}
            deg_en = {1: self.cfg.get("enable_degree_warn", True),
                      2: self.cfg.get("enable_degree_alert", True)}
            for building, room in self._watch_list():
                key = f"{building}:{room}"
                try:
                    pred = await self._room_prediction(building, room)
                    meters_raw = [p["meter"] for p in pred["meters"]]
                except Exception as e:
                    logger.error(f"[electric_query] 定时查询失败 {key}: {e}")
                    continue

                # —— 通道1：预测式（按预计断电剩余小时）——
                hours = pred["hours"]
                raw_h = 0
                if hours is not None:
                    raw_h = (3 if hours <= urgent_h
                             else 2 if hours <= alert_h
                             else 1 if hours <= warn_h else 0)
                h_tier = 0
                for lv in (3, 2, 1):
                    if raw_h >= lv and hour_en.get(lv, True):
                        h_tier = lv
                        break

                # —— 通道2：固定阈值（按剩余度数）——
                remain = min(m["remain"] for m in meters_raw)
                raw_d = 2 if remain < alert_deg else (1 if remain < warn_deg else 0)
                d_tier = 0
                for lv in (2, 1):
                    if raw_d >= lv and deg_en.get(lv, True):
                        d_tier = lv
                        break

                # 取更高一级；同级别优先用预测式文案（更精确）
                tier = max(h_tier, d_tier)
                mode = "hours" if h_tier >= d_tier else "degrees"

                if not self._should_notify(key, tier):
                    continue
                self._save_data()
                now = datetime.now().strftime("%Y-%m-%d %H:%M")
                if tier == 3:
                    head = f"🔥 紧急提醒：预计约 {hours:.1f} 小时后断电"
                elif tier == 2:
                    if mode == "hours":
                        head = f"🚨 即将断电：预计{self._outage_desc(pred['outage'])}"
                    else:
                        head = f"🚨 电量严重不足：剩余 {remain:.2f} 度（低于 {alert_deg:g} 度）"
                elif tier == 1:
                    if mode == "hours":
                        head = f"⚠️ 电量提醒：预计还有 {hours:.0f} 小时"
                    else:
                        head = f"⚠️ 电量偏低：剩余 {remain:.2f} 度（低于 {warn_deg:g} 度）"
                else:
                    continue  # 所有提醒级别均已关闭
                text = (f"{head}\n\n"
                        f"🏠 {building} {room}\n"
                        f"{self._fmt_meters(meters_raw)}\n\n"
                        f"{self._recharge_hint()}\n"
                        f"📅 {now}")
                for sess in targets:
                    try:
                        await self.context.send_message(sess, self._plain(text))
                    except Exception as e:
                        logger.error(f"[electric_query] 推送提醒失败 {sess}: {e}")
                await asyncio.sleep(2)

        # ---------------- 夜间断电保护 ----------------
        async def _night_check(self):
            targets = list(self.cfg.get("notify_sessions") or []) or list(self._sessions)
            if not targets:
                return
            today = datetime.now().strftime("%Y-%m-%d")
            warn_deg = float(self.cfg.get("warn_threshold", 20))
            for building, room in self._watch_list():
                key = f"{building}:{room}"
                nkey = f"night:{key}"
                if self._night_sent.get(nkey) == today:
                    continue
                try:
                    pred = await self._room_prediction(building, room)
                except Exception as e:
                    logger.error(f"[electric_query] 夜间检查失败 {key}: {e}")
                    continue
                if pred["hours"] is None:
                    continue
                # 今夜窗口：下一个 00:00 - 08:00
                now = datetime.now()
                midnight = (now + timedelta(days=1)).replace(hour=0, minute=0,
                                                             second=0, microsecond=0)
                window_end = midnight + timedelta(hours=8)
                # 找出「今夜有断电风险」且当前电量偏低的表
                risk = None
                for p in pred["meters"]:
                    m = p["meter"]
                    if m["remain"] >= warn_deg:
                        continue
                    hist = self._history(building, room, p["type"])
                    prof = forecast_mod.build_hour_profile(hist)
                    if prof:
                        # 从今夜 00:00 起按分时曲线模拟：断电须落在今夜窗口内才算风险
                        hours, outage = forecast_mod.sim_deplete(prof, m["remain"],
                                                                 now=midnight)
                        in_window = False
                        if outage:
                            try:
                                out_dt = datetime.strptime(outage[:16], "%Y-%m-%d %H:%M")
                                in_window = midnight <= out_dt < window_end
                            except (ValueError, TypeError):
                                in_window = False
                        night_usage = forecast_mod.usage_between(prof, midnight,
                                                                 window_end)
                        if in_window or (night_usage > m["remain"]):
                            p["night_usage"] = night_usage
                            p["night_outage"] = outage if in_window else None
                            if risk is None or p["hours"] < risk["hours"]:
                                risk = p
                    else:
                        # Profile 不可用：回退到「预计断电时刻在今晚窗口内」的判定
                        if not p["outage"]:
                            continue
                        try:
                            out_dt = datetime.strptime(p["outage"][:16],
                                                       "%Y-%m-%d %H:%M")
                        except (ValueError, TypeError):
                            continue
                        if midnight <= out_dt < window_end:
                            p["night_usage"] = None
                            p["night_outage"] = p["outage"]
                            if risk is None or p["hours"] < risk["hours"]:
                                risk = p
                if not risk:
                    continue
                self._night_sent[nkey] = today
                self._save_data()
                m = risk["meter"]
                if risk.get("night_outage"):
                    line = f"预计：凌晨 {risk['night_outage'][11:16]} 断电"
                elif risk.get("night_usage") is not None:
                    line = (f"预计今夜（0-8点）耗电约 {risk['night_usage']:.2f} 度，"
                            f"已超过当前剩余")
                else:
                    line = f"预计：凌晨 {risk['outage'][11:16]} 断电"
                text = (f"🌙 夜间断电风险\n\n"
                        f"{line}\n"
                        f"当前剩余：{self._icon(risk['type'])} {m['room']} "
                        f"{m['remain']:.2f} 度\n\n"
                        f"建议提前充值。\n{self._recharge_hint()}")
                for sess in targets:
                    try:
                        await self.context.send_message(sess, self._plain(text))
                    except Exception as e:
                        logger.error(f"[electric_query] 夜间提醒推送失败 {sess}: {e}")
                await asyncio.sleep(2)

        @staticmethod
        def _plain(text: str):
            from astrbot.api.message_components import Plain
            return [Plain(text)]

        async def terminate(self):
            if self._scheduler:
                try:
                    self._scheduler.shutdown(wait=False)
                except Exception:
                    pass
            logger.info("[electric_query] 插件已卸载")
else:
    logger.warning("[electric_query] 未识别到 AstrBot filter 模块，插件功能不可用。"
                   "请升级 AstrBot 到 v3.4+ 后重新加载插件。")
