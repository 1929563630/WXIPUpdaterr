"""
通知服务 - 多渠道 + WebSocket 推送

支持的渠道（在下面的「通知渠道配置区」修改 enabled 和地址即可）：
- Telegram（含代理）
- Bark
- 企业微信群机器人
- 企业微信自建应用
- 钉钉
- 飞书
- Server 酱
- PushPlus
- Go-WXPush / WXPush
- WxPusher
- QQ 机器人

对外提供的函数：
- send_notification      应用更新结果通知
- notify_startup         启动成功通知
- notify_startup_failed  启动失败通知
- notify_login_expired   登录态失效通知
- notify_ip_changed      检测到IP变化通知
- notify_job_error       定时任务异常通知
- notify_status_report   定期运行状态汇报

容错原则：任何渠道失败都只打印警告，不影响主流程，也不影响其他渠道。
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime

from database import add_log

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


# ======================================================================
#                            通知渠道配置区
# ======================================================================
# 每个渠道一个字典，改 enabled 为 True 并填好地址就会自动推送。
# 用不到的渠道留着 enabled=False 即可，不会产生任何请求。
# 字段值都写成字符串，'' 表示没填，脚本会自动跳过。
# ======================================================================

# --- Telegram --------------------------------------------------------
TELEGRAM = {
    'enabled': False,      # True/False
    'botToken': '',       # 必填：Bot Token
    'chatId': '',         # 必填：Chat ID
    'proxy': 'http://192.168.2.30:7890',          # 可选：如 http://127.0.0.1:7890，留空则直连
}

# --- Bark（iOS 推送）--------------------------------------------------
BARK = {
    'enabled': False,     # True/False
    'url': '',            # 必填：https://api.day.app/你的Key
    'group': '',          # 可选
    'sound': '',          # 可选
    'icon': '',           # 可选
    'level': '',          # 可选
}

# --- 企业微信群机器人 --------------------------------------------------
WECOM = {
    'enabled': False,     # True/False
    'webhook': '',        # 必填：https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
}

# --- 企业微信自建应用 --------------------------------------------------
WECOM_APP = {
    'enabled': False,     # True/False
    'corpId': '',         # 必填：企业ID
    'agentId': '',        # 必填：应用ID（数字）
    'secret': '',         # 必填：应用密钥
    'touser': '@all',     # 可选：接收人，默认 @all
}

# --- 钉钉群机器人 -----------------------------------------------------
DINGTALK = {
    'enabled': False,     # True/False
    'webhook': '',        # 必填
    'secret': '',         # 可选：加签密钥 SECxxx
}

# --- 飞书群机器人 -----------------------------------------------------
FEISHU = {
    'enabled': False,     # True/False
    'webhook': '',        # 必填
}

# --- Server 酱 --------------------------------------------------------
SERVERCHAN = {
    'enabled': False,     # True/False
    'sendKey': '',        # 必填：SCTxxxx
}

# --- PushPlus ---------------------------------------------------------
PUSHPLUS = {
    'enabled': False,     # True/False
    'token': '',          # 必填
}

# --- Go-WXPush / WXPush ----------------------------------------------
WXPUSH = {
    'enabled': False,     # True/False
    'url': 'http://192.168.2.30:5566',            # 必填：如 http://192.168.2.30:5566
    'token': '',          # 仅 WXPush（Cloudflare版）填，Go-WXPush 留空
}

# --- WxPusher ---------------------------------------------------------
WXPUSHER = {
    'enabled': False,     # True/False
    'appToken': '',       # 必填：AT_xxxxx
    'uids': '',           # 接收用户 UID，多个用英文逗号分隔
    'topicIds': '',       # 可选
    'summary': '',        # 可选
}

# --- QQ 机器人（官方单聊）---------------------------------------------
QQ = {
    'enabled': False,     # True/False
    'appId': '',          # 必填
    'clientSecret': '',   # 必填
    'openid': '',         # 必填
}


# ======================================================================
#                        以下为内部实现，一般不用改
# ======================================================================

_TG_API = 'https://api.telegram.org/bot{token}/sendMessage'
_MAX_LEN = 3800


def _str(value):
    return str(value).strip() if value not in (None, '') else ''


def _escape_html(text):
    return (
        str(text)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
    )


def _build_proxies(config):
    if not config:
        return None
    proxy = config.get('proxy')
    if isinstance(proxy, str) and proxy.strip():
        url = proxy.strip()
        return {'http': url, 'https': url}
    if isinstance(proxy, dict) and proxy.get('enabled') and proxy.get('url'):
        url = str(proxy['url']).strip()
        if url:
            return {'http': url, 'https': url}
    return None


def _http(name, method, url, **kwargs):
    if requests is None:
        print('⚠️  notify[%s]: 缺少 requests，跳过' % name)
        return None
    kwargs.setdefault('timeout', 30)
    try:
        return requests.request(method, url, **kwargs)
    except Exception as error:  # noqa: BLE001
        print('⚠️  notify[%s]: 请求异常 %s' % (name, error))
        return None


def _report(name, response, predicate):
    if response is None:
        return False
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if response.ok and predicate(payload):
        print('✅ notify[%s]: 已推送' % name)
        return True
    print('⚠️  notify[%s]: 推送失败 %s' % (name, payload or response.status_code))
    return False


# ---------------------------------------------------------------- 各渠道实现

def _send_telegram(section, title, content):
    token = _str(section.get('botToken'))
    chat_id = _str(section.get('chatId'))
    if not token or not chat_id:
        print('ℹ️  notify[telegram]: 未配置 Bot Token 或 Chat ID，跳过')
        return False

    header = '🎯 <b>%s</b>' % _escape_html(title)
    body = _escape_html(content)
    if len(body) > _MAX_LEN:
        body = body[:_MAX_LEN] + '\n…（内容过长已截断）'
    text = '%s\n<pre>%s</pre>' % (header, body)

    response = _http(
        'telegram', 'POST', _TG_API.format(token=token),
        json={
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        },
        proxies=_build_proxies(section),
    )
    return _report('telegram', response, lambda p: p.get('ok'))


def _send_bark(section, title, content):
    url = _str(section.get('url'))
    if not url:
        print('ℹ️  notify[bark]: 未配置 url，跳过')
        return False
    payload = {'title': title, 'body': content}
    for key in ('group', 'sound', 'icon', 'level'):
        value = _str(section.get(key))
        if value:
            payload[key] = value
    response = _http('bark', 'POST', url.rstrip('/') + '/push', json=payload)
    return _report('bark', response, lambda p: p.get('code') == 200)


def _send_wecom(section, title, content):
    webhook = _str(section.get('webhook'))
    if not webhook:
        print('ℹ️  notify[wecom]: 未配置 webhook，跳过')
        return False
    text = '%s\n%s' % (title, content)
    if len(text) > 2000:
        text = text[:2000] + '…'
    response = _http(
        'wecom', 'POST', webhook,
        json={'msgtype': 'text', 'text': {'content': text}},
    )
    return _report('wecom', response, lambda p: p.get('errcode') == 0)


def _send_wecom_app(section, title, content):
    corp_id = _str(section.get('corpId'))
    agent_id = _str(section.get('agentId'))
    secret = _str(section.get('secret'))
    touser = _str(section.get('touser')) or '@all'

    if not corp_id or not agent_id or not secret:
        print('ℹ️  notify[wecom_app]: 未配置 corpId/agentId/secret，跳过')
        return False

    try:
        agent_id_int = int(agent_id)
    except ValueError:
        print('⚠️  notify[wecom_app]: agentId 必须为数字，跳过')
        return False

    token_url = 'https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=%s&corpsecret=%s' % (corp_id, secret)
    token_resp = _http('wecom_app', 'GET', token_url)
    if token_resp is None:
        return False

    try:
        token_payload = token_resp.json() if token_resp.content else {}
    except ValueError:
        token_payload = {}
    if not isinstance(token_payload, dict):
        token_payload = {}

    access_token = _str(token_payload.get('access_token'))
    if not access_token:
        print('⚠️  notify[wecom_app]: 获取 access_token 失败 %s' % (token_payload or token_resp.status_code))
        return False

    send_url = 'https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=%s' % access_token
    text = '%s\n%s' % (title, content)
    if len(text) > 2000:
        text = text[:2000] + '…'

    payload = {
        'touser': touser,
        'msgtype': 'text',
        'agentid': agent_id_int,
        'text': {'content': text},
        'safe': 0,
    }
    response = _http('wecom_app', 'POST', send_url, json=payload)
    return _report('wecom_app', response, lambda p: p.get('errcode') == 0)


def _send_dingtalk(section, title, content):
    webhook = _str(section.get('webhook'))
    secret = _str(section.get('secret'))
    if not webhook:
        print('ℹ️  notify[dingtalk]: 未配置 webhook，跳过')
        return False

    if secret:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = '%s\n%s' % (timestamp, secret)
        digest = hmac.new(
            secret.encode('utf-8'),
            string_to_sign.encode('utf-8'),
            hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        webhook += ('&' if '?' in webhook else '?') + 'timestamp=%s&sign=%s' % (timestamp, sign)

    response = _http(
        'dingtalk', 'POST', webhook,
        json={'msgtype': 'text', 'text': {'content': '%s\n%s' % (title, content)}},
    )
    return _report('dingtalk', response, lambda p: p.get('errcode') == 0)


def _send_feishu(section, title, content):
    webhook = _str(section.get('webhook'))
    if not webhook:
        print('ℹ️  notify[feishu]: 未配置 webhook，跳过')
        return False
    response = _http(
        'feishu', 'POST', webhook,
        json={'msg_type': 'text', 'content': {'text': '%s\n%s' % (title, content)}},
    )
    return _report(
        'feishu', response,
        lambda p: p.get('code') == 0 or p.get('StatusCode') == 0,
    )


def _send_serverchan(section, title, content):
    key = _str(section.get('sendKey')) or _str(section.get('key'))
    if not key:
        print('ℹ️  notify[serverchan]: 未配置 sendKey，跳过')
        return False
    response = _http(
        'serverchan', 'POST', 'https://sctapi.ftqq.com/%s.send' % key,
        data={'title': title[:32], 'desp': content},
    )
    return _report('serverchan', response, lambda p: p.get('code') == 0)


def _send_pushplus(section, title, content):
    token = _str(section.get('token'))
    if not token:
        print('ℹ️  notify[pushplus]: 未配置 token，跳过')
        return False
    response = _http(
        'pushplus', 'POST', 'https://www.pushplus.plus/send',
        json={'token': token, 'title': title, 'content': content, 'template': 'txt'},
    )
    return _report('pushplus', response, lambda p: p.get('code') == 200)


def _send_wxpush(section, title, content):
    base = _str(section.get('url'))
    if not base:
        print('ℹ️  notify[wxpush]: 未配置 url，跳过')
        return False
    params = {'title': title, 'content': content}
    token = _str(section.get('token'))
    if token:
        params['token'] = token
    response = _http('wxpush', 'GET', base.rstrip('/') + '/wxsend', params=params)
    return _report(
        'wxpush', response,
        lambda p: str(p.get('code')) == '200' or str(p.get('errcode')) == '0',
    )


def _send_wxpusher(section, title, content):
    app_token = _str(section.get('appToken'))
    if not app_token:
        print('ℹ️  notify[wxpusher]: 未配置 appToken，跳过')
        return False

    def _split_ids(raw):
        return [item.strip() for item in _str(raw).split(',') if item.strip()]

    uids = _split_ids(section.get('uids'))
    topic_ids = _split_ids(section.get('topicIds'))
    if not uids and not topic_ids:
        print('ℹ️  notify[wxpusher]: 未配置 uids 或 topicIds，跳过')
        return False

    payload = {
        'appToken': app_token,
        'content': content,
        'summary': _str(section.get('summary')) or title,
        'contentType': 1,
    }
    if uids:
        payload['uids'] = uids
    if topic_ids:
        payload['topicIds'] = topic_ids

    response = _http(
        'wxpusher', 'POST',
        'https://wxpusher.zjiecode.com/api/send/message',
        json=payload,
    )
    return _report('wxpusher', response, lambda p: str(p.get('code')) == '1000')


def _send_qq(section, title, content):
    app_id = _str(section.get('appId'))
    client_secret = _str(section.get('clientSecret'))
    openid = _str(section.get('openid'))
    if not app_id or not client_secret or not openid:
        print('ℹ️  notify[qq]: 未配置 appId/clientSecret/openid，跳过')
        return False

    token_resp = _http(
        'qq', 'POST', 'https://bots.qq.com/app/getAppAccessToken',
        json={'appId': app_id, 'clientSecret': client_secret},
    )
    if token_resp is None:
        return False

    try:
        token_payload = token_resp.json() if token_resp.content else {}
    except ValueError:
        token_payload = {}
    if not isinstance(token_payload, dict):
        token_payload = {}

    access_token = _str(token_payload.get('access_token'))
    if not access_token:
        print('⚠️  notify[qq]: 获取 access_token 失败 %s' % (token_payload or token_resp.status_code))
        return False

    text = '%s\n%s' % (title, content)
    if len(text) > 900:
        text = text[:900] + '…'

    response = _http(
        'qq', 'POST',
        'https://api.sgroup.qq.com/v2/users/%s/messages' % openid,
        json={'content': text, 'msg_type': 0},
        headers={'Authorization': 'QQBot %s' % access_token},
    )
    return _report('qq', response, lambda p: True)


# 渠道注册表
_EXTRA_CHANNELS = (
    ('telegram',   TELEGRAM,   _send_telegram),
    ('bark',       BARK,       _send_bark),
    ('wecom',      WECOM,      _send_wecom),
    ('wecom_app',  WECOM_APP,  _send_wecom_app),
    ('dingtalk',   DINGTALK,   _send_dingtalk),
    ('feishu',     FEISHU,     _send_feishu),
    ('serverchan', SERVERCHAN, _send_serverchan),
    ('pushplus',   PUSHPLUS,   _send_pushplus),
    ('wxpush',     WXPUSH,     _send_wxpush),
    ('wxpusher',   WXPUSHER,   _send_wxpusher),
    ('qq',         QQ,         _send_qq),
)


def _multi_send_sync(title, content):
    """同步发送到所有已启用渠道，返回是否至少一个成功"""
    print('\n📢 %s\n%s' % (title, content))
    if requests is None:
        print('⚠️  notify: 缺少 requests，跳过推送')
        return False

    results = []
    for name, section, handler in _EXTRA_CHANNELS:
        if section.get('enabled') is not True:
            continue
        try:
            results.append((name, handler(section, title, content)))
        except Exception as error:  # noqa: BLE001
            print('⚠️  notify[%s]: 异常 %s' % (name, error))
            results.append((name, False))

    if not results:
        print('ℹ️  notify: 没有启用任何通知渠道，跳过推送')
        return False

    ok = [n for n, r in results if r]
    bad = [n for n, r in results if not r]
    print('📮 notify: 成功 [%s] 失败 [%s]' % (', '.join(ok) or '无', ', '.join(bad) or '无'))
    return bool(ok)


async def _multi_channel_push(title: str, content: str):
    """异步调用多通道推送（不阻塞事件循环）"""
    try:
        await asyncio.to_thread(_multi_send_sync, title, content)
    except Exception as e:  # noqa: BLE001
        try:
            add_log("WARN", f"多通道通知失败: {e}")
        except Exception:
            pass


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ 事件通知

async def send_notification(old_ip: str, new_ip: str, results: list, ws_broadcast=None):
    """全部应用更新完成后推送通知（WebSocket + 多通道）"""
    success_count = sum(1 for r in results if r.get("success"))
    total = len(results)
    now = _now()

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
    try:
        add_log("INFO", f"通知:\n{content}")
    except Exception:
        pass

    if ws_broadcast:
        await ws_broadcast("notification", {
            "title": title,
            "content": content,
            "success": success_count == total,
        })

    await _multi_channel_push(title, content)
    return content


async def notify_startup(ws_broadcast=None, port: int = 18800):
    title = "✅ 企业微信IP更新器已启动"
    content = f"时间: {_now()}\n端口: {port}\n状态: 运行中"
    try:
        add_log("INFO", "服务启动成功")
    except Exception:
        pass
    if ws_broadcast:
        await ws_broadcast("notification", {
            "title": title, "content": content, "success": True,
        })
    await _multi_channel_push(title, content)
    return content


async def notify_startup_failed(error_msg: str, ws_broadcast=None):
    title = "❌ 企业微信IP更新器启动失败"
    content = f"时间: {_now()}\n错误: {error_msg}"
    try:
        add_log("ERROR", f"启动失败: {error_msg}")
    except Exception:
        pass
    if ws_broadcast:
        await ws_broadcast("notification", {
            "title": title, "content": content, "success": False,
        })
    await _multi_channel_push(title, content)
    return content


async def notify_login_expired(ws_broadcast=None):
    title = "⚠️ 企业微信登录态已失效"
    content = (
        f"时间: {_now()}\n"
        f"请打开面板重新扫码登录\n"
        f"地址: http://localhost:18800"
    )
    try:
        add_log("WARN", "登录态失效，需要重新扫码")
    except Exception:
        pass
    if ws_broadcast:
        await ws_broadcast("notification", {
            "title": title, "content": content, "success": False,
        })
    await _multi_channel_push(title, content)
    return content


async def notify_ip_changed(old_ip: str, new_ip: str, ws_broadcast=None):
    title = "🌐 检测到公网IP变化"
    content = f"时间: {_now()}\n旧IP: {old_ip}\n新IP: {new_ip}\n正在更新可信IP..."
    if ws_broadcast:
        await ws_broadcast("notification", {
            "title": title, "content": content, "success": True,
        })
    await _multi_channel_push(title, content)
    return content


async def notify_job_error(error_msg: str, ws_broadcast=None):
    title = "❌ 定时检测任务异常"
    content = f"时间: {_now()}\n错误: {error_msg}"
    if ws_broadcast:
        await ws_broadcast("notification", {
            "title": title, "content": content, "success": False,
        })
    await _multi_channel_push(title, content)
    return content


# --------------------------------------------------------------- 定期状态汇报

async def notify_status_report(
    ws_broadcast=None,
    reason: str = "定时汇报",
    scheduler_running=None,
    browser_manager=None,
):
    """
    定期推送运行状态汇报。

    参数：
    - scheduler_running: 由 main.py 传入的真实定时器状态（True/False/None）
    - browser_manager: 由 main.py 传入，用于实时检查登录态
    """
    try:
        from database import get_current_ip, get_apps
        from config import IP_CHECK_INTERVAL
    except Exception as e:
        title = "📊 运行状态汇报"
        content = f"时间: {_now()}\n错误: 读取状态失败 {e}"
        await _multi_channel_push(title, content)
        return content

    try:
        current = get_current_ip() or {}
    except Exception:
        current = {}
    try:
        apps = get_apps() or []
    except Exception:
        apps = []

    cur_ip = current.get("ip", "0.0.0.0")
    last_check = current.get("last_check", "—")

    # 登录态：用传入的 browser_manager 实时检查
    logged_in = False
    if browser_manager is not None:
        try:
            logged_in = await browser_manager.check_login_valid()
            browser_manager._logged_in = logged_in
            browser_manager._login_checked = True
        except Exception:
            logged_in = bool(browser_manager._logged_in)
    login_text = "✅ 有效" if logged_in else "❌ 已失效（请重新扫码）"

    # 应用明细
    app_lines = []
    if apps:
        for a in apps:
            aid = a.get("agent_id", "?")
            app_ip = a.get("current_ip") or "（未配置）"
            status = a.get("status", "pending")
            badge = {"success": "✅", "failed": "❌"}.get(status, "⏳")
            app_lines.append(f"  {badge} {aid} → {app_ip}")
    else:
        app_lines.append("  （未配置任何应用）")

    # 定时任务状态：用传入的参数
    if scheduler_running is True:
        sched_text = "✅ 运行中"
    elif scheduler_running is False:
        sched_text = "⏸ 已停止"
    else:
        sched_text = "—"

    title = f"📊 运行状态汇报（{reason}）"
    lines = [
        f"时间: {_now()}",
        "",
        f"🌐 当前公网IP: {cur_ip}",
        f"🕒 上次检测: {last_check}",
        f"🔐 微信登录态: {login_text}",
        f"⏱ 检测间隔: {IP_CHECK_INTERVAL}s",
        f"📋 定时任务: {sched_text}",
        "",
        f"📱 应用可信IP（共 {len(apps)} 个）:",
    ]
    lines.extend(app_lines)
    content = "\n".join(lines)

    try:
        add_log("INFO", f"状态汇报:\n{content}")
    except Exception:
        pass

    if ws_broadcast:
        await ws_broadcast("notification", {
            "title": title,
            "content": content,
            "success": logged_in,
        })

    await _multi_channel_push(title, content)
    return content


# 便于单独测试
if __name__ == '__main__':
    asyncio.run(notify_status_report(reason="自检"))
