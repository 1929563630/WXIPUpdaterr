"""
企业微信可信IP自动更新器 - 配置管理
所有路径使用相对路径，支持任意设备部署
"""
import os
import sys

# 项目根目录（main.py 所在目录）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 数据目录（运行时自动生成）
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# 浏览器登录态文件
AUTH_STATE_FILE = os.path.join(DATA_DIR, "auth_state.json")

# 数据库文件
DB_FILE = os.path.join(DATA_DIR, "updater.db")

# 日志文件
LOG_FILE = os.path.join(DATA_DIR, "updater.log")

# 二维码图片
QR_IMAGE_FILE = os.path.join(DATA_DIR, "qr_code.png")

# Web 服务端口（可通过环境变量覆盖）
WEB_PORT = int(os.environ.get("UPDATER_PORT", 18800))

# 企业微信管理后台 URL
WECHAT_LOGIN_URL = "https://work.weixin.qq.com/wework_admin/loginpage_wx?from=myhome"
WECHAT_ADMIN_URL = "https://work.weixin.qq.com/wework_admin/frame"

# 公网IP查询接口列表（容灾，按优先级排序）
IP_QUERY_APIS = [
    ("https://ddns.oray.com/checkip", "text"),
    ("http://v4.66666.host:66/ip", "text"),
    ("https://myip.ipip.net", "text"),
    ("http://v4.666666.host:66/ip", "text"),
    ("https://4.ipw.cn", "text"),
    ("https://ip.3322.net", "text"),
]

# IP检测间隔（秒）
IP_CHECK_INTERVAL = 300  # 5分钟

# 用户配置文件
USER_CONFIG_FILE = os.path.join(DATA_DIR, "user_config.json")


def load_user_config() -> dict:
    """加载用户配置"""
    if os.path.exists(USER_CONFIG_FILE):
        try:
            with open(USER_CONFIG_FILE, "r", encoding="utf-8") as f:
                import json
                return json.load(f)
        except Exception:
            pass
    return {}


def save_user_config(config: dict):
    """保存用户配置"""
    import json
    with open(USER_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
