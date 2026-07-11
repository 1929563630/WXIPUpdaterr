"""
公网IP检测服务
多接口容灾，纯文本提取IPv4
"""
import re
import aiohttp
import asyncio
from config import IP_QUERY_APIS


async def get_public_ip() -> str:
    """多接口容灾获取公网IP"""
    timeout = aiohttp.ClientTimeout(total=10)
    headers = {"User-Agent": "Mozilla/5.0"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for url, parse_type in IP_QUERY_APIS:
            try:
                async with session.get(url, ssl=False) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text()
                    text = text.strip()

                    if parse_type == "json":
                        import json
                        data = json.loads(text)
                        ip = data.get("ip", "")
                    else:
                        # 从返回内容中提取第一个IPv4地址
                        match = re.search(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", text)
                        ip = match.group(1) if match else ""

                    # 校验IPv4格式
                    if ip and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                        # 排除内网IP
                        parts = ip.split(".")
                        if parts[0] in ("10", "127") or \
                           (parts[0] == "172" and 16 <= int(parts[1]) <= 31) or \
                           (parts[0] == "192" and parts[1] == "168"):
                            continue
                        return ip
            except Exception:
                continue
    return ""
