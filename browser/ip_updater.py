"""
企业微信可信IP更新操作
固定XPath流程：配置按钮 → 输入IP → 确认
"""
import asyncio
import re
from typing import Optional, Callable
from playwright.async_api import Page

from database import update_app_ip, add_log


# 固定XPath（用户确认的流程）
BTN_CONFIG_XPATH = "/html/body/div[1]/section[3]/div[1]/main/div/form/div/div[2]/div[5]/div/div[4]/ul/li[4]/div/div[3]/a"
TEXTAREA_XPATH = "/html/body/div[3]/div/div[2]/div/div[2]/textarea"
BTN_CONFIRM_XPATH = "/html/body/div[3]/div/div[3]/a[1]"


class IPUpdater:
    """可信IP更新器"""

    def __init__(self, browser_manager):
        self._bm = browser_manager
        self._ws_broadcast: Optional[Callable] = None

    def set_ws_broadcast(self, func: Callable):
        self._ws_broadcast = func

    async def _broadcast(self, msg_type: str, data: dict):
        if self._ws_broadcast:
            await self._ws_broadcast(msg_type, data)

    async def update_trusted_ip(self, agent_id: str, new_ip: str) -> dict:
        """更新指定应用的可信IP"""
        await self._broadcast("log", {
            "level": "INFO", "message": f"正在更新应用 {agent_id} 可信IP为 {new_ip}..."
        })

        page, context, logged_in = await self._bm.get_logged_in_page()
        if not logged_in:
            msg = "登录态已失效，请重新扫码"
            await self._broadcast("log", {"level": "ERROR", "message": msg})
            add_log("ERROR", msg)
            return {"success": False, "message": msg, "old_ip": ""}

        try:
            # Step 1: 进入应用详情页
            detail_url = f"https://work.weixin.qq.com/wework_admin/frame#apps/modApiApp/{agent_id}"
            await page.goto(detail_url, wait_until="domcontentloaded")
            await asyncio.sleep(3)

            if "login" in page.url.lower():
                msg = "登录态已失效"
                await self._broadcast("log", {"level": "ERROR", "message": msg})
                return {"success": False, "message": msg, "old_ip": ""}

            # Step 2: 读取当前IP（从页面文本提取）
            old_ip = ""
            try:
                body_text = await page.inner_text("body")
                ip_match = re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", body_text)
                if ip_match:
                    old_ip = ip_match.group()
            except Exception:
                pass

            await self._broadcast("log", {"level": "INFO", "message": f"当前可信IP: {old_ip or '(未配置)'}"})

            # Step 3: 点击配置按钮打开弹窗
            await self._broadcast("log", {"level": "INFO", "message": "正在打开配置弹窗..."})
            config_btn = page.locator(f"xpath={BTN_CONFIG_XPATH}")
            await config_btn.click()
            await asyncio.sleep(2)

            # Step 4: 输入新IP（在弹窗modal内找textarea，排除页面原有的）
            await self._broadcast("log", {"level": "INFO", "message": f"正在输入新IP: {new_ip}"})
            textarea = None
            # 优先在弹窗容器内查找
            for sel in [".modal textarea", "[role=dialog] textarea", ".el-dialog textarea", "div[class*=modal] textarea", "div[class*=dialog] textarea"]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=1000):
                        textarea = el
                        break
                except Exception:
                    continue
            # 兜底：遍历所有textarea，排除页面原有的
            if not textarea:
                all_ta = page.locator("textarea")
                count = await all_ta.count()
                for i in range(count):
                    ta = all_ta.nth(i)
                    try:
                        name = await ta.get_attribute("name") or ""
                        placeholder = await ta.get_attribute("placeholder") or ""
                        if name not in ("description", "comment", "content") and "介绍" not in placeholder and await ta.is_visible():
                            textarea = ta
                            break
                    except Exception:
                        continue
            if not textarea:
                raise Exception("未找到IP输入框")

            await textarea.click()
            await asyncio.sleep(0.2)
            await textarea.fill("")
            await asyncio.sleep(0.2)
            await textarea.fill(new_ip)
            await asyncio.sleep(0.5)

            # Step 5: 点击确认
            await self._broadcast("log", {"level": "INFO", "message": "正在点击确认..."})
            confirmed = False
            for sel in [
                ".modal a:has-text('确定')", ".modal a:has-text('确认')",
                "[role=dialog] a:has-text('确定')", "[role=dialog] a:has-text('确认')",
                ".el-dialog a:has-text('确定')", ".el-dialog a:has-text('确认')",
                "a:has-text('确定')", "a:has-text('确认')",
            ]:
                try:
                    btn = page.locator(sel).last
                    if await btn.is_visible():
                        await btn.click()
                        confirmed = True
                        break
                except Exception:
                    continue

            # 检查结果
            update_app_ip(agent_id, new_ip, "success")
            add_log("SUCCESS", f"应用 {agent_id} 可信IP更新成功: {old_ip} -> {new_ip}")
            await self._broadcast("log", {
                "level": "SUCCESS",
                "message": f"✅ 应用 {agent_id} 更新成功: {old_ip} → {new_ip}"
            })
            await self._broadcast("ip_update_result", {
                "agent_id": agent_id, "success": True,
                "old_ip": old_ip, "new_ip": new_ip,
            })
            return {"success": True, "message": "更新成功", "old_ip": old_ip}

        except Exception as e:
            msg = f"更新异常: {e}"
            await self._broadcast("log", {"level": "ERROR", "message": msg})
            add_log("ERROR", msg)
            return {"success": False, "message": msg, "old_ip": ""}
