# -*- coding: utf-8 -*-
"""
山文宿舍电费查询 AstrBot 插件
=============================
面向山东文化产业职业学院（山文）学生的宿舍电费/剩余电量查询与低电量提醒。

数据来源：山东文化产业职业学院校园一卡通 (card.sdcivc.edu.cn)

=== 接口（公开，无需登录/token，2026-09-14 实测）===
  网关基址: https://card.sdcivc.edu.cn/bc/gateway
  楼栋列表: POST {base}/ecu/api/roomInfo/query/1   body: {"loudongId": null}
  房间列表: POST {base}/ecu/api/roomInfo/query/2   body: {"loudongId": <楼栋id>}
  房间条目: {loudong, loudongId, room("2-319照明"), roomId, allAmp, usedAmp}
  剩余电量 = allAmp - usedAmp（度）

=== 功能 ===
  · 电费/查询电费/电量 [房间号]  查询剩余电量（如：电费 2-319）
  · 电费帮助                      用法说明
  · 定时检查：每 N 小时自动查询，剩余 <20 度普通提醒，<10 度强提醒，
    提醒附带充值入口提示（支付由用户在微信手动完成，插件不参与）
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime

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

logger = logging.getLogger("electric_query")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(PLUGIN_DIR, "data.json")

DEFAULT_CONFIG = {
    # ---- 默认宿舍 ----
    "building": "2号公寓",
    "room": "319",
    # 展示的电表类型：照明/空调/水表；[] = 全部显示
    "meter_types": ["照明", "空调"],
    # ---- 网络 ----
    "query_url": "https://card.sdcivc.edu.cn/bc/gateway",
    "cache_ttl": 300,      # 查询结果缓存秒数
    "timeout": 15,
    "retries": 2,
    # ---- 定时监控 ----
    "check_enabled": True,
    "check_interval_hours": 6,   # 自动检查间隔（小时），24 = 每天一次
    "warn_threshold": 20.0,      # < 20 度：普通提醒
    "alert_threshold": 10.0,     # < 10 度：强提醒
    "warn_repeat_hours": 24,     # 普通提醒同级别重复间隔
    "alert_repeat_hours": 6,     # 强提醒同级别重复间隔
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
    @register("electric_query", "AstrBot User", "山文宿舍电费查询：宿舍电费/剩余电量查询与低电量定时提醒（山东文化产业职业学院）", "3.3.0",
              "https://github.com/YourName/astrbot_plugin_electric_query")
    class ElectricQueryPlugin(Star):
        def __init__(self, context: Context, config: dict = None):
            super().__init__(context)
            # 配置来源：config.json 提供初始默认值；AstrBot 通过 _conf_schema.json
            # 把 WebUI 中修改的配置以 config 参数传入，覆盖默认值（WebUI 优先）
            self.cfg = load_config()
            if config:
                try:
                    for k, v in dict(config).items():
                        if v is not None:
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
            self._alert_state = {}    # roomkey -> {"tier":0/1/2,"last_ts":...}
            self._scheduler = None
            self._load_data()
            self._start_scheduler()

        # ---------------- 持久化 ----------------
        def _load_data(self):
            try:
                if os.path.exists(DATA_FILE):
                    with open(DATA_FILE, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    self._sessions = set(d.get("sessions", []))
                    self._alert_state = d.get("alert_state", {})
            except Exception as e:
                logger.error(f"[electric_query] 读取 data.json 失败: {e}")

        def _save_data(self):
            try:
                with open(DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump({"sessions": sorted(self._sessions),
                               "alert_state": self._alert_state},
                              f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"[electric_query] 写入 data.json 失败: {e}")

        # ---------------- 缓存 ----------------
        def _cache_get(self, key):
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < int(self.cfg.get("cache_ttl", 300)):
                return hit[1]
            return None

        async def _fetch_meters(self, building: str, room: str) -> list:
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
            return meters

        # ---------------- 格式化 ----------------
        @staticmethod
        def _fmt_meters(meters: list) -> str:
            lines = []
            for m in meters:
                suffix = ""
                for t in ("照明", "空调", "水表"):
                    if m["room"].endswith(t):
                        suffix = t
                        break
                icon = {"照明": "💡", "空调": "❄️", "水表": "🚰"}.get(suffix, "⚡")
                lines.append(f"{icon} {m['room']}：{m['remain']:.2f} 度")
            return "\n".join(lines)

        def _recharge_hint(self) -> str:
            return (f"👉 充值入口：{self.cfg.get('recharge_url', '')}\n"
                    f"（微信打开后选楼栋→选房间→选金额，支付请手动确认）")

        # ---------------- 指令：电费查询 ----------------
        @filter.command("电费", alias={"查询电费", "电量"})
        async def query_cmd(self, event: AstrMessageEvent):
            """电费 [房间号]：查询宿舍剩余电量。房间号如 319 或 2-319。"""
            self._remember(event)
            args = event.message_str.strip()
            for cmd in ("查询电费", "电费", "电量"):
                if args.startswith(cmd):
                    args = args[len(cmd):].strip()
                    break
            building, room = self.cfg.get("building", ""), self.cfg.get("room", "")
            if args:
                b, r = self._parse_room_arg(args)
                building = b or building
                room = r or room
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

        @filter.command("电费帮助")
        async def help_cmd(self, event: AstrMessageEvent):
            yield event.plain_result(
                "📖 电费查询插件\n"
                "· 电费 / 查询电费 / 电量：查询默认宿舍剩余电量\n"
                "· 电费 319：按配置楼栋查询 319 房间\n"
                "· 电费 2-319：查询 2号公寓 319 房间\n"
                f"· 自动检查：每 {self.cfg.get('check_interval_hours', 6)} 小时，"
                f"剩余<{self.cfg.get('warn_threshold', 20):g}度普通提醒，"
                f"<{self.cfg.get('alert_threshold', 10):g}度强提醒"
            )

        # ---------------- 定时监控：分级提醒 ----------------
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
            self._scheduler.start()
            logger.info(f"[electric_query] 电量监控已启动：每 {hours:g} 小时检查一次")

        def _remember(self, event):
            try:
                self._sessions.add(event.unified_msg_origin)
                self._save_data()
            except Exception:
                pass

        @staticmethod
        def _parse_room_arg(arg: str):
            """'2-319' / '319' / '2号公寓-319' -> (楼栋, 房间)。"""
            arg = arg.strip()
            m = re.match(r"^(\d+)[-－](\d+)$", arg)
            if m:
                return f"{m.group(1)}号公寓", m.group(2)
            m = re.match(r"^(\d+)号?公寓[-－](\d+)$", arg)
            if m:
                return f"{m.group(1)}号公寓", m.group(2)
            return ("", arg if re.match(r"^\d+$", arg) else "")

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

        async def _scheduled_check(self):
            targets = list(self.cfg.get("notify_sessions") or []) or list(self._sessions)
            if not targets:
                return
            warn = float(self.cfg.get("warn_threshold", 20))
            alert = float(self.cfg.get("alert_threshold", 10))
            for building, room in self._watch_list():
                key = f"{building}:{room}"
                try:
                    meters = await self._fetch_meters(building, room)
                except Exception as e:
                    logger.error(f"[electric_query] 定时查询失败 {key}: {e}")
                    continue
                remain = min(m["remain"] for m in meters)
                tier = 2 if remain < alert else (1 if remain < warn else 0)
                if not self._should_notify(key, tier):
                    continue
                self._save_data()
                now = datetime.now().strftime("%Y-%m-%d %H:%M")
                if tier >= 2:
                    head = "🚨 电量严重不足，请立即充值！"
                    extra = f"剩余仅 {remain:.2f} 度（低于 {alert:g} 度强提醒线）"
                else:
                    head = "⚠️ 电量偏低提醒"
                    extra = f"剩余 {remain:.2f} 度（低于 {warn:g} 度提醒线）"
                text = (f"{head}\n\n"
                        f"🏠 {building} {room}\n"
                        f"{self._fmt_meters(meters)}\n"
                        f"{extra}\n\n"
                        f"{self._recharge_hint()}\n"
                        f"📅 {now}")
                for sess in targets:
                    try:
                        await self.context.send_message(sess, self._plain(text))
                    except Exception as e:
                        logger.error(f"[electric_query] 推送提醒失败 {sess}: {e}")
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
