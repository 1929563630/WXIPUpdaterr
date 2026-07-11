"""
企业微信可信IP自动更新器 - 主程序
FastAPI + WebSocket + APScheduler
"""
import asyncio
import json
import os
import sys
import logging
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import WEB_PORT, IP_CHECK_INTERVAL, DATA_DIR, load_user_config, save_user_config
from database import (
    init_db, add_log, get_logs, get_conn, get_current_ip, update_current_ip,
    add_ip_history, get_ip_history, upsert_app, get_apps, delete_app,
    update_app_ip
)
from services.ip_check import get_public_ip
from browser.auth import BrowserManager
from browser.ip_updater import IPUpdater

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(DATA_DIR, "updater.log"), encoding="utf-8"),
    ]
)
logger = logging.getLogger("WeChatIPUpdater")

# 全局对象
browser_manager = BrowserManager()
ip_updater = IPUpdater(browser_manager)
ws_clients: set[WebSocket] = set()
scheduler = AsyncIOScheduler()


async def ws_broadcast(msg_type: str, data: dict):
    """广播消息到所有 WebSocket 客户端"""
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
    """定时检测公网IP，变化则自动更新"""
    try:
        new_ip = await get_public_ip()
        if not new_ip:
            return

        current = get_current_ip()
        old_ip = current.get("ip", "0.0.0.0")

        if new_ip == old_ip:
            return

        # IP变化了
        logger.info(f"检测到IP变化: {old_ip} -> {new_ip}")
        add_log("INFO", f"检测到公网IP变化: {old_ip} -> {new_ip}")
        await ws_broadcast("log", {"level": "INFO", "message": f"检测到IP变化: {old_ip} -> {new_ip}"})
        await ws_broadcast("ip_changed", {"old_ip": old_ip, "new_ip": new_ip})

        # 更新所有应用的可信IP
        apps = get_apps()
        config = load_user_config()
        app_ids = config.get("app_ids", [])

        if not app_ids and not apps:
            add_log("WARN", "没有配置应用ID，跳过更新")
            await ws_broadcast("log", {"level": "WARN", "message": "没有配置应用ID，跳过更新"})
            update_current_ip(new_ip)
            return

        # 确保数据库中有所有配置的应用
        for aid in app_ids:
            upsert_app(aid)

        all_success = True
        results = []

        for app_id in app_ids:
            result = await ip_updater.update_trusted_ip(app_id, new_ip)
            results.append({"app_id": app_id, **result})
            if not result["success"]:
                all_success = False

        # 更新当前IP记录
        update_current_ip(new_ip)
        add_ip_history(old_ip, new_ip, "全部成功" if all_success else "部分失败")

        # 发送通知
        from services.notify import send_notification
        await send_notification(old_ip, new_ip, results, ws_broadcast)

    except Exception as e:
        logger.error(f"IP检测任务异常: {e}")
        add_log("ERROR", f"IP检测任务异常: {str(e)}")


# FastAPI 生命周期
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动
    init_db()
    browser_manager.set_ws_broadcast(ws_broadcast)
    ip_updater.set_ws_broadcast(ws_broadcast)

    # 启动定时任务
    scheduler.add_job(
        ip_check_job,
        IntervalTrigger(seconds=IP_CHECK_INTERVAL),
        id="ip_check",
        name="公网IP检测",
        replace_existing=True,
    )
    scheduler.start()
    add_log("INFO", "服务启动，定时IP检测已开启")
    logger.info(f"服务启动: http://0.0.0.0:{WEB_PORT}")

    # 启动时立即检测一次
    asyncio.create_task(ip_check_job())

    yield

    # 关闭
    scheduler.shutdown()
    await browser_manager.close()
    add_log("INFO", "服务已停止")


app = FastAPI(title="企业微信可信IP自动更新器", lifespan=lifespan)

# 静态文件
static_dir = os.path.join(os.path.dirname(__file__), "web", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/api/qr_image")
async def api_qr_image():
    """获取二维码图片"""
    from browser.auth import QR_IMAGE_FILE
    if os.path.exists(QR_IMAGE_FILE):
        return FileResponse(QR_IMAGE_FILE, media_type="image/png")
    return JSONResponse({"error": "no qr code"}, status_code=404)


@app.get("/", response_class=HTMLResponse)
async def index():
    """主页"""
    html_path = os.path.join(static_dir, "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>企业微信可信IP自动更新器</h1><p>前端文件缺失</p>")


@app.get("/api/status")
async def api_status():
    """获取系统状态"""
    current = get_current_ip()
    config = load_user_config()
    apps = get_apps()

    # 首次请求时验证cookie是否真的有效
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
        "app_ids": config.get("app_ids", []),
        "apps": apps,
        "logged_in": browser_manager._logged_in,
        "check_interval": IP_CHECK_INTERVAL,
        "scheduler_running": scheduler.running,
    })


@app.get("/api/logs")
async def api_logs(limit: int = 100):
    """获取日志"""
    return JSONResponse(get_logs(limit))


@app.post("/api/logs/clear")
async def api_clear_logs():
    """清空日志"""
    conn = get_conn()
    conn.execute("DELETE FROM logs")
    conn.commit()
    conn.close()
    return JSONResponse({"success": True})


@app.get("/api/ip_history")
async def api_ip_history(limit: int = 20):
    """获取IP变更历史"""
    return JSONResponse(get_ip_history(limit))


@app.post("/api/apps")
async def api_add_apps(data: dict):
    """添加应用ID"""
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
    """删除应用"""
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
    """启动扫码登录"""
    asyncio.create_task(_do_login_flow())
    return JSONResponse({"message": "登录流程已启动"})


async def _do_login_flow():
    """执行登录流程"""
    result = await browser_manager.start_login()
    if result["success"]:
        # 等待扫码，登录成功后自动同步应用并更新IP
        asyncio.create_task(_wait_login_and_extract())


async def _wait_login_and_extract():
    """等待登录成功后自动提取配置"""
    success = await browser_manager.poll_login_status()
    if success:
        await _auto_extract_and_update()


async def _auto_extract_and_update():
    """登录成功后自动更新IP"""
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
    """提交验证码"""
    code = data.get("code", "")
    if not code:
        return JSONResponse({"error": "验证码不能为空"}, status_code=400)
    result = await browser_manager.submit_verification_code(code)
    return JSONResponse({"success": result})


@app.post("/api/check_login")
async def api_check_login():
    """检查登录态"""
    valid = await browser_manager.check_login_valid()
    return JSONResponse({"valid": valid})


@app.post("/api/scheduler/start")
async def api_scheduler_start():
    """启动定时检测"""
    if scheduler.running:
        return JSONResponse({"running": True, "message": "定时检测已在运行"})
    scheduler.start()
    add_log("INFO", "定时IP检测已启动")
    return JSONResponse({"running": True, "message": "定时检测已启动"})


@app.post("/api/scheduler/stop")
async def api_scheduler_stop():
    """停止定时检测"""
    if not scheduler.running:
        return JSONResponse({"running": False, "message": "定时检测未在运行"})
    scheduler.shutdown(wait=False)
    add_log("INFO", "定时IP检测已停止")
    return JSONResponse({"running": False, "message": "定时检测已停止"})





@app.post("/api/logout")
async def api_logout():
    """退出登录，清除所有配置"""
    # 删除登录态文件
    auth_file = os.path.join(DATA_DIR, "auth_state.json")
    if os.path.exists(auth_file):
        os.remove(auth_file)

    # 清空应用配置
    config = load_user_config()
    config["app_ids"] = []
    save_user_config(config)

    # 清空数据库中的应用记录
    conn = get_conn()
    conn.execute("DELETE FROM apps")
    conn.execute("DELETE FROM logs")
    conn.commit()
    conn.close()

    # 重置浏览器管理器登录状态
    browser_manager._logged_in = False
    browser_manager._login_checked = True

    add_log("INFO", "已退出登录，所有配置已清除")
    return JSONResponse({"success": True, "message": "已退出登录"})


@app.post("/api/force_update")
async def api_force_update():
    """强制更新所有应用IP"""
    asyncio.create_task(_do_force_update())
    return JSONResponse({"message": "强制更新已启动"})


async def _do_force_update():
    """执行强制更新"""
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

    update_current_ip(new_ip)
    add_ip_history(old_ip, new_ip, "手动强制更新")

    from services.notify import send_notification
    await send_notification(old_ip, new_ip, results, ws_broadcast)


@app.post("/api/check_ip")
async def api_check_ip():
    """手动检测IP"""
    new_ip = await get_public_ip()
    current = get_current_ip()
    return JSONResponse({
        "current_ip": current.get("ip", "0.0.0.0"),
        "detected_ip": new_ip,
        "changed": new_ip != current.get("ip", ""),
    })





@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 端点"""
    await websocket.accept()
    ws_clients.add(websocket)
    try:
        # 发送当前状态
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
            # 处理前端发来的消息
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
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=WEB_PORT, log_level="info")
