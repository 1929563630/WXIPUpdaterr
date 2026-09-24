"""
浏览器登录管理 - Playwright 自动化
负责：扫码登录、Cookie持久化、验证码交互、Cookie保活
"""
import asyncio
import os
import json
import base64
import time
import shutil
from datetime import datetime
from typing import Optional, Callable, Awaitable
from playwright.async_api import async_playwright, Page, BrowserContext, Browser

from config import AUTH_STATE_FILE, WECHAT_LOGIN_URL, WECHAT_ADMIN_URL, DATA_DIR

QR_IMAGE_FILE = os.path.join(DATA_DIR, "qr_code.png")


class BrowserManager:
    """浏览器管理器"""

    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._logged_in = False
        self._login_checked = False
        self._ws_broadcast: Optional[Callable] = None
        self._login_lock = asyncio.Lock()
        self._verify_notified = False
        self._verify_submitted = False

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
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--lang=zh-CN"]
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

    async def _save_login_state(self, silent: bool = False):
        """保存当前 context 的 Cookie 到文件，覆盖前自动备份"""
        if not self._context:
            return
        try:
            # 覆盖前先备份
            if os.path.exists(AUTH_STATE_FILE):
                try:
                    shutil.copy2(AUTH_STATE_FILE, AUTH_STATE_FILE + ".bak")
                except Exception:
                    pass

            await self._context.storage_state(path=AUTH_STATE_FILE)
            if not silent:
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

    async def keepalive(self) -> bool:
        """
        Cookie 保活：访问企业微信后台，让服务器下发新 Cookie 并保存。

        返回：
        - True  登录态仍有效，Cookie 已刷新并写回文件
        - False 登录态已失效，不做保存
        """
        try:
            await self.init_browser()

            # 优先复用现有 context（避免每次重建）
            if not self._context or not self._page:
                self._context = await self._create_context(use_saved_state=True)
                self._page = await self._context.new_page()

            await self._page.goto(WECHAT_ADMIN_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)

            url = self._page.url
            valid = "login" not in url.lower()

            if valid:
                # 保存新的 Cookie 到文件（静默，避免日志刷屏）
                await self._save_login_state(silent=True)
                self._logged_in = True
                return True
            else:
                self._logged_in = False
                return False
        except Exception as e:
            await self._broadcast("log", {"level": "WARN", "message": f"Cookie保活异常: {e}"})
            # 出错时保留已有状态，不轻易判定失效
            return bool(self._logged_in)

    async def start_login(self) -> dict:
        async with self._login_lock:
            await self._broadcast("log", {"level": "INFO", "message": "正在启动浏览器..."})
            self._verify_notified = False
            self._verify_submitted = False
            try:
                await self._close_context()
                self._context = await self._create_context(use_saved_state=False)
                self._page = await self._context.new_page()

                await self._page.goto(WECHAT_LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(3)

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
        if not self._page:
            return ""
        img_bytes = None
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
        try:
            if os.path.exists(QR_IMAGE_FILE):
                os.remove(QR_IMAGE_FILE)
        except Exception:
            pass

    async def _find_in_frames(self, selectors, require_visible: bool = True):
        if not self._page:
            return None
        for frame in self._page.frames:
            for sel in selectors:
                try:
                    el = await frame.query_selector(sel)
                    if el:
                        if not require_visible:
                            return el
                        try:
                            if await el.is_visible():
                                return el
                        except Exception:
                            continue
                except Exception:
                    continue
        return None

    async def _find_verify_input(self):
        if not self._page:
            return None
        el = await self._find_in_frames([
            "input[placeholder*='验证码']",
            "input[placeholder*='短信']",
            "input[placeholder*='校验']",
            "input[name*='code']",
            "input[name*='verify']",
            "input[name*='smsCode']",
            "input[id*='verify']",
            "input[id*='code']",
            "#verifyCode",
        ])
        if el:
            return el
        for frame in self._page.frames:
            try:
                candidates = await frame.query_selector_all(
                    "input, textarea, [contenteditable='true']"
                )
                for inp in candidates:
                    try:
                        if not await inp.is_visible():
                            continue
                        itype = (await inp.get_attribute("type") or "text").lower()
                        if itype in ("hidden", "checkbox", "radio", "submit", "button", "file"):
                            continue
                        return inp
                    except Exception:
                        continue
            except Exception:
                continue
        for frame in self._page.frames:
            try:
                handle = await frame.evaluate_handle("""
                    () => {
                        function walk(root) {
                            const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                            for (const el of all) {
                                if (el.shadowRoot) {
                                    const inner = el.shadowRoot.querySelectorAll(
                                        'input, textarea, [contenteditable]'
                                    );
                                    for (const i of inner) {
                                        const r = i.getBoundingClientRect();
                                        if (r.width > 0 && r.height > 0) return i;
                                    }
                                    const r = walk(el.shadowRoot);
                                    if (r) return r;
                                }
                            }
                            return null;
                        }
                        return walk(document);
                    }
                """)
                el = handle.as_element()
                if el:
                    return el
            except Exception:
                continue
        return None

    async def _find_submit_button(self):
        if not self._page:
            return None
        el = await self._find_in_frames([
            "button:has-text('确认')", "button:has-text('确定')",
            "button:has-text('提交')", "button:has-text('验证')",
            "button:has-text('登录')", "button:has-text('下一步')",
            "button:has-text('继续')", "button[type='submit']",
            "a:has-text('确认')", "a:has-text('确定')",
            "a:has-text('提交')", "a:has-text('验证')",
            "a:has-text('登录')", "a:has-text('下一步')",
            "div[role='button']:has-text('确认')",
            "div[role='button']:has-text('提交')",
            "div[role='button']:has-text('验证')",
            "div[role='button']:has-text('登录')",
            "div[role='button']:has-text('下一步')",
            ".btn-primary", ".submit-btn", ".confirm-btn",
        ])
        if el:
            return el
        keywords = ["确认", "确定", "提交", "验证", "登录", "下一步", "继续"]
        for frame in self._page.frames:
            try:
                btns = await frame.query_selector_all("button, [role='button'], a")
                for b in btns:
                    try:
                        if not await b.is_visible():
                            continue
                        text = (await b.inner_text() or "").strip()
                        if text and any(k in text for k in keywords):
                            return b
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    async def _dump_debug_info(self):
        if not self._page:
            return
        info = []
        for i, frame in enumerate(self._page.frames):
            try:
                info.append(f"[frame{i}] url={frame.url[:100]}")
            except Exception:
                pass
            try:
                inputs = await frame.query_selector_all("input, textarea")
                for inp in inputs:
                    try:
                        if not await inp.is_visible():
                            continue
                        name = await inp.get_attribute("name")
                        pid = await inp.get_attribute("id")
                        ph = await inp.get_attribute("placeholder")
                        itype = await inp.get_attribute("type")
                        info.append(f"  input name={name} id={pid} type={itype} ph={ph}")
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                btns = await frame.query_selector_all("button, [role='button'], a")
                for b in btns:
                    try:
                        if not await b.is_visible():
                            continue
                        t = (await b.inner_text() or "").strip()[:25]
                        if t:
                            info.append(f"  btn '{t}'")
                    except Exception:
                        pass
            except Exception:
                pass
        if info:
            await self._broadcast("log", {"level": "INFO", "message": "调试: " + " || ".join(info[:50])})
        else:
            await self._broadcast("log", {"level": "INFO", "message": "调试: 未找到任何可见元素"})

    async def _detect_verify_needed(self):
        verify_btn = await self._find_in_frames([
            "text=获取验证码", "text=发送验证码",
            "button:has-text('获取验证码')", "button:has-text('发送验证码')",
            "button:has-text('获取')", "button:has-text('发送')",
            "a:has-text('获取验证码')", "a:has-text('发送验证码')",
            "a:has-text('获取')", "a:has-text('发送')",
        ])
        verify_input = await self._find_verify_input()
        return (verify_btn is not None or verify_input is not None), verify_btn, verify_input

    async def poll_login_status(self, max_attempts: int = 150, interval: int = 2):
        scanned = False
        expired_notified = False
        debug_dumped = False
        for attempt in range(max_attempts):
            if not self._page:
                break
            try:
                url = self._page.url

                if "wework_admin/frame" in url and "login" not in url.lower():
                    self._logged_in = True
                    self.cleanup_qr()
                    try:
                        await self._page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    await self._save_login_state()
                    await self._broadcast("log", {"level": "SUCCESS", "message": "✅ 登录成功，登录态已保存"})
                    await self._broadcast("login_status", {"status": "success"})
                    self._verify_notified = False
                    self._verify_submitted = False
                    return True

                if not expired_notified and not self._verify_notified:
                    try:
                        body = await self._page.inner_text("body")
                        if any(k in body for k in ["二维码已失效", "二维码已过期", "二维码失效"]):
                            expired_notified = True
                            self.cleanup_qr()
                            await self._broadcast("log", {"level": "WARN", "message": "⏰ 二维码已过期，请重新点击扫码登录"})
                            await self._broadcast("login_status", {"status": "expired"})
                            return False
                    except Exception:
                        pass

                if not scanned:
                    try:
                        body_text = await self._page.inner_text("body")
                        for hint in ["确认登录", "请在手机", "已扫码", "企业微信确认"]:
                            if hint in body_text:
                                scanned = True
                                await self._broadcast("log", {"level": "INFO", "message": "📱 已检测到扫码，请在手机端点击确认登录"})
                                await self._broadcast("login_status", {"status": "scanned"})
                                break
                    except Exception:
                        pass

                need_verify, verify_btn, verify_input = await self._detect_verify_needed()

                if need_verify and not self._verify_notified:
                    self._verify_notified = True
                    await self._broadcast("log", {"level": "WARN", "message": "⚠️ 检测到需要输入验证码"})

                    if verify_btn:
                        try:
                            await verify_btn.click()
                            await self._broadcast("log", {"level": "INFO", "message": "📩 已点击获取验证码，请查看手机短信"})
                            await asyncio.sleep(2)
                        except Exception as e:
                            await self._broadcast("log", {"level": "WARN", "message": f"点击获取验证码失败: {e}"})

                    if not debug_dumped:
                        debug_dumped = True
                        await self._dump_debug_info()

                    verify_img = await self._capture_verification_image()
                    await self._broadcast("verification_required", {
                        "message": "请输入验证码",
                        "image": verify_img
                    })

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
            for frame in self._page.frames:
                for sel in ["img[src*='verify']", "img[src*='captcha']",
                            ".verify-img img", ".captcha-img img"]:
                    try:
                        el = await frame.query_selector(sel)
                        if el:
                            shot = await el.screenshot()
                            if shot and len(shot) > 200:
                                return base64.b64encode(shot).decode()
                    except Exception:
                        continue
            shot = await self._page.screenshot()
            return base64.b64encode(shot).decode() if shot else ""
        except Exception:
            return ""

    async def submit_verification_code(self, code: str) -> bool:
        if not self._page:
            return False
        try:
            textarea = await self._find_verify_input()
            if not textarea:
                await self._dump_debug_info()
                await self._broadcast("log", {"level": "ERROR", "message": "未找到验证码输入框（已打印调试信息，请看上一行）"})
                return False

            await textarea.click()
            await asyncio.sleep(0.2)
            try:
                await textarea.fill("")
            except Exception:
                pass
            await asyncio.sleep(0.2)
            await textarea.type(code, delay=100)
            await asyncio.sleep(0.5)
            await self._broadcast("log", {"level": "INFO", "message": f"已填入验证码: {code}"})

            btn = await self._find_submit_button()
            clicked = False
            if btn:
                try:
                    await btn.click()
                    clicked = True
                    await self._broadcast("log", {"level": "INFO", "message": "已点击提交按钮"})
                except Exception:
                    try:
                        await btn.evaluate("el => el.click()")
                        clicked = True
                        await self._broadcast("log", {"level": "INFO", "message": "已用 JS 点击提交按钮"})
                    except Exception:
                        pass
            if not clicked:
                await self._broadcast("log", {"level": "WARN", "message": "未找到提交按钮，尝试回车提交"})
                await textarea.press("Enter")

            await self._broadcast("log", {"level": "INFO", "message": "验证码已提交，等待结果..."})
            await asyncio.sleep(3)

            if "login" not in self._page.url.lower() and "wework_admin/frame" in self._page.url:
                self._logged_in = True
                self.cleanup_qr()
                await self._save_login_state()
                await self._broadcast("log", {"level": "SUCCESS", "message": "✅ 验证码验证成功，登录完成"})
                await self._broadcast("login_status", {"status": "success"})
                self._verify_submitted = True
                return True

            need_verify, _, _ = await self._detect_verify_needed()
            if need_verify:
                await self._broadcast("log", {"level": "WARN", "message": "验证码可能错误或未通过，请重新输入"})
                self._verify_notified = False
                return False
            else:
                self._verify_submitted = True
                await self._broadcast("log", {"level": "INFO", "message": "验证码已提交，等待页面跳转..."})
                return True

        except Exception as e:
            await self._broadcast("log", {"level": "ERROR", "message": f"提交验证码失败: {e}"})
        return False

    async def get_logged_in_page(self) -> tuple:
        if self._context and self._page:
            try:
                url = self._page.url
                if "login" not in url.lower():
                    return self._page, self._context, True
            except Exception:
                pass

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
