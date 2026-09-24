"""
SQLite 数据库操作
"""
import sqlite3
import os
import json
from datetime import datetime
from config import DB_FILE


def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cursor = conn.cursor()

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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ip_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            old_ip TEXT,
            new_ip TEXT,
            changed_at TEXT DEFAULT (datetime('now','localtime')),
            update_result TEXT DEFAULT ''
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT DEFAULT 'INFO',
            message TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS current_ip (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            ip TEXT DEFAULT '0.0.0.0',
            last_check TEXT DEFAULT (datetime('now','localtime')),
            last_change TEXT DEFAULT '',
            last_login_check TEXT DEFAULT '',
            login_valid INTEGER DEFAULT -1,
            login_checked_at TEXT DEFAULT ''
        )
    """)

    # 兼容旧库：逐列检查并 ALTER
    try:
        cols = [row["name"] for row in cursor.execute("PRAGMA table_info(current_ip)").fetchall()]
        if "last_change" not in cols:
            cursor.execute("ALTER TABLE current_ip ADD COLUMN last_change TEXT DEFAULT ''")
        if "last_login_check" not in cols:
            cursor.execute("ALTER TABLE current_ip ADD COLUMN last_login_check TEXT DEFAULT ''")
        if "login_valid" not in cols:
            cursor.execute("ALTER TABLE current_ip ADD COLUMN login_valid INTEGER DEFAULT -1")
        if "login_checked_at" not in cols:
            cursor.execute("ALTER TABLE current_ip ADD COLUMN login_checked_at TEXT DEFAULT ''")
    except Exception:
        pass

    # 迁移：last_change 为空时，从 ip_history 填充
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
    conn = get_conn()
    conn.execute("INSERT INTO logs (level, message) VALUES (?, ?)", (level, message))
    cursor = conn.cursor()
    cursor.execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 1000)")
    conn.commit()
    conn.close()


def get_logs(limit: int = 100) -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_current_ip() -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM current_ip WHERE id = 1").fetchone()
    conn.close()
    if row:
        result = dict(row)
        result.setdefault("last_change", "")
        result.setdefault("last_login_check", "")
        result.setdefault("login_valid", -1)
        result.setdefault("login_checked_at", "")
        return result
    return {
        "ip": "0.0.0.0",
        "last_check": "",
        "last_change": "",
        "last_login_check": "",
        "login_valid": -1,
        "login_checked_at": "",
    }


def update_current_ip(ip: str, ip_changed: bool = False):
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


def update_login_check_time():
    """刷新'上次登录检测'时间"""
    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO current_ip (id, last_login_check) VALUES (1, datetime('now','localtime'))
            ON CONFLICT(id) DO UPDATE SET
                last_login_check = datetime('now','localtime')
        """)
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def update_login_state(valid: bool):
    """写入登录态和检查时间（Cookie保活/手动检查/登录成功后调用）"""
    conn = get_conn()
    try:
        v = 1 if valid else 0
        conn.execute("""
            INSERT INTO current_ip (id, login_valid, login_checked_at, last_login_check)
            VALUES (1, ?, datetime('now','localtime'), datetime('now','localtime'))
            ON CONFLICT(id) DO UPDATE SET
                login_valid = excluded.login_valid,
                login_checked_at = datetime('now','localtime'),
                last_login_check = datetime('now','localtime')
        """, (v,))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def get_login_state() -> dict:
    """
    读取缓存的登录态。
    返回 {"valid": True/False/None, "checked_at": "..."}
    valid 为 None 表示"还没检测过"
    """
    conn = get_conn()
    try:
        row = conn.execute("SELECT login_valid, login_checked_at FROM current_ip WHERE id = 1").fetchone()
        if row is None:
            return {"valid": None, "checked_at": ""}
        v = row["login_valid"]
        if v == 1:
            valid = True
        elif v == 0:
            valid = False
        else:
            valid = None
        return {"valid": valid, "checked_at": row["login_checked_at"] or ""}
    except Exception:
        return {"valid": None, "checked_at": ""}
    finally:
        conn.close()


def add_ip_history(old_ip: str, new_ip: str, result: str = ""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO ip_history (old_ip, new_ip, update_result) VALUES (?, ?, ?)",
        (old_ip, new_ip, result)
    )
    conn.commit()
    conn.close()


def get_ip_history(limit: int = 20) -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM ip_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_app(agent_id: str, app_name: str = "", current_ip: str = ""):
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
    conn = get_conn()
    conn.execute("""
        UPDATE apps SET current_ip = ?, last_update = datetime('now','localtime'), status = ?
        WHERE agent_id = ?
    """, (ip, status, agent_id))
    conn.commit()
    conn.close()


def get_apps() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM apps ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_app(agent_id: str):
    conn = get_conn()
    conn.execute("DELETE FROM apps WHERE agent_id = ?", (agent_id,))
    conn.commit()
    conn.close()
