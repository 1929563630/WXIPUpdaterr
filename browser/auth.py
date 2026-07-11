"""
浏览器登录管理 - Playwright 自动化
负责：扫码登录、Cookie持久化、验证码交互
"""
import asyncio
import os
import json
import base64
import time
from datetime import datetime
from typing import Optional, Callable, Awaitable
from playwright.async_api import async_playwright, Page, BrowserContext, Browser

from config import AUTH_STATE_FILE, WECHAT_LOGIN_URL, WECHAT_ADMIN_URL, DATA_DIR

# 二维码图片路径
QR_IMAGE_FILE = os.path.join(DATA_DIR, "qr_code.png")


class BrowserManager:
    """浏览器管理器"""

    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._logged_in = False
        self._login_checked = False  # 是否已做过首次登录验证
        self._ws_broadcast: Optional[Callable] = None
        self._login_lock = asyncio.Lock()

    def set_ws_broadcast(self, func: Callable):
        self._ws_broadcast = func

    async def _broadcast(self, msg_type: str, data: dict):
        if self._ws_broadcast:
            await self._ws_broadcast(msg_type, data)

    async def init_browser(self):
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        if self._browser is None:
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--lang=zh-CN"]
            )

    async def _create_context(self, use_saved_state: bool = True) -> BrowserContext:
        await self.init_browser()
        if use_saved_state and os.path.exists(AUTH_STATE_FILE):
            try:
                ctx = await self._browser.new_context(
                    storage_state=AUTH_STATE_FILE,
                    viewport={"width": 1280, "height": 720},
                    locale="zh-CN",
                )
                return ctx
            except Exception:
                pass
        return await self._browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="zh-CN",
        )

    async def _save_login_state(self):
        if self._context:
            try:
                await self._context.storage_state(path=AUTH_STATE_FILE)
                await self._broadcast("log", {"level": "INFO", "message": "登录态已保存"})
            except Exception as e:
                await self._broadcast("log", {"level": "ERROR", "message": f"保存登录态失败: {e}"})

    async def check_login_valid(self) -> bool:
        try:
            if self._context and self._page:
                url = self._page.url
                if "login" not in url.lower() and "work.weixin.qq.com" in url:
                    return True

            ctx = await self._create_context(use_saved_state=True)
            page = await ctx.new_page()
            await page.goto(WECHAT_ADMIN_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            is_valid = "login" not in page.url.lower()
            if is_valid:
                self._logged_in = True
            await ctx.close()
            return is_valid
        except Exception:
            return False

    async def start_login(self) -> dict:
        async with self._login_lock:
            await self._broadcast("log", {"level": "INFO", "message": "正在启动浏览器..."})

            try:
                await self._close_context()
                self._context = await self._create_context(use_saved_state=False)
                self._page = await self._context.new_page()

                await self._page.goto(WECHAT_LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(3)

                # 检查是否已登录
                if "login" not in self._page.url.lower():
                    self._logged_in = True
                    await self._save_login_state()
                    await self._broadcast("log", {"level": "SUCCESS", "message": "已有登录态"})
                    await self._broadcast("login_status", {"status": "success"})
                    return {"success": True, "qr_base64": "", "message": "已有登录态"}

                qr_path = await self._capture_qr_code()
                if qr_path:
                    await self._broadcast("log", {"level": "INFO", "message": "二维码已生成，请扫码"})
                    await self._broadcast("qr_code", {"image_url": "/api/qr_image?t=" + str(int(time.time()))})
                    return {"success": True, "qr_base64": "", "message": "请扫码"}
                else:
                    await self._broadcast("log", {"level": "ERROR", "message": "未找到二维码"})
                    return {"success": False, "qr_base64": "", "message": "未找到二维码"}
            except Exception as e:
                await self._broadcast("log", {"level": "ERROR", "message": f"启动登录失败: {e}"})
                return {"success": False, "qr_base64": "", "message": str(e)}

    async def _capture_qr_code(self) -> str:
        """精准获取二维码：iframe内 img.qrcode_login_img"""
        if not self._page:
            return ""

        img_bytes = None

        # 方式1: 企业微信登录页 - 二维码在iframe内
        try:
            await self._page.wait_for_selector("iframe", timeout=8000)
            iframe_el = await self._page.query_selector("iframe")
            if iframe_el:
                frame = await iframe_el.content_frame()
                if frame:
                    qr_el = await frame.query_selector("img.qrcode_login_img")
                    if qr_el:
                        src = await qr_el.get_attribute("src")
                        if src:
                            if src.startswith("/"):
                                src = "https://work.weixin.qq.com" + src
                            if src.startswith("http"):
                                import aiohttp
                                async with aiohttp.ClientSession() as session:
                                    async with session.get(src, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                        if resp.status == 200:
                                            img_bytes = await resp.read()
                        if not img_bytes or len(img_bytes) < 500:
                            img_bytes = await qr_el.screenshot()
        except Exception:
            pass

        # 方式2: 兜底 - 直接在主页面找
        if not img_bytes or len(img_bytes) < 500:
            qr_selectors = [
                "xpath=//div[contains(@class,'qr_code')]//img",
                "xpath=/html/body/div/div/div[2]/div[1]/img",
                "img[src*='qrcode']", "img.qrcode_login_img",
            ]
            for sel in qr_selectors:
                try:
                    el = self._page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        src = await el.get_attribute("src")
                        if src and src.startswith("http"):
                            import aiohttp
                            async with aiohttp.ClientSession() as session:
                                async with session.get(src, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                    if resp.status == 200:
                                        img_bytes = await resp.read()
                        if not img_bytes or len(img_bytes) < 500:
                            img_bytes = await el.screenshot()
                        if img_bytes and len(img_bytes) > 500:
                            break
                        img_bytes = None
                except Exception:
                    continue

        if img_bytes and len(img_bytes) > 200:
            with open(QR_IMAGE_FILE, "wb") as f:
                f.write(img_bytes)
            return QR_IMAGE_FILE

        return ""

    @staticmethod
    def cleanup_qr():
        """清理二维码图片"""
        try:
            if os.path.exists(QR_IMAGE_FILE):
                os.remove(QR_IMAGE_FILE)
        except Exception:
            pass

    async def poll_login_status(self, max_attempts: int = 60, interval: int = 2):
        """持续轮询登录状态，识别扫码→确认→登录成功全流程"""
        scanned = False
        for attempt in range(max_attempts):
            if not self._page:
                break
            try:
                url = self._page.url

                # 登录成功：页面跳转到后台首页
                if "wework_admin/frame" in url and "login" not in url.lower():
                    self._logged_in = True
                    self.cleanup_qr()
                    # 等待页面加载完成再保存cookie
                    try:
                        await self._page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    await self._save_login_state()
                    await self._broadcast("log", {"level": "SUCCESS", "message": "✅ 手机确认登录成功，登录态已保存"})
                    await self._broadcast("login_status", {"status": "success"})
                    return True

                # 二维码过期：页面跳回登录页
                if attempt > 5 and "loginpage_wx" in url:
                    self.cleanup_qr()
                    await self._broadcast("log", {"level": "WARN", "message": "⏰ 二维码已过期，请重新点击扫码登录"})
                    await self._broadcast("login_status", {"status": "expired"})
                    return False

                # 检测是否已扫码（页面可能出现确认提示）
                if not scanned:
                    try:
                        body_text = await self._page.inner_text("body")
                        confirm_hints = ["确认登录", "请在手机", "已扫码", "企业微信确认"]
                        for hint in confirm_hints:
                            if hint in body_text:
                                scanned = True
                                await self._broadcast("log", {"level": "INFO", "message": "📱 已检测到扫码，请在手机端点击确认登录"})
                                await self._broadcast("login_status", {"status": "scanned"})
                                break
                    except Exception:
                        pass

                # 检测验证码页面
                verify_detected = False
                for sel in ["input[placeholder*='验证码']", "input[name*='code']", "input[name*='verify']", "#verifyCode"]:
                    el = await self._page.query_selector(sel)
                    if el and await el.is_visible():
                        verify_detected = True
                        break

                if verify_detected:
                    await self._broadcast("log", {"level": "WARN", "message": "⚠️ 检测到需要输入验证码"})
                    # 尝试点击「获取验证码」按钮
                    for btn_sel in ["text=获取验证码", "text=发送验证码", "text=获取", "button:has-text('获取')", "button:has-text('发送')", "a:has-text('获取验证码')", "a:has-text('发送验证码')"]:
                        try:
                            btn = await self._page.query_selector(btn_sel)
                            if btn and await btn.is_visible():
                                await btn.click()
                                await self._broadcast("log", {"level": "INFO", "message": "📩 已点击获取验证码，请查看手机短信"})
                                await asyncio.sleep(2)
                                break
                        except Exception:
                            continue
                    # 截取验证码区域
                    verify_img = await self._capture_verification_image()
                    await self._broadcast("verification_required", {
                        "message": "请输入验证码",
                        "image": verify_img
                    })
                    return False

            except Exception:
                pass
            await asyncio.sleep(interval)

        self.cleanup_qr()
        await self._broadcast("log", {"level": "ERROR", "message": "⏰ 登录超时，请重试"})
        await self._broadcast("login_status", {"status": "timeout"})
        return False

    async def _capture_verification_image(self) -> str:
        if not self._page:
            return ""
        try:
            for sel in ["img[src*='verify']", "img[src*='captcha']", ".verify-img img"]:
                el = await self._page.query_selector(sel)
                if el:
                    shot = await el.screenshot()
                    if shot and len(shot) > 200:
                        return base64.b64encode(shot).decode()
            shot = await self._page.screenshot()
            return base64.b64encode(shot).decode() if shot else ""
        except Exception:
            return ""

    async def submit_verification_code(self, code: str) -> bool:
        """提交验证码：填入输入框 → 寻找确认按钮"""
        if not self._page:
            return False
        try:
            # 找到验证码输入框
            textarea = None
            for sel in ["input[placeholder*='验证码']", "input[name*='code']", "input[name*='verify']", "#verifyCode", "input[type='text']"]:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    textarea = el
                    break

            if not textarea:
                await self._broadcast("log", {"level": "ERROR", "message": "未找到验证码输入框"})
                return False

            # 填入验证码
            await textarea.click()
            await asyncio.sleep(0.2)
            await textarea.fill("")
            await textarea.fill(code)
            await asyncio.sleep(0.5)

            # 寻找确认/提交/登录按钮
            clicked = False
            for sel in [
                "button:has-text('确认')", "button:has-text('确定')", "button:has-text('提交')",
                "button:has-text('验证')", "button:has-text('登录')", "button[type='submit']",
                "a:has-text('确认')", "a:has-text('确定')", "a:has-text('提交')",
            ]:
                try:
                    btn = await self._page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                await self._page.keyboard.press("Enter")

            await asyncio.sleep(3)

            # 检查是否登录成功
            if "login" not in self._page.url.lower() and "wework_admin/frame" in self._page.url:
                self._logged_in = True
                self.cleanup_qr()
                await self._save_login_state()
                await self._broadcast("log", {"level": "SUCCESS", "message": "✅ 验证码验证成功，登录完成"})
                await self._broadcast("login_status", {"status": "success"})
                return True

            await self._broadcast("log", {"level": "INFO", "message": "验证码已提交，等待结果..."})
            return True

        except Exception as e:
            await self._broadcast("log", {"level": "ERROR", "message": f"提交验证码失败: {e}"})
        return False

    async def get_logged_in_page(self) -> tuple:
        """获取已登录的页面，优先复用已有context"""
        # 优先复用现有context
        if self._context and self._page:
            try:
                url = self._page.url
                if "login" not in url.lower():
                    return self._page, self._context, True
            except Exception:
                pass

        # 创建新context（加载保存的cookie）
        await self.init_browser()
        self._context = await self._create_context(use_saved_state=True)
        self._page = await self._context.new_page()
        await self._page.goto(WECHAT_ADMIN_URL, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2)

        if "login" in self._page.url.lower():
            await self._context.close()
            self._context = None
            self._page = None
            return None, None, False

        return self._page, self._context, True

    async def _close_context(self):
        try:
            if self._page:
                await self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        self._page = None
        self._context = None

    async def close(self):
        await self._close_context()
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
