"""
通知服务 - WebSocket推送
"""
from datetime import datetime
from database import add_log


async def send_notification(old_ip: str, new_ip: str, results: list, ws_broadcast=None):
    """全部应用更新完成后推送通知"""
    success_count = sum(1 for r in results if r.get("success"))
    total = len(results)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    title = f"{'✅' if success_count == total else '⚠️'} 可信IP更新{'成功' if success_count == total else '异常'}"
    lines = [
        f"IP变更: {old_ip} → {new_ip}",
        f"时间: {now}",
        f"结果: {success_count}/{total} 个应用更新成功",
        "",
    ]
    for r in results:
        status = "✅" if r.get("success") else "❌"
        lines.append(f"{status} 应用 {r['app_id']}: {r.get('message', '')}")

    content = "\n".join(lines)
    add_log("INFO", f"通知:\n{content}")

    if ws_broadcast:
        await ws_broadcast("notification", {
            "title": title,
            "content": content,
            "success": success_count == total,
        })

    return content
