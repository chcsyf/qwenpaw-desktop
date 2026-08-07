# 变更记录

## [0.0.1] — 2026-08-07（首个发布版本）

首个发布版本。专用于 `qwenpaw.platform.agentscope.io` 平台。
应用启动入口：**应用 → 远程桌面** 🖥️

⚠️ 安装前需在服务器手动安装系统依赖包（插件不会自动安装系统包）：
`xvfb`、`openbox`、`x11vnc`、`xdotool`、`x11-xserver-utils`、`novnc`、
`xfce4-terminal`、`thunar`、`chromium`（详见 README.md）。

功能：
- 纯桌面视图（无浏览器工具栏），右下角竖排快捷入口：GitHub / Google / Bing / 百度 / 终端 / 文件
- 底部最小状态条：连接状态、重连、关闭桌面
- 桌面端物理键盘直接可用；远程 X 桌面强制开启 NumLock，数字小键盘正常
- 适合访问本地打不开的网站（如 github）：在服务器桌面里以 chromium 窗口打开
