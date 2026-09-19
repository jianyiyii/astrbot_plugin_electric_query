# -*- coding: utf-8 -*-
"""
SQLite 历史电量数据存储（山文宿舍电费查询）

库文件: electric_history.db（插件目录下）
表: power_history(id, room, meter_type, remaining_power, created_at)
"""

import os
import shutil
import sqlite3
import threading
from datetime import datetime

# 插件目录：旧版本持久化数据的存放处，用于升级时迁移
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

_lock = threading.Lock()
_DATA_DIR = None


def get_data_dir() -> str:
    """AstrBot 约定持久化目录 data/plugin_data/electric_query/。
    独立运行 / 识别不到 AstrBot 环境时回落到插件目录。"""
    global _DATA_DIR
    if _DATA_DIR is None:
        try:
            from astrbot.core import paths
            root = getattr(paths, "ASTRBOT_DATA_DIR", None)
        except Exception:
            root = None
        _DATA_DIR = (os.path.join(str(root), "plugin_data", "electric_query")
                     if root else _PLUGIN_DIR)
        os.makedirs(_DATA_DIR, exist_ok=True)
    return _DATA_DIR


DB_FILE = os.path.join(get_data_dir(), "electric_history.db")


def _migrate_legacy_db() -> None:
    """v4.5.0 起数据库移到 AstrBot 约定目录；插件目录里的旧库首次启动时自动搬移。"""
    src = os.path.join(_PLUGIN_DIR, "electric_history.db")
    if not os.path.exists(src) or os.path.exists(DB_FILE):
        return
    for suffix in ("", "-wal", "-shm"):
        s = src + suffix
        if os.path.exists(s):
            try:
                shutil.copy2(s, DB_FILE + suffix)
            except OSError:
                pass


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def init_db() -> None:
    """建表 + 索引。插件启动时调用一次。"""
    with _lock:
        _migrate_legacy_db()
        conn = _connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS power_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room TEXT NOT NULL,
                    meter_type TEXT NOT NULL,
                    remaining_power REAL NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ph_room_time "
                "ON power_history(room, meter_type, created_at)"
            )
            conn.commit()
        finally:
            conn.close()


def record_power(room: str, meter_type: str, remaining_power: float,
                 created_at: str = "") -> None:
    """写入一条电量采样。created_at 缺省为当前时间（ISO 字符串）。"""
    if not created_at:
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO power_history(room, meter_type, remaining_power, created_at) "
                "VALUES (?, ?, ?, ?)",
                (room, meter_type, float(remaining_power), created_at),
            )
            conn.commit()
        finally:
            conn.close()


def query_history(room: str, meter_type: str = "",
                  start: str = "", end: str = "") -> list:
    """
    查询历史采样，按时间升序。
    返回: [(created_at, remaining_power), ...]
    start/end 为 ISO 字符串（含时即含）。
    """
    sql = "SELECT created_at, remaining_power FROM power_history WHERE room = ?"
    params = [room]
    if meter_type:
        sql += " AND meter_type = ?"
        params.append(meter_type)
    if start:
        sql += " AND created_at >= ?"
        params.append(start)
    if end:
        sql += " AND created_at <= ?"
        params.append(end)
    sql += " ORDER BY created_at ASC"
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    return [(r[0], float(r[1])) for r in rows]


def latest(room: str, meter_type: str):
    """返回该表最近一条采样 (created_at, remaining_power) 或 None。"""
    sql = "SELECT created_at, remaining_power FROM power_history WHERE room = ?"
    params = [room]
    if meter_type:
        sql += " AND meter_type = ?"
        params.append(meter_type)
    sql += " ORDER BY created_at DESC LIMIT 1"
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
    return (row[0], float(row[1])) if row else None


def prune_history(keep_days: int = 30) -> None:
    """删除超过 keep_days 天的旧记录，控制库体积。"""
    if keep_days <= 0:
        return
    cutoff = datetime.fromtimestamp(
        datetime.now().timestamp() - keep_days * 86400
    ).strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM power_history WHERE created_at < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()
