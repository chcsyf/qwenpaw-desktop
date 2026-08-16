"""
QwenPaw Web 远程桌面插件（qwenpaw-desktop）v0.1.2
（qwenpaw.platform.agentscope.io 专用插件）

在 QwenPaw 界面内实时查看**服务器上的虚拟桌面**，鼠标/键盘操作完整映射到远程
桌面（noVNC 客户端 + Xvfb 虚拟屏幕 + openbox 窗口管理器 + x11vnc）。

架构：
  Xvfb :99（虚拟屏幕，默认 1440x900，可切换 1280x720 / 1920x1080）
    ├─ openbox（窗口管理器，窗口可拖拽/缩放）
    └─ x11vnc :99 -rfbport 5900（VNC 服务，仅监听 127.0.0.1）
        └─ 插件内置 WebSocket 转发 /api/qwenpaw-desktop/vnc ⇄ localhost:5900
            └─ 前端 noVNC（/api/qwenpaw-desktop/novnc/ 静态资源）连接

界面：
  - 纯桌面视图（无浏览器工具栏），桌面右下角竖排快捷入口
    （GitHub / Google / Bing / 百度 / 终端 / 文件）
  - 右下工具：快捷入口 / 📷 一键截图 / 📋 粘贴到远程 / 🖥 分辨率切换 / 重连 / 关闭桌面
  - 📷 一键截图：截取当前画面，预览 / 下载 PNG / 保存到平台公共数据目录
    plugin_data/screenshots/（公共持久，不属于某个智能体）
  - 剪贴板互通（xclip 读写远程 CLIPBOARD selection，UTF-8 中文无乱码）：
    远程 Ctrl+C 复制 → 自动写入本地剪贴板；本地复制 → 📋 写入远程并自动粘贴
    （终端类窗口自动用 Ctrl+Shift+V，其他用 Ctrl+V；不碰 PRIMARY，
    因此不会"选中文字就自动复制"）
  - 🖥 分辨率切换：1280x720 / 1440x900（默认）/ 1920x1080，重启桌面栈生效
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
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from qwenpaw.pawapp import PawApp

logger = logging.getLogger(__name__)

PLUGIN_VERSION = "0.1.2"
PLUGIN_NAME = "远程桌面"
PLUGIN_ID = "qwenpaw-desktop"

DISPLAY = ":99"
SCREEN_SIZE = "1440x900x24"  # 默认屏幕尺寸（可通过 /resolution 运行时切换）
SCREEN_PRESETS = [
    {"label": "1280x720", "width": 1280, "height": 720},
    {"label": "1440x900", "width": 1440, "height": 900},
    {"label": "1920x1080", "width": 1920, "height": 1080},
]
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
# 当前生效的屏幕尺寸（可变，支持运行时切换分辨率）
_screen_size = SCREEN_SIZE


def _current_screen() -> str:
    """当前生效的屏幕尺寸（如 1440x900x24）。"""
    return _screen_size


def _set_screen(w: int, h: int) -> None:
    """切换虚拟屏幕分辨率：重启桌面栈使新尺寸生效。"""
    global _screen_size
    _stop_desktop()
    _screen_size = f"{w}x{h}x24"
    _ensure_desktop()


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
    # Xvfb 虚拟屏幕（用当前生效的屏幕尺寸，支持运行时切换分辨率）
    if not _is_alive("xvfb"):
        _popen(["Xvfb", DISPLAY, "-screen", "0", _screen_size, "-nolisten", "tcp"], "xvfb")
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


# ---------- 截图 ----------

SHOT_DIR = Path("/tmp/qwenpaw-desktop-shots")


def _capture_screen_png() -> bytes:
    """截取远程桌面当前屏幕，返回 PNG 字节。

    优先用 scrot（ImageMagick 的 import 在部分环境缺失）。
    需要 DISPLAY 指向虚拟屏幕 :99。
    """
    _ensure_desktop()
    scrot = shutil.which("scrot")
    if scrot is None:
        raise RuntimeError("服务器缺少 scrot，无法截图（请先安装：apt install scrot）")
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    out = SHOT_DIR / "shot.png"
    try:
        env = dict(os.environ)
        env["DISPLAY"] = DISPLAY
        subprocess.run(
            [scrot, "-d", "0", "-o", str(out)],
            env=env, timeout=10,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        data = out.read_bytes()
        if not data:
            raise RuntimeError("截图失败（scrot 输出为空）")
        return data
    finally:
        try:
            out.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def _save_screenshot(png: bytes) -> Path:
    """把截图保存到平台公共数据目录 plugin_data/screenshots/。

    plugin_data 位于 QwenPaw 根目录（NAS 持久层）下，是**插件公共数据区**：
    - 公共：不属于任何单个智能体工作区（app 是公共的）
    - 持久：NAS 层，容器重启不丢
    - 平台不碰：不同于 plugins/（安装目录）与 plugin_runtime/（依赖+锁的
      缓存目录），plugin_data 不会被平台管理逻辑清理或重建
    目录不存在时自动创建；解析失败时 fallback 到 /tmp。
    """
    try:
        from qwenpaw.constant import WORKING_DIR

        data_root = Path(WORKING_DIR) / "plugin_data"
    except Exception:  # noqa: BLE001
        logger.warning("[qwenpaw-desktop] WORKING_DIR unavailable, fallback to /tmp",
                       exc_info=True)
        data_root = Path("/tmp")
    save_dir = data_root / "screenshots"
    save_dir.mkdir(parents=True, exist_ok=True)
    fname = f"desktop-{time.strftime('%Y%m%d-%H%M%S')}.png"
    fpath = save_dir / fname
    fpath.write_bytes(png)
    return fpath


# ---------- 剪贴板（X11 selection + xclip，UTF-8） ----------

# 说明：noVNC 的 VNC 剪贴板协议是 Latin-1 编码（x11vnc 0.9.16 不支持
# extended clipboard / UTF-8），中文会乱码；且 x11vnc 会把 PRIMARY
# selection（鼠标选中即写入）也发给客户端，造成"选中就自动复制"。
# 这里绕开 VNC 剪贴板，直接用 xclip 读写远程 X 桌面的 CLIPBOARD selection：
#   - 只读写 CLIPBOARD（Ctrl+C 语义），不碰 PRIMARY，解决"选中即复制"
#   - xclip 按 UTF-8 存取，中文无乱码
#   - 写入后由前端/后端触发 Ctrl+V，完成"粘贴"动作


def _clip_env() -> dict:
    env = dict(os.environ)
    env["DISPLAY"] = DISPLAY
    return env


def _read_clipboard() -> str:
    """读取远程 X 桌面 CLIPBOARD selection 文本（UTF-8）。"""
    xclip = shutil.which("xclip")
    if xclip is None:
        raise RuntimeError("服务器缺少 xclip，无法读写剪贴板（apt install xclip）")
    try:
        out = subprocess.run(
            [xclip, "-o", "-selection", "clipboard"],
            env=_clip_env(), capture_output=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        # 没有 owner 时 xclip -o 可能挂起等待；超时视为空剪贴板
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", errors="replace")


def _write_clipboard(text: str) -> None:
    """把文本写入远程 X 桌面 CLIPBOARD selection（UTF-8）。

    xclip -i 进程保持存活以持有 selection（直到被下次写入替换）。
    """
    xclip = shutil.which("xclip")
    if xclip is None:
        raise RuntimeError("服务器缺少 xclip，无法读写剪贴板（apt install xclip）")
    proc = subprocess.Popen(
        [xclip, "-i", "-selection", "clipboard"],
        env=_clip_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        proc.communicate(text.encode("utf-8"), timeout=5)
        # 等 selection 就绪（xclip 进程持有 owner），避免紧接的 Ctrl+V 读不到
        time.sleep(0.3)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        raise


def _paste_into_desktop() -> None:
    """向远程桌面当前焦点窗口发送粘贴快捷键（配合 selection 完成粘贴）。

    终端类应用（xfce4-terminal 等）的粘贴是 Ctrl+Shift+V，普通图形应用
    （chromium、文本编辑器等）是 Ctrl+V —— 按焦点窗口名智能选择。
    """
    xdotool = shutil.which("xdotool")
    if xdotool is None:
        raise RuntimeError("服务器缺少 xdotool（apt install xdotool）")
    combo = "ctrl+v"
    try:
        out = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            env=_clip_env(), capture_output=True, text=True, timeout=5,
        )
        name = (out.stdout or "").strip().lower()
        if any(k in name for k in (
            "terminal", "终端", "xfce4-terminal", "konsole",
            "gnome-terminal", "bash", "zsh", "tty",
        )):
            combo = "ctrl+shift+v"
    except Exception:  # noqa: BLE001
        logger.debug("[qwenpaw-desktop] window name lookup failed", exc_info=True)
    subprocess.run(
        [xdotool, "key", combo],
        env=_clip_env(), timeout=5,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


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
    # no-store：前端迭代频繁，避免浏览器缓存旧页面
    return FileResponse(str(DESKTOP_HTML), media_type="text/html",
                        headers={"Cache-Control": "no-store"})


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
        "screen": _current_screen(),
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


@router.get("/resolutions")
async def list_resolutions():
    """返回可选分辨率列表与当前生效分辨率。"""
    return {
        "ok": True,
        "current": _current_screen(),
        "presets": SCREEN_PRESETS,
    }


class ResolutionRequest(BaseModel):
    width: int
    height: int


@router.post("/resolution")
async def set_resolution(req: ResolutionRequest):
    """切换虚拟屏幕分辨率（重启桌面栈生效，已打开的窗口会关闭）。"""
    if (req.width, req.height) not in [(p["width"], p["height"]) for p in SCREEN_PRESETS]:
        raise HTTPException(status_code=400, detail="不支持的分辨率")
    try:
        await asyncio.to_thread(_set_screen, req.width, req.height)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[qwenpaw-desktop] set resolution failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "screen": _current_screen(), "message": f"分辨率已切换为 {req.width}x{req.height}"}


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


@router.get("/screenshot")
async def screenshot():
    """截取远程桌面当前屏幕，返回 PNG 图片。"""
    try:
        png = await asyncio.to_thread(_capture_screen_png)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[qwenpaw-desktop] screenshot failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/screenshot/save")
async def screenshot_save():
    """截屏并保存到平台公共数据目录 plugin_data/screenshots/，返回保存路径。"""
    try:
        png = await asyncio.to_thread(_capture_screen_png)
        fpath = await asyncio.to_thread(_save_screenshot, png)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[qwenpaw-desktop] screenshot save failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "path": str(fpath), "name": fpath.name}


@router.get("/clipboard/read")
async def clipboard_read():
    """读取远程桌面 CLIPBOARD selection 文本（UTF-8，只读 Ctrl+C 复制的）。"""
    try:
        await asyncio.to_thread(_ensure_desktop)
        text = await asyncio.to_thread(_read_clipboard)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[qwenpaw-desktop] clipboard read failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "text": text}


class ClipboardWriteRequest(BaseModel):
    text: str


@router.post("/clipboard/write")
async def clipboard_write(req: ClipboardWriteRequest):
    """把文本写入远程 CLIPBOARD selection，并自动 Ctrl+V 粘贴到当前焦点。"""
    try:
        await asyncio.to_thread(_ensure_desktop)
        await asyncio.to_thread(_write_clipboard, req.text)
        await asyncio.to_thread(_paste_into_desktop)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[qwenpaw-desktop] clipboard write failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "message": "已写入远程剪贴板并触发粘贴"}


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
