# 企业微信可信IP自动更新器

自动检测公网IP变化，实时更新企业微信自建应用的可信IP配置。

## 快速开始（3步）

### 1. 安装

```bash
bash install.sh
```

自动完成：创建虚拟环境 → 安装依赖 → 安装浏览器

### 2. 启动

```bash
bash start.sh
```

### 3. 使用

浏览器打开 **http://localhost:18800**

1. 点击「扫码登录」→ 用微信扫码确认
2. 输入应用AgentId（网址后的数字）
3. 点击「更新」或开启定时检测

---

## 功能

- 📱 扫码登录企业微信管理后台
- 🌐 自动检测公网IP变化（6个接口容灾）
- ⚡ IP变化自动更新所有应用可信IP
- 📊 实时日志、IP变更历史
- 🔑 验证码网页端交互
- 🔄 支持手动单应用更新

## 系统要求

- Python 3.10+
- 200MB 磁盘空间（Chromium 浏览器）
- 网络畅通

## 支持系统

| 系统 | 命令 |
|------|------|
| Ubuntu/Debian | `sudo apt install python3 python3-pip python3-venv` |
| CentOS/RHEL | `sudo yum install python3 python3-pip` |
| macOS | `brew install python@3.11` |
| Windows | 安装 Python 3.10+，用 Git Bash 运行脚本 |

## 自定义端口

```bash
UPDATER_PORT=8080 bash start.sh
```

## 后台运行

```bash
nohup bash start.sh > /dev/null 2>&1 &
```

## 目录结构

```
WeChatIPUpdater/
├── install.sh         # 一键安装
├── start.sh           # 启动脚本
├── main.py            # 主程序
├── config.py          # 配置
├── database.py        # 数据库
├── requirements.txt   # 依赖列表
├── browser/           # 浏览器自动化
│   ├── auth.py        # 登录+Cookie
│   └── ip_updater.py  # IP更新
├── services/          # 服务
│   ├── ip_check.py    # IP检测
│   └── notify.py      # 通知
├── web/static/        # 前端页面
│   ├── index.html
│   └── lib/           # 前端资源
└── data/              # 运行时数据（自动生成）
    ├── auth_state.json
    ├── updater.db
    └── user_config.json
```

## 常见问题

**Q: 安装失败？**
A: 确保 Python 3.10+ 已安装，网络畅通

**Q: 二维码不显示？**
A: 运行 `venv/bin/playwright install chromium`

**Q: Cookie失效？**
A: 重新扫码登录即可

**Q: 如何修改IP检测间隔？**
A: 编辑 `config.py` 中的 `IP_CHECK_INTERVAL`（单位：秒）

## 开发者

**JYONG**

## 许可

MIT License
