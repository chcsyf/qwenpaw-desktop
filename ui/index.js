/**
 * QwenPaw Web 远程桌面 v0.1.1 — 前端 GUI
 * （qwenpaw.platform.agentscope.io 专用插件）
 *
 * 纯桌面视图：iframe 内嵌自定义 noVNC 页面（/api/qwenpaw-desktop/desktop_page）。
 * 无浏览器工具栏；桌面右下角竖排快捷入口（GitHub/Google/Bing/百度/终端/文件），
 * 右下工具含 📷 截图与 📋 剪贴板互通。
 * 底部仅保留最小状态条（桌面状态 + 重连 + 关闭桌面）。
 * 桌面端物理键盘直接可用，移动端点击画面唤起系统软键盘。
 */
(function () {
  "use strict";

  if (!window.QwenPaw || !window.QwenPaw.host) {
    console.error("[qwenpaw-desktop] QwenPaw not ready");
    return;
  }

  var QP = window.QwenPaw;
  var React = QP.host.React;
  var h = React.createElement;

  var PLUGIN_ID = "qwenpaw-desktop";
  var PLUGIN_NAME = "远程桌面";
  var VERSION = "0.1.1";

  // ---------- 样式（GitHub Dark，最小化） ----------
  var C = {
    bg: "#0d1117",
    panel: "#161b22",
    border: "#30363d",
    text: "#e6edf3",
    muted: "#8b949e",
    green: "#3fb950",
    red: "#f85149",
  };

  var S = {
    wrap: {
      display: "flex", flexDirection: "column",
      height: "100%", minHeight: 0,
      background: C.bg, color: C.text,
      fontFamily: "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif",
      fontSize: 13,
    },
    frameWrap: {
      flex: 1, minHeight: 0,
      position: "relative",
      background: "#010409",
    },
    frame: {
      width: "100%", height: "100%",
      border: "none", display: "block",
    },
    statusBar: {
      display: "flex", alignItems: "center", gap: 8,
      padding: "5px 10px",
      background: C.panel,
      borderTop: "1px solid " + C.border,
      flexShrink: 0,
      color: C.muted, fontSize: 12,
      minHeight: 28,
    },
    statusDot: { width: 8, height: 8, borderRadius: "50%", flexShrink: 0 },
    hint: { color: C.muted, fontSize: 12, flexShrink: 0 },
    spacer: { flex: 1 },
    closeBtn: {
      background: "transparent", color: C.red,
      border: "1px solid " + C.red,
      borderRadius: 6,
      padding: "3px 10px",
      cursor: "pointer", fontSize: 12,
      flexShrink: 0,
    },
  };

  // ---------- 工具 ----------
  function fetchJson(url, opts) {
    var o = opts || {};
    return fetch(url, {
      method: o.method || "GET",
      headers: o.body ? { "Content-Type": "application/json" } : undefined,
      body: o.body ? JSON.stringify(o.body) : undefined,
    }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(t || r.status); });
      return r.json();
    });
  }

  // ---------- 主组件 ----------
  function DesktopApp() {
    var _st = React.useState({
      connKey: 0,
      toast: null,
      running: null,
      startedAt: null,
      procs: {},
    });
    var state = _st[0];
    var setState = _st[1];
    var update = function (patch) {
      setState(function (prev) { return Object.assign({}, prev, patch); });
    };

    var toastTimer = React.useRef(null);
    var showToast = function (msg) {
      update({ toast: msg });
      if (toastTimer.current) clearTimeout(toastTimer.current);
      toastTimer.current = setTimeout(function () { update({ toast: null }); }, 4000);
    };

    // 重新连接（重建 iframe）
    var reconnect = function () {
      update({ connKey: state.connKey + 1 });
      showToast("已重新连接");
    };

    // 关闭桌面
    var doClose = function () {
      if (!window.confirm("关闭服务器远程桌面，释放资源？\n下次打开会自动重启。")) {
        return;
      }
      fetchJson("/api/" + PLUGIN_ID + "/close", { method: "POST" })
        .then(function (r) {
          update({ running: false, procs: {}, startedAt: null });
          showToast(r.message || "远程桌面已关闭");
        })
        .catch(function (e) { showToast(String(e)); });
    };

    // 状态轮询（每 5s）
    var refreshStatus = function () {
      fetchJson("/api/" + PLUGIN_ID + "/status").then(function (r) {
        if (r.ok) {
          update({ running: r.running, startedAt: r.started_at, procs: r.procs || {} });
        }
      }).catch(function () { /* 忽略 */ });
    };
    React.useEffect(function () {
      refreshStatus();
      var timer = setInterval(refreshStatus, 5000);
      return function () { clearInterval(timer); };
    }, []);

    var statusLabel = "桌面状态未知";
    var statusColor = C.muted;
    if (state.running === true) {
      var pids = Object.keys(state.procs).filter(function (k) { return state.procs[k]; })
        .map(function (k) { return state.procs[k]; });
      statusLabel = "桌面运行中 (PID " + pids.join(", ") + ")";
      statusColor = C.green;
    } else if (state.running === false) {
      statusLabel = "桌面未启动（连接或点快捷入口时自动启动）";
      statusColor = C.muted;
    }

    return h("div", { style: S.wrap },
      // 桌面视口（纯桌面，无工具栏）
      h("div", { style: S.frameWrap },
        h("iframe", {
          key: state.connKey,
          style: S.frame,
          src: "/api/" + PLUGIN_ID + "/desktop_page",
          allow: "clipboard-read; clipboard-write",
        }),
        state.toast ? h("div", {
          style: {
            position: "absolute", left: "50%", bottom: 40,
            transform: "translateX(-50%)",
            background: "#d73a49", color: "#fff",
            padding: "6px 14px", borderRadius: 6,
            fontSize: 12, zIndex: 10, maxWidth: "80%",
            boxShadow: "0 4px 12px rgba(0,0,0,.4)",
          },
        }, state.toast) : null,
      ),
      // 最小状态条
      h("div", { style: S.statusBar },
        h("span", { style: Object.assign({}, S.statusDot, { background: statusColor }), title: statusLabel }),
        h("span", { style: S.hint }, statusLabel),
        h("button", {
          type: "button",
          style: {
            background: "transparent", color: C.muted,
            border: "1px solid " + C.border, borderRadius: 6,
            padding: "2px 8px", cursor: "pointer", fontSize: 12,
          },
          title: "重新建立 noVNC 连接",
          onClick: reconnect,
        }, "重连"),
        h("span", { style: S.spacer }),
        h("button", { type: "button", style: S.closeBtn, title: "关闭服务器远程桌面（释放资源）", onClick: doClose }, "关闭桌面"),
      ),
    );
  }

  // ---------- 注册（应用中心路由） ----------
  if (QP.registerRoutes) {
    try {
      QP.registerRoutes(PLUGIN_ID, [
        {
          path: "/apps/" + PLUGIN_ID,
          component: DesktopApp,
          label: PLUGIN_NAME,
          icon: "🖥️",
        },
      ]);
    } catch (e) { console.error("[qwenpaw-desktop] registerRoutes", e); }
  }
  console.log("[qwenpaw-desktop] v" + VERSION + " registered");
})();
