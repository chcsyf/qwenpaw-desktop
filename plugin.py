"""
QwenPaw Web 远程桌面插件（qwenpaw-desktop）v0.1.0
（qwenpaw.platform.agentscope.io 专用插件）

在 QwenPaw 界面内实时查看**服务器上的虚拟桌面**，鼠标/键盘操作完整映射到远程
桌面（noVNC 客户端 + Xvfb 虚拟屏幕 + openbox 窗口管理器 + x11vnc）。

架构：
  Xvfb :99（虚拟屏幕 1440x900）
    ├─ openbox（窗口管理器，窗口可拖拽/缩放）
    └─ x11vnc :99 -rfbport 5900（VNC 服务，仅监听 127.0.0.1）
        └─ 插件内置 WebSocket 转发 /api/qwenpaw-desktop/vnc ⇄ localhost:5900
            └─ 前端 noVNC（/api/qwenpaw-desktop/novnc/ 静态资源）连接

界面：
  - 纯桌面视图（无浏览器工具栏），桌面右下角竖排快捷入口
    （GitHub / Google / Bing / 百度 / 终端 / 文件）
  - 底部最小状态条：连接状态、重连、关闭桌面
  - 桌面端物理键盘直接可用；远程 X 桌面强制开启 NumLock，
    保证 noVNC 右侧数字小键盘（KP_ 系列 keysym）正常工作

安全：
  - x11vnc 只监听 127.0.0.1，外部不可达；唯一入口是插件的同源 WS 路由
    （复用 QwenPaw 登录态），不暴露额外端口
  - /open 只允许 http/https URL（file://、javascript: 等拒绝）

进程生命周期（单例，懒启动/手动关闭）：
  - 首次连接 /open /launch 时自动启动 Xvfb + openbox + x11vnc
  - 界面可手动「关闭桌面」释放资源；关闭后下次操作自动重启
  - 插件主服务退出时（shutdown hook）自动清理子进程
"""
import asyncio
import logging
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from qwenpaw.pawapp import PawApp

logger = logging.getLogger(__name__)

PLUGIN_VERSION = "0.1.0"
PLUGIN_NAME = "远程桌面"
PLUGIN_ID = "qwenpaw-desktop"

DISPLAY = ":99"
SCREEN_SIZE = "1440x900x24"
VNC_HOST = "127.0.0.1"
VNC_PORT = 5900
NOVNC_DIR = "/usr/share/novnc"
CHROMIUM = "/usr/bin/chromium"
CHROME_DATA = "/tmp/qwenpaw-desktop-chrome"
# 快捷入口图标：优先用插件自带的 assets/icons/，缺失时 fallback 到 /root/.icons/
ICON_DIRS = [Path(__file__).parent / "assets" / "icons", Path("/root/.icons")]

# 桌面应用启动映射
DESKTOP_APPS = {
    "terminal": ["xfce4-terminal", "--title=终端", "--geometry=140x45"],
    "files": ["thunar"],
    "chromium": [
        CHROMIUM, "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
        f"--user-data-dir={CHROME_DATA}",
        "--no-first-run", "--no-default-browser-check",
        "--window-size=1400,860", "--window-position=0,0",
        "https://www.google.com",
    ],
}

# 全局单例状态
_started_at = None
_procs = {}  # name -> subprocess.Popen


# ---------- 子进程管理 ----------

def _popen(cmd: list, name: str):
    """启动一个子进程（detach 会话，不随调用者结束）。"""
    global _procs
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _procs[name] = proc
    logger.info("[qwenpaw-desktop] started %s pid=%s cmd=%s", name, proc.pid, cmd[0])
    return proc


def _is_alive(name: str) -> bool:
    proc = _procs.get(name)
    return proc is not None and proc.poll() is None


def _vnc_ready() -> bool:
    try:
        with socket.create_connection((VNC_HOST, VNC_PORT), timeout=1):
            return True
    except OSError:
        return False


def _ensure_numlock() -> None:
    """确保远程 X 桌面 NumLock 开启。

    noVNC 对 NumPad 数字键（location=3）发送 KP_ 系列 keysym
    （KP_0..KP_9），x11vnc 将其投递到远程 NumPad 物理键；若远程
    NumLock 关闭（Xvfb 默认状态），这些键按 NumLock-off 语义输出
    （Insert/End/方向键等），导致右侧数字小键盘"不起作用"。
    强制 NumLock 开启后 KP_ 系列 keysym 正确产生数字字符。
    """
    try:
        env = dict(os.environ)
        env["DISPLAY"] = DISPLAY
        out = subprocess.run(
            ["xset", "q"], env=env, capture_output=True, text=True, timeout=5
        ).stdout
        numlock_on = any("Num Lock" in line and "off" not in line
                         for line in out.splitlines())
        if not numlock_on:
            subprocess.run(
                ["xdotool", "key", "Num_Lock"], env=env, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            logger.info("[qwenpaw-desktop] NumLock enabled")
    except Exception:  # noqa: BLE001
        logger.warning("[qwenpaw-desktop] NumLock check failed", exc_info=True)


def _ensure_desktop():
    """懒启动虚拟桌面（Xvfb + openbox + x11vnc），单例幂等。"""
    global _started_at
    if _is_alive("xvfb") and _is_alive("x11vnc") and _vnc_ready():
        return
    # Xvfb 虚拟屏幕
    if not _is_alive("xvfb"):
        _popen(["Xvfb", DISPLAY, "-screen", "0", SCREEN_SIZE, "-nolisten", "tcp"], "xvfb")
        time.sleep(1.5)
    # openbox 窗口管理器（失败不影响核心功能，但窗口不能拖动）
    if not _is_alive("openbox"):
        try:
            env = dict(os.environ)
            env["DISPLAY"] = DISPLAY
            proc = subprocess.Popen(
                ["openbox"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            _procs["openbox"] = proc
        except Exception:  # noqa: BLE001
            logger.exception("[qwenpaw-desktop] openbox failed")
    # 远程 X 桌面开启 NumLock（保证 noVNC NumPad 数字键工作）
    _ensure_numlock()
    # x11vnc 服务（仅 localhost）
    if not _is_alive("x11vnc"):
        _popen([
            "x11vnc", "-display", DISPLAY, "-rfbport", str(VNC_PORT),
            "-localhost", "-shared", "-forever", "-nopw", "-quiet",
        ], "x11vnc")
        for _ in range(20):
            if _vnc_ready():
                break
            time.sleep(0.3)
    if _started_at is None:
        _started_at = time.time()


def _stop_desktop():
    """关闭桌面（释放资源）；下次操作自动重启。"""
    global _started_at, _procs
    for name in ("x11vnc", "openbox", "xvfb"):
        proc = _procs.get(name)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
    # 关掉桌面里打开的 chromium（若有）
    try:
        subprocess.run(
            ["pkill", "-f", CHROME_DATA],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        )
    except Exception:  # noqa: BLE001
        pass
    _procs = {}
    _started_at = None


# ---------- 桌面应用 ----------

def _normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("URL 不能为空")
    if "://" in raw or raw.startswith(("javascript:", "data:", "about:")):
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"仅支持 http/https 协议: {parsed.scheme}")
        return raw
    return "https://" + raw


def open_in_desktop(url: str) -> dict:
    """在虚拟桌面里用 chromium 打开 URL（普通窗口，带地址栏）。"""
    u = _normalize_url(url)
    _ensure_desktop()
    env = dict(os.environ)
    env["DISPLAY"] = DISPLAY
    proc = subprocess.Popen(
        [
            CHROMIUM,
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-gpu",
            f"--user-data-dir={CHROME_DATA}",
            "--no-first-run", "--no-default-browser-check",
            "--window-size=1400,860", "--window-position=0,0",
            u,
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _procs["chromium"] = proc
    logger.info("[qwenpaw-desktop] chromium opened %s pid=%s", u, proc.pid)
    return {"ok": True, "url": u, "pid": proc.pid}


def launch_in_desktop(app: str) -> dict:
    """在虚拟桌面启动一个桌面应用（terminal / files / chromium）。"""
    cmd = DESKTOP_APPS.get(app)
    if cmd is None:
        raise ValueError(f"未知应用: {app}（可用: {', '.join(DESKTOP_APPS)}）")
    _ensure_desktop()
    env = dict(os.environ)
    env["DISPLAY"] = DISPLAY
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _procs[app] = proc
    logger.info("[qwenpaw-desktop] launched %s pid=%s", app, proc.pid)
    return {"ok": True, "app": app, "pid": proc.pid}


# ---------- HTTP API ----------

router = APIRouter()


class OpenRequest(BaseModel):
    url: str


class LaunchRequest(BaseModel):
    app: str


# 自定义远程桌面页面（纯桌面 + 右下角快捷入口）
DESKTOP_HTML = Path(__file__).parent / "ui" / "desktop.html"


@router.get("/desktop_page")
async def get_desktop_page():
    """返回自定义 noVNC 页面（纯桌面，无浏览器工具栏）。"""
    if not DESKTOP_HTML.exists():
        raise HTTPException(status_code=404, detail="desktop.html 未找到")
    return FileResponse(str(DESKTOP_HTML), media_type="text/html")


@router.get("/icon")
async def get_icon(name: str):
    """返回快捷入口图标（优先插件自带 assets/icons/，fallback /root/.icons/）。"""
    if name in ("", ".") or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="非法图标名")
    for d in ICON_DIRS:
        path = d / f"{name}.png"
        if path.exists():
            return FileResponse(str(path))
    raise HTTPException(status_code=404, detail=f"图标不存在: {name}")


@router.get("/status")
async def get_status():
    return {
        "ok": True,
        "name": PLUGIN_NAME,
        "version": PLUGIN_VERSION,
        "type": "desktop",
        "display": DISPLAY,
        "screen": SCREEN_SIZE,
        "vnc_port": VNC_PORT,
        "running": _vnc_ready(),
        "started_at": _started_at,
        "uptime_sec": int(time.time() - _started_at) if _started_at else None,
        "procs": {k: (v.pid if v.poll() is None else None) for k, v in _procs.items()},
        "lifecycle": {
            "single_process": True,
            "background_running": True,
            "manual_close": True,
            "auto_restart": True,
        },
    }


@router.post("/open")
async def open_page(req: OpenRequest):
    """在虚拟桌面打开 URL（chromium 窗口）。"""
    try:
        return open_in_desktop(req.url)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[qwenpaw-desktop] open failed")
        return {"ok": False, "error": str(exc)}


@router.post("/launch")
async def launch_page(req: LaunchRequest):
    """在虚拟桌面启动桌面应用（terminal / files / chromium）。"""
    try:
        return launch_in_desktop(req.app)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[qwenpaw-desktop] launch failed")
        return {"ok": False, "error": str(exc)}


@router.post("/close")
async def close_desktop():
    """手动关闭桌面，释放资源；下次操作自动重启。"""
    _stop_desktop()
    return {"ok": True, "message": "远程桌面已关闭，资源已释放；下次打开会自动重启"}


# ---------- WebSocket：VNC 转发（同源） ----------

@router.websocket("/vnc")
async def vnc_ws(ws: WebSocket):
    """把前端 noVNC 的 WebSocket 字节流转发到本地 VNC 服务（5900）。

    noVNC 客户端通过 ws(s)://host:port/api/qwenpaw-desktop/vnc 连接，
    该路由与 QwenPaw 同源（复用登录态），不暴露额外端口。
    RFB 是二进制字节流，这里做纯透明双向转发。
    """
    await ws.accept()
    _ensure_desktop()
    reader, writer = None, None
    try:
        reader, writer = await asyncio.open_connection(VNC_HOST, VNC_PORT)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[qwenpaw-desktop] vnc connect failed")
        try:
            await ws.send_text("error: VNC 连接失败: %s" % exc)
        except Exception:  # noqa: BLE001
            pass
        await ws.close()
        return

    async def ws_to_tcp():
        """WebSocket → VNC：前端发来的帧写入 TCP。"""
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                payload = msg.get("bytes")
                if payload is None:
                    payload = (msg.get("text") or "").encode("latin1")
                if payload:
                    writer.write(payload)
                    await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            _safe_close_writer(writer)

    async def tcp_to_ws():
        """VNC → WebSocket：TCP 读到的数据发给前端。"""
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await ws.send_bytes(data)
        except Exception:  # noqa: BLE001
            pass
        finally:
            _safe_close_writer(writer)

    tasks = [asyncio.ensure_future(ws_to_tcp()), asyncio.ensure_future(tcp_to_ws())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in done:
        try:
            await t
        except Exception:  # noqa: BLE001
            pass
    _safe_close_writer(writer)


def _safe_close_writer(writer):
    try:
        if writer is not None:
            writer.close()
    except Exception:  # noqa: BLE001
        pass


# ---------- noVNC 静态资源 ----------

router.mount("/novnc", StaticFiles(directory=NOVNC_DIR), name="novnc")


# ============ 应用注册 ============

app = PawApp(name=PLUGIN_NAME, app_id=PLUGIN_ID)
app.include_router(router)


@app.hook("shutdown")
async def _shutdown() -> None:
    """主服务退出时清理桌面进程，避免残留。"""
    _stop_desktop()
    logger.info("[qwenpaw-desktop] Plugin stopped")


# REQUIRED: 模块级 plugin 实例（PawApp 导出为 'app'；loader 同时接受 'plugin'）
plugin = app
