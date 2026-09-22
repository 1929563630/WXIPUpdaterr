"""
企业微信可信IP自动更新器 - 主程序
FastAPI + WebSocket + APScheduler
"""
import asyncio
import json
import os
import sys
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    WEB_PORT, IP_CHECK_INTERVAL, DATA_DIR,
    load_user_config, save_user_config,
    STATUS_REPORT_INTERVAL, STATUS_REPORT_ON_START,
)
from database import (
    init_db, add_log, get_logs, get_conn, get_current_ip, update_current_ip,
    add_ip_history, get_ip_history, upsert_app, get_apps, delete_app
)
from services.ip_check import get_public_ip
from services.notify import (
    send_notification, notify_startup, notify_startup_failed,
    notify_login_expired, notify_ip_changed, notify_job_error,
    notify_status_report,
)
from browser.auth import BrowserManager
from browser.ip_updater import IPUpdater

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(DATA_DIR, "updater.log"), encoding="utf-8"),
    ]
)
logger = logging.getLogger("WeChatIPUpdater")

browser_manager = BrowserManager()
ip_updater = IPUpdater(browser_manager)
ws_clients = set()
scheduler = AsyncIOScheduler()

# 上次已知的登录状态（用于避免重复通知）
# None = 未知，True = 有效，False = 失效
_last_login_state = None


def get_effective_settings() -> dict:
    """获取当前生效的运行参数（用户配置优先，否则用默认值）"""
    cfg = load_user_config()
    try:
        ip_interval = int(cfg.get("ip_check_interval", IP_CHECK_INTERVAL))
    except (ValueError, TypeError):
        ip_interval = IP_CHECK_INTERVAL
    try:
        report_interval = int(cfg.get("status_report_interval", STATUS_REPORT_INTERVAL))
    except (ValueError, TypeError):
        report_interval = STATUS_REPORT_INTERVAL
    try:
        login_interval = int(cfg.get("login_check_interval", 1800))
    except (ValueError, TypeError):
        login_interval = 1800

    report_enabled = bool(cfg.get("status_report_enabled", True))
    report_on_start = bool(cfg.get("status_report_on_start", STATUS_REPORT_ON_START))
    login_enabled = bool(cfg.get("login_check_enabled", True))

    # 保护性下限
    if ip_interval < 30:
        ip_interval = 30
    if report_interval < 60:
        report_interval = 60
    if login_interval < 300:
        login_interval = 300

    return {
        "ip_check_interval": ip_interval,
        "login_check_enabled": login_enabled,
        "login_check_interval": login_interval,
        "status_report_enabled": report_enabled,
        "status_report_interval": report_interval,
        "status_report_on_start": report_on_start,
    }


def _has_login_expired(results):
    for r in results or []:
        msg = r.get("message") or ""
        if "登录态已失效" in msg or "登录已失效" in msg or "未登录" in msg:
            return True
    return False


async def ws_broadcast(msg_type, data):
    global ws_clients
    message = json.dumps({"type": msg_type, "data": data}, ensure_ascii=False)
    disconnected = set()
    for ws in ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.add(ws)
    ws_clients -= disconnected


async def ip_check_job():
    try:
        new_ip = await get_public_ip()
        if not new_ip:
            return
        current = get_current_ip()
        old_ip = current.get("ip", "0.0.0.0")
        ip_changed = (new_ip != old_ip)

        if not ip_changed:
            update_current_ip(new_ip, ip_changed=False)
            return

        logger.info(f"检测到IP变化: {old_ip} -> {new_ip}")
        add_log("INFO", f"检测到公网IP变化: {old_ip} -> {new_ip}")
        await ws_broadcast("log", {"level": "INFO", "message": f"检测到IP变化: {old_ip} -> {new_ip}"})
        await ws_broadcast("ip_changed", {"old_ip": old_ip, "new_ip": new_ip})
        await notify_ip_changed(old_ip, new_ip, ws_broadcast)

        apps = get_apps()
        config = load_user_config()
        app_ids = config.get("app_ids", [])
        if not app_ids and not apps:
            add_log("WARN", "没有配置应用ID，跳过更新")
            await ws_broadcast("log", {"level": "WARN", "message": "没有配置应用ID，跳过更新"})
            update_current_ip(new_ip, ip_changed=True)
            return

        for aid in app_ids:
            upsert_app(aid)

        all_success = True
        results = []
        for app_id in app_ids:
            result = await ip_updater.update_trusted_ip(app_id, new_ip)
            results.append({"app_id": app_id, **result})
            if not result["success"]:
                all_success = False

        update_current_ip(new_ip, ip_changed=True)
        add_ip_history(old_ip, new_ip, "全部成功" if all_success else "部分失败")

        await send_notification(old_ip, new_ip, results, ws_broadcast)

        if _has_login_expired(results):
            await notify_login_expired(ws_broadcast)

    except Exception as e:
        logger.error(f"IP检测任务异常: {e}")
        add_log("ERROR", f"IP检测任务异常: {str(e)}")
        await notify_job_error(str(e), ws_broadcast)


async def login_check_job():
    """
    低频检查企业微信登录态。

    行为：
    - 未登录（auth_state.json 不存在）时跳过，不算"失效"；
    - 状态从"有效 → 失效"时发通知；
    - 状态从"失效 → 有效"时记一条恢复日志；
    - 首次运行时只记录状态，不发通知（避免启动即误报）。
    """
    global _last_login_state
    try:
        auth_file = os.path.join(DATA_DIR, "auth_state.json")
        if not os.path.exists(auth_file):
            # 从未登录过，跳过
            return

        valid = await browser_manager.check_login_valid()
        browser_manager._logged_in = valid
        browser_manager._login_checked = True

        # 首次运行：只记录，不通知
        if _last_login_state is None:
            _last_login_state = valid
            add_log("INFO", f"登录态检查: {'有效' if valid else '已失效'}")
            return

        # 从有效 → 失效：发通知
        if _last_login_state and not valid:
            logger.warning("登录态已失效")
            add_log("WARN", "登录态检查发现已失效")
            await ws_broadcast("log", {"level": "WARN", "message": "⚠️ 登录态已失效，请重新扫码"})
            await notify_login_expired(ws_broadcast)

        # 从失效 → 有效：记一条恢复日志
        elif not _last_login_state and valid:
            add_log("INFO", "登录态已恢复")
            await ws_broadcast("log", {"level": "SUCCESS", "message": "✅ 登录态已恢复"})

        _last_login_state = valid

    except Exception as e:
        logger.error(f"登录态检查异常: {e}")


async def status_report_job():
    """定期运行状态汇报"""
    try:
        await notify_status_report(
            ws_broadcast,
            reason="定时汇报",
            scheduler_running=scheduler.running,
            browser_manager=browser_manager,
        )
    except Exception as e:
        logger.error(f"状态汇报异常: {e}")
        add_log("ERROR", f"状态汇报异常: {e}")


async def _reschedule_jobs():
    """根据当前有效设置重新排期定时任务"""
    settings = get_effective_settings()
    try:
        # 1. IP 检测任务（一直有）
        scheduler.add_job(
            ip_check_job,
            IntervalTrigger(seconds=settings["ip_check_interval"]),
            id="ip_check",
            name="公网IP检测",
            replace_existing=True,
        )

        # 2. 登录态检查任务
        if settings["login_check_enabled"]:
            scheduler.add_job(
                login_check_job,
                IntervalTrigger(seconds=settings["login_check_interval"]),
                id="login_check",
                name="登录态检查",
                replace_existing=True,
            )
        else:
            try:
                scheduler.remove_job("login_check")
            except Exception:
                pass

        # 3. 状态汇报任务
        if settings["status_report_enabled"]:
            scheduler.add_job(
                status_report_job,
                IntervalTrigger(seconds=settings["status_report_interval"]),
                id="status_report",
                name="运行状态汇报",
                replace_existing=True,
            )
        else:
            try:
                scheduler.remove_job("status_report")
            except Exception:
                pass

        add_log(
            "INFO",
            f"定时任务已重新排期: IP检测 {settings['ip_check_interval']}s, "
            f"登录检查 {'每 ' + str(settings['login_check_interval']) + 's' if settings['login_check_enabled'] else '已关闭'}, "
            f"状态汇报 {'每 ' + str(settings['status_report_interval']) + 's' if settings['status_report_enabled'] else '已关闭'}"
        )
    except Exception as e:
        logger.error(f"重新排期任务失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_db()
        browser_manager.set_ws_broadcast(ws_broadcast)
        ip_updater.set_ws_broadcast(ws_broadcast)

        settings = get_effective_settings()

        # IP 检测
        scheduler.add_job(
            ip_check_job,
            IntervalTrigger(seconds=settings["ip_check_interval"]),
            id="ip_check",
            name="公网IP检测",
            replace_existing=True,
        )

        # 登录态检查
        if settings["login_check_enabled"]:
            scheduler.add_job(
                login_check_job,
                IntervalTrigger(seconds=settings["login_check_interval"]),
                id="login_check",
                name="登录态检查",
                replace_existing=True,
            )

        # 状态汇报
        if settings["status_report_enabled"]:
            scheduler.add_job(
                status_report_job,
                IntervalTrigger(seconds=settings["status_report_interval"]),
                id="status_report",
                name="运行状态汇报",
                replace_existing=True,
            )

        scheduler.start()
        add_log(
            "INFO",
            f"服务启动，IP检测每 {settings['ip_check_interval']}s，"
            f"登录检查 {'每 ' + str(settings['login_check_interval']) + 's' if settings['login_check_enabled'] else '已关闭'}，"
            f"状态汇报 {'每 ' + str(settings['status_report_interval']) + 's' if settings['status_report_enabled'] else '已关闭'}"
        )
        logger.info(f"服务启动: http://0.0.0.0:{WEB_PORT}")

        asyncio.create_task(notify_startup(ws_broadcast, WEB_PORT))
        asyncio.create_task(ip_check_job())
        if settings["status_report_enabled"] and settings["status_report_on_start"]:
            asyncio.create_task(notify_status_report(
                ws_broadcast,
                reason="服务启动",
                scheduler_running=True,
                browser_manager=browser_manager,
            ))
    except Exception as e:
        logger.exception("启动失败")
        try:
            asyncio.create_task(notify_startup_failed(str(e), ws_broadcast))
        except Exception:
            pass
        raise

    yield

    scheduler.shutdown()
    await browser_manager.close()
    add_log("INFO", "服务已停止")


app = FastAPI(title="企业微信可信IP自动更新器", lifespan=lifespan)

static_dir = os.path.join(os.path.dirname(__file__), "web", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/api/qr_image")
async def api_qr_image():
    from browser.auth import QR_IMAGE_FILE
    if os.path.exists(QR_IMAGE_FILE):
        return FileResponse(QR_IMAGE_FILE, media_type="image/png")
    return JSONResponse({"error": "no qr code"}, status_code=404)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(static_dir, "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>企业微信可信IP自动更新器</h1><p>前端文件缺失</p>")


@app.get("/api/status")
async def api_status():
    current = get_current_ip()
    config = load_user_config()
    apps = get_apps()
    settings = get_effective_settings()

    if not browser_manager._login_checked:
        browser_manager._login_checked = True
        auth_file = os.path.join(DATA_DIR, "auth_state.json")
        if os.path.exists(auth_file):
            browser_manager._logged_in = await browser_manager.check_login_valid()
        else:
            browser_manager._logged_in = False

    return JSONResponse({
        "current_ip": current.get("ip", "0.0.0.0"),
        "last_check": current.get("last_check", ""),
        "last_change": current.get("last_change", ""),
        "app_ids": config.get("app_ids", []),
        "apps": apps,
        "logged_in": browser_manager._logged_in,
        "check_interval": settings["ip_check_interval"],
        "login_check_enabled": settings["login_check_enabled"],
        "login_check_interval": settings["login_check_interval"],
        "status_report_enabled": settings["status_report_enabled"],
        "status_report_interval": settings["status_report_interval"],
        "status_report_on_start": settings["status_report_on_start"],
        "scheduler_running": scheduler.running,
    })


@app.get("/api/settings")
async def api_get_settings():
    return JSONResponse(get_effective_settings())


@app.post("/api/settings")
async def api_save_settings(data: dict):
    """保存运行参数并重新排期"""
    cfg = load_user_config()
    changed = False

    if "ip_check_interval" in data:
        try:
            v = int(data["ip_check_interval"])
            if v < 30:
                v = 30
            cfg["ip_check_interval"] = v
            changed = True
        except (ValueError, TypeError):
            return JSONResponse({"success": False, "message": "IP检测间隔必须为整数"}, status_code=400)

    if "login_check_enabled" in data:
        cfg["login_check_enabled"] = bool(data["login_check_enabled"])
        changed = True

    if "login_check_interval" in data:
        try:
            v = int(data["login_check_interval"])
            if v < 300:
                v = 300
            cfg["login_check_interval"] = v
            changed = True
        except (ValueError, TypeError):
            return JSONResponse({"success": False, "message": "登录态检查间隔必须为整数"}, status_code=400)

    if "status_report_enabled" in data:
        cfg["status_report_enabled"] = bool(data["status_report_enabled"])
        changed = True

    if "status_report_interval" in data:
        try:
            v = int(data["status_report_interval"])
            if v < 60:
                v = 60
            cfg["status_report_interval"] = v
            changed = True
        except (ValueError, TypeError):
            return JSONResponse({"success": False, "message": "状态汇报间隔必须为整数"}, status_code=400)

    if "status_report_on_start" in data:
        cfg["status_report_on_start"] = bool(data["status_report_on_start"])
        changed = True

    if changed:
        save_user_config(cfg)
        await _reschedule_jobs()
        add_log("INFO", "运行参数已更新")
        await ws_broadcast("log", {"level": "SUCCESS", "message": "运行参数已保存，定时任务已重新排期"})

    return JSONResponse({"success": True, "settings": get_effective_settings()})


@app.get("/api/logs")
async def api_logs(limit: int = 100):
    return JSONResponse(get_logs(limit))


@app.post("/api/logs/clear")
async def api_clear_logs():
    conn = get_conn()
    conn.execute("DELETE FROM logs")
    conn.commit()
    conn.close()
    return JSONResponse({"success": True})


@app.get("/api/ip_history")
async def api_ip_history(limit: int = 20):
    return JSONResponse(get_ip_history(limit))


@app.post("/api/apps")
async def api_add_apps(data: dict):
    app_ids = data.get("app_ids", [])
    if not app_ids:
        return JSONResponse({"error": "app_ids 不能为空"}, status_code=400)
    config = load_user_config()
    existing = set(config.get("app_ids", []))
    for aid in app_ids:
        aid = aid.strip()
        if aid:
            existing.add(aid)
            upsert_app(aid)
    config["app_ids"] = list(existing)
    save_user_config(config)
    add_log("INFO", f"应用ID列表已更新: {list(existing)}")
    return JSONResponse({"app_ids": list(existing)})


@app.delete("/api/apps/{agent_id}")
async def api_delete_app(agent_id: str):
    delete_app(agent_id)
    config = load_user_config()
    app_ids = config.get("app_ids", [])
    if agent_id in app_ids:
        app_ids.remove(agent_id)
        config["app_ids"] = app_ids
        save_user_config(config)
    return JSONResponse({"success": True})


@app.post("/api/login")
async def api_start_login():
    asyncio.create_task(_do_login_flow())
    return JSONResponse({"message": "登录流程已启动"})


async def _do_login_flow():
    result = await browser_manager.start_login()
    if result["success"]:
        asyncio.create_task(_wait_login_and_extract())


async def _wait_login_and_extract():
    success = await browser_manager.poll_login_status()
    if success:
        # 登录成功后重置上一次的登录状态
        global _last_login_state
        _last_login_state = True
        await _auto_extract_and_update()


async def _auto_extract_and_update():
    config = load_user_config()
    agent_ids = config.get("app_ids", [])
    if not agent_ids:
        await ws_broadcast("log", {"level": "WARN", "message": "未配置应用ID，请先添加"})
        await ws_broadcast("show_agent_input", {"reason": "未配置应用ID"})
        return
    await ws_broadcast("log", {"level": "INFO", "message": f"开始更新 {len(agent_ids)} 个应用的可信IP..."})
    await _do_force_update()


@app.post("/api/verify_code")
async def api_submit_code(data: dict):
    code = (data.get("code") or "").strip()
    if not code:
        return JSONResponse({"success": False, "message": "验证码不能为空"}, status_code=400)
    try:
        ok = await browser_manager.submit_verification_code(code)
        if ok:
            return JSONResponse({"success": True, "message": "验证码已提交"})
        return JSONResponse({"success": False, "message": "提交失败，请检查验证码是否正确"})
    except Exception as e:
        logger.exception("提交验证码异常")
        return JSONResponse({"success": False, "message": f"提交异常: {e}"}, status_code=500)


@app.post("/api/check_login")
async def api_check_login():
    valid = await browser_manager.check_login_valid()
    return JSONResponse({"valid": valid})


@app.post("/api/scheduler/start")
async def api_scheduler_start():
    if scheduler.running:
        return JSONResponse({"running": True, "message": "定时检测已在运行"})
    scheduler.start()
    add_log("INFO", "定时IP检测已启动")
    return JSONResponse({"running": True, "message": "定时检测已启动"})


@app.post("/api/scheduler/stop")
async def api_scheduler_stop():
    if not scheduler.running:
        return JSONResponse({"running": False, "message": "定时检测未在运行"})
    scheduler.shutdown(wait=False)
    add_log("INFO", "定时IP检测已停止")
    return JSONResponse({"running": False, "message": "定时检测已停止"})


@app.post("/api/status_report")
async def api_status_report():
    """手动触发一次状态汇报"""
    asyncio.create_task(notify_status_report(
        ws_broadcast,
        reason="手动触发",
        scheduler_running=scheduler.running,
        browser_manager=browser_manager,
    ))
    return JSONResponse({"success": True, "message": "状态汇报已触发"})


@app.post("/api/logout")
async def api_logout():
    global _last_login_state
    auth_file = os.path.join(DATA_DIR, "auth_state.json")
    if os.path.exists(auth_file):
        os.remove(auth_file)
    config = load_user_config()
    config["app_ids"] = []
    save_user_config(config)
    conn = get_conn()
    conn.execute("DELETE FROM apps")
    conn.execute("DELETE FROM logs")
    conn.commit()
    conn.close()
    browser_manager._logged_in = False
    browser_manager._login_checked = True
    _last_login_state = None
    add_log("INFO", "已退出登录，所有配置已清除")
    return JSONResponse({"success": True, "message": "已退出登录"})


@app.post("/api/force_update")
async def api_force_update():
    asyncio.create_task(_do_force_update())
    return JSONResponse({"message": "强制更新已启动"})


async def _do_force_update():
    new_ip = await get_public_ip()
    if not new_ip:
        add_log("ERROR", "获取公网IP失败")
        await ws_broadcast("log", {"level": "ERROR", "message": "获取公网IP失败"})
        return
    config = load_user_config()
    app_ids = config.get("app_ids", [])
    if not app_ids:
        add_log("WARN", "没有配置应用ID")
        await ws_broadcast("log", {"level": "WARN", "message": "没有配置应用ID，请先添加"})
        return
    old_ip = get_current_ip().get("ip", "")
    results = []
    for app_id in app_ids:
        result = await ip_updater.update_trusted_ip(app_id, new_ip)
        results.append({"app_id": app_id, **result})
    update_current_ip(new_ip, ip_changed=(old_ip != new_ip))
    add_ip_history(old_ip, new_ip, "手动强制更新")

    await send_notification(old_ip, new_ip, results, ws_broadcast)

    if _has_login_expired(results):
        await notify_login_expired(ws_broadcast)


@app.post("/api/check_ip")
async def api_check_ip():
    new_ip = await get_public_ip()
    current = get_current_ip()
    return JSONResponse({
        "current_ip": current.get("ip", "0.0.0.0"),
        "detected_ip": new_ip,
        "changed": new_ip != current.get("ip", ""),
    })


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)
    try:
        current = get_current_ip()
        await websocket.send_text(json.dumps({
            "type": "status",
            "data": {
                "current_ip": current.get("ip", "0.0.0.0"),
                "logged_in": os.path.exists(os.path.join(DATA_DIR, "auth_state.json")),
            }
        }, ensure_ascii=False))
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(websocket)


if __name__ == "__main__":
    main_settings = get_effective_settings()
    logger.info(
        f"有效配置: IP检测 {main_settings['ip_check_interval']}s, "
        f"登录检查 {'每 ' + str(main_settings['login_check_interval']) + 's' if main_settings['login_check_enabled'] else '已关闭'}, "
        f"状态汇报 {'每 ' + str(main_settings['status_report_interval']) + 's' if main_settings['status_report_enabled'] else '已关闭'}"
    )
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=WEB_PORT, log_level="info")
