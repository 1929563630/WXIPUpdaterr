"""
SQLite 数据库操作
"""
import sqlite3
import os
import json
from datetime import datetime
from config import DB_FILE


def get_conn():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表"""
    conn = get_conn()
    cursor = conn.cursor()

    # 应用配置表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS apps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT UNIQUE NOT NULL,
            app_name TEXT DEFAULT '',
            current_ip TEXT DEFAULT '',
            last_update TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    # IP变更历史表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ip_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            old_ip TEXT,
            new_ip TEXT,
            changed_at TEXT DEFAULT (datetime('now','localtime')),
            update_result TEXT DEFAULT ''
        )
    """)

    # 操作日志表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT DEFAULT 'INFO',
            message TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    # 当前IP记录
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS current_ip (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            ip TEXT DEFAULT '0.0.0.0',
            last_check TEXT DEFAULT (datetime('now','localtime')),
            last_change TEXT DEFAULT ''
        )
    """)

    # 兼容旧库：检查 last_change 列是否存在，不存在就 ALTER 加列
    try:
        cols = [row["name"] for row in cursor.execute("PRAGMA table_info(current_ip)").fetchall()]
        if "last_change" not in cols:
            cursor.execute("ALTER TABLE current_ip ADD COLUMN last_change TEXT DEFAULT ''")
    except Exception:
        pass

    # 迁移：如果 last_change 为空，从 ip_history 取最近一次变更时间填充
    try:
        row = cursor.execute(
            "SELECT last_change, last_check FROM current_ip WHERE id = 1"
        ).fetchone()
        if row is not None:
            lc = row["last_change"] if "last_change" in row.keys() else ""
            if not lc:
                hist = cursor.execute(
                    "SELECT changed_at FROM ip_history ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if hist and hist["changed_at"]:
                    cursor.execute(
                        "UPDATE current_ip SET last_change = ? WHERE id = 1",
                        (hist["changed_at"],),
                    )
                else:
                    cursor.execute(
                        "UPDATE current_ip SET last_change = last_check WHERE id = 1"
                    )
    except Exception:
        pass

    conn.commit()
    conn.close()


def add_log(level: str, message: str):
    """添加日志，自动裁剪超过1000条的旧日志"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO logs (level, message) VALUES (?, ?)",
        (level, message)
    )
    cursor = conn.cursor()
    cursor.execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 1000)")
    conn.commit()
    conn.close()


def get_logs(limit: int = 100) -> list:
    """获取最近日志"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_current_ip() -> dict:
    """获取当前记录的IP"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM current_ip WHERE id = 1").fetchone()
    conn.close()
    if row:
        result = dict(row)
        # 兜底字段，兼容旧库
        result.setdefault("last_change", "")
        return result
    return {"ip": "0.0.0.0", "last_check": "", "last_change": ""}


def update_current_ip(ip: str, ip_changed: bool = False):
    """
    更新当前IP。

    参数：
    - ip: 新的公网IP
    - ip_changed: 本次 IP 是否真的发生了变化
        - True  → 同时刷新 last_change
        - False → 只刷新 last_check
    """
    conn = get_conn()
    if ip_changed:
        conn.execute("""
            INSERT INTO current_ip (id, ip, last_check, last_change)
            VALUES (1, ?, datetime('now','localtime'), datetime('now','localtime'))
            ON CONFLICT(id) DO UPDATE SET
                ip = excluded.ip,
                last_check = datetime('now','localtime'),
                last_change = datetime('now','localtime')
        """, (ip,))
    else:
        conn.execute("""
            INSERT INTO current_ip (id, ip, last_check, last_change)
            VALUES (1, ?, datetime('now','localtime'), '')
            ON CONFLICT(id) DO UPDATE SET
                ip = excluded.ip,
                last_check = datetime('now','localtime')
        """, (ip,))
    conn.commit()
    conn.close()


def add_ip_history(old_ip: str, new_ip: str, result: str = ""):
    """记录IP变更历史"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO ip_history (old_ip, new_ip, update_result) VALUES (?, ?, ?)",
        (old_ip, new_ip, result)
    )
    conn.commit()
    conn.close()


def get_ip_history(limit: int = 20) -> list:
    """获取IP变更历史"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM ip_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_app(agent_id: str, app_name: str = "", current_ip: str = ""):
    """添加或更新应用"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO apps (agent_id, app_name, current_ip) VALUES (?, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
            app_name = CASE WHEN excluded.app_name != '' THEN excluded.app_name ELSE apps.app_name END,
            current_ip = CASE WHEN excluded.current_ip != '' THEN excluded.current_ip ELSE apps.current_ip END
    """, (agent_id, app_name, current_ip))
    conn.commit()
    conn.close()


def update_app_ip(agent_id: str, ip: str, status: str = "success"):
    """更新应用的可信IP"""
    conn = get_conn()
    conn.execute("""
        UPDATE apps SET current_ip = ?, last_update = datetime('now','localtime'), status = ?
        WHERE agent_id = ?
    """, (ip, status, agent_id))
    conn.commit()
    conn.close()


def get_apps() -> list:
    """获取所有应用"""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM apps ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_app(agent_id: str):
    """删除应用"""
    conn = get_conn()
    conn.execute("DELETE FROM apps WHERE agent_id = ?", (agent_id,))
    conn.commit()
    conn.close()
