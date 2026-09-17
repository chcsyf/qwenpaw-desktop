/**
 * qwenpaw-desktop —— WebSocket polyfill（平台网关剥 WS 头时的降级传输）
 *
 * 背景
 * ----
 * 部分平台网关（如 *.qwenpaw.platform.agentscope.io）按 RFC 7230 §6.1 剥掉
 * WebSocket 握手的 `Upgrade` / `Connection`（hop-by-hop）头，导致 noVNC 的
 * `new WebSocket()` 永远无法升级，浏览器侧表现为连接失败 + 无限重连。
 *
 * 实测确认该网关：**只剥 hop-by-hop 头** —— 自定义头、流式响应都不受影响。
 * 因此可以把 WebSocket 流量改走普通 HTTP：
 *
 *   下行  GET    /api/qwenpaw-desktop/vnc-stream?sid=xxx  → ReadableStream 持续读
 *   上行  POST   /api/qwenpaw-desktop/vnc-input?sid=xxx   → 负载写入 VNC
 *   关闭  DELETE /api/qwenpaw-desktop/vnc-stream?sid=xxx
 *
 * 契约
 * ----
 * noVNC 的 core/websock.js 只要求 raw channel 具备 8 个成员：
 *   ["send","close","binaryType","onerror","onmessage","onopen","protocol","readyState"]
 * 并且用 `Object.keys(inst)` + `Object.getOwnPropertyNames(proto)` 做鸭子类型检查，
 * 所以这 8 个成员必须出现在「实例自身属性」或「原型属性」上。本实现全部满足，
 * 且 readyState 用数字（noVNC 的 ReadyStates 用 includes() 比较，数字/字符串都接受）。
 *
 * 安全
 * ----
 * 只接管同源的 `<WS_PATH>`；其他 URL 一律原样交给原生 WebSocket，行为不变。
 */
(function () {
  "use strict";

  var Native = window.WebSocket;
  if (!Native) return;

  // 逃生开关：访问 /desktop_page?ws=native 时强制使用原生 WebSocket（不接管）
  if (/[?&]ws=native\b/.test(location.search)) {
    window.__qwenpawWsPolyfill = { active: false, reason: "disabled by ?ws=native" };
    return;
  }

  // 需要接管的 WS 路径（noVNC 连接的就是这里）
  var WS_PATH = "/api/qwenpaw-desktop/vnc";
  var STREAM_PATH = "/api/qwenpaw-desktop/vnc-stream";
  var INPUT_PATH = "/api/qwenpaw-desktop/vnc-input";

  var CONNECTING = 0,
    OPEN = 1,
    CLOSING = 2,
    CLOSED = 3;

  // 上行合并窗口：把这段时间内积压的客户端数据合成一个 POST。
  // 减少请求数可以显著降低被中间层串改 body 的概率（实测网关会把相邻 POST 的 body 粘在一起）。
  var UP_FLUSH_MS = 15;

  // 上行帧格式：魔数 "QK"(2B) + 序号(2B 大端) + 载荷长度(2B 大端) + 载荷
  // 序号的作用：上行不再"串行等待前一个响应"（那会让每一帧都付一次完整网关往返，
  // 实测累积成 20-40 秒延迟），改成立即并发发出，因此需要序号让服务端判序。
  var FRAME_MAGIC_0 = 0x51; // 'Q'
  var FRAME_MAGIC_1 = 0x4b; // 'K'
  var FRAME_HDR = 6;

  function makeSid() {
    return (
      "p" +
      Date.now().toString(36) +
      Math.random().toString(36).slice(2, 10)
    );
  }

  function toAbsolute(url) {
    try {
      return new URL(url, location.href);
    } catch (e) {
      return null;
    }
  }

  /** 该 URL 是否属于我们要接管的路径。 */
  function shouldHandle(url) {
    var u = toAbsolute(url);
    if (!u) return false;
    return u.pathname === WS_PATH;
  }

  function PseudoWebSocket(url, protocols) {
    if (!(this instanceof PseudoWebSocket)) {
      return new PseudoWebSocket(url, protocols);
    }

    this.url = String(url);
    this.readyState = CONNECTING;
    this.binaryType = "arraybuffer";
    this.protocol = "";
    this.extensions = "";
    this.bufferedAmount = 0;

    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;

    this._sid = makeSid();
    this._listeners = Object.create(null);
    this._sendChain = Promise.resolve();
    this._pending = [];
    this._flushTimer = null;
    this._seq = 0;
    this._closed = false;
    this._abort = null;

    var self = this;
    // 异步启动，保证 onopen 等处理器有机会先被赋值（与原生行为一致）
    Promise.resolve().then(function () {
      self._open();
    });
  }

  PseudoWebSocket.CONNECTING = CONNECTING;
  PseudoWebSocket.OPEN = OPEN;
  PseudoWebSocket.CLOSING = CLOSING;
  PseudoWebSocket.CLOSED = CLOSED;

  PseudoWebSocket.prototype._fire = function (type, ev) {
    // 诊断日志：默认开启，可在 Console 用 window.__qwenpawWsPolyfillDebug=false 关闭
    if (window.__qwenpawWsPolyfillDebug !== false) {
      try {
        var extra = "";
        if (type === "message" && ev && ev.data) {
          extra = " byteLength=" + ev.data.byteLength;
        } else if (type === "close" && ev) {
          extra = " code=" + ev.code + " clean=" + ev.wasClean + " reason=" + (ev.reason || "");
        }
        console.log(
          "%c[ws-polyfill]%c " + type + extra,
          "color:#4ade80;font-weight:bold",
          "color:inherit"
        );
      } catch (e) {
        /* ignore */
      }
    }

    // 事件对象：尽量贴近原生（MessageEvent / Event / CloseEvent）
    var handler = this["on" + type];
    if (typeof handler === "function") {
      try {
        handler.call(this, ev);
      } catch (e) {
        /* 不因业务回调抛错而中断内部流程 */
      }
    }
    var list = this._listeners[type];
    if (list) {
      for (var i = 0; i < list.length; i++) {
        try {
          list[i].call(this, ev);
        } catch (e) {
          /* 同上 */
        }
      }
    }
  };

  PseudoWebSocket.prototype._open = function () {
    var self = this;
    var streamUrl = STREAM_PATH + "?sid=" + encodeURIComponent(this._sid);

    this._abort = typeof AbortController === "function" ? new AbortController() : null;

    fetch(streamUrl, {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "X-QwenPaw-WS-Transport": "http-stream" },
      signal: this._abort ? this._abort.signal : undefined,
    })
      .then(function (res) {
        if (!res.ok) throw new Error("stream HTTP " + res.status);
        if (!res.body || typeof res.body.getReader !== "function") {
          throw new Error("ReadableStream 不可用");
        }
        if (self._closed) return null;
        self.readyState = OPEN;
        self._fire("open", new Event("open"));
        return self._pump(res.body.getReader());
      })
      .catch(function (err) {
        if (self._closed) return;
        self.readyState = CLOSED;
        self._fire("error", new Event("error"));
        self._fire(
          "close",
          new CloseEvent("close", { code: 1006, reason: String(err), wasClean: false })
        );
      });
  };

  /** 持续读取下行流，每块作为一个 message 投递。 */
  PseudoWebSocket.prototype._pump = function (reader) {
    var self = this;

    function step() {
      return reader.read().then(function (r) {
        if (r.done) {
          if (!self._closed) {
            self._closed = true;
            self.readyState = CLOSED;
            self._fire("close", new CloseEvent("close", { code: 1000, wasClean: true }));
          }
          return;
        }
        if (self._closed) return;

        var chunk = r.value;
        var payload;
        if (chunk instanceof ArrayBuffer) {
          payload = chunk;
        } else if (chunk && chunk.buffer) {
          // 复制一份：流可能复用同一块底层 buffer
          payload = chunk.buffer.slice(chunk.byteOffset, chunk.byteOffset + chunk.byteLength);
        } else {
          payload = new ArrayBuffer(0);
        }

        // noVNC 用 binaryType='arraybuffer'，这里始终投递 ArrayBuffer
        self._fire("message", new MessageEvent("message", { data: payload }));
        return step();
      });
    }

    return step();
  };

  PseudoWebSocket.prototype.send = function (data) {
    if (this.readyState !== OPEN) return;

    var bytes;
    if (data instanceof ArrayBuffer) {
      bytes = new Uint8Array(data);
    } else if (data && data.buffer instanceof ArrayBuffer) {
      bytes = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    } else if (typeof data === "string") {
      bytes = new TextEncoder().encode(data);
    } else {
      return;
    }
    // 复制一份：底层 buffer 可能被调用方复用
    this._pending.push(new Uint8Array(bytes));

    if (this._flushTimer === null) {
      var self = this;
      this._flushTimer = setTimeout(function () {
        self._flushTimer = null;
        self._flush();
      }, UP_FLUSH_MS);
    }
  };

  /** 把积压的客户端数据合并成一个帧，POST 给服务端。 */
  PseudoWebSocket.prototype._flush = function () {
    if (!this._pending.length || this._closed) return;

    // 鼠标移动合并：noVNC 对每一次鼠标移动都会 send 一帧 PointerEvent(6 字节：
    // type=5 + mask + x + y)，这是上行请求量的绝对大头。鼠标位置是"状态量"，
    // 同一批里只保留最后一个即可 —— 既大幅减少上行请求数，也让键盘事件不必排在
    // 长长的鼠标队列后面（用户感受就是"键盘比鼠标慢"）。
    var items = this._pending;
    this._pending = [];
    var lastPtr = -1;
    for (var a = 0; a < items.length; a++) {
      if (items[a].length === 6 && items[a][0] === 5) {
        lastPtr = a; // PointerEvent
      }
    }
    if (lastPtr >= 0) {
      var kept = [];
      for (var b = 0; b < items.length; b++) {
        var isPtr = items[b].length === 6 && items[b][0] === 5;
        if (!isPtr || b === lastPtr) {
          kept.push(items[b]);
        }
      }
      items = kept;
    }

    var total = 0;
    for (var i = 0; i < items.length; i++) {
      total += items[i].length;
    }
    var payload = new Uint8Array(total);
    var off = 0;
    for (var j = 0; j < items.length; j++) {
      payload.set(items[j], off);
      off += items[j].length;
    }

    // 帧封装：魔数 "QK" + 序号(2B) + 载荷长度(2B) + 载荷。
    // 平台网关会串改上行 body（实测：SetPixelFormat 与 SetEncodings 两个 POST 的
    // 内容被粘到了一起，导致 x11vnc 收到非法字节后 RST）。服务端靠魔数 + 长度
    // 校验把坏帧丢掉，绝不写进 VNC 流；序号让服务端能识别乱序/重复。
    this._seq = (this._seq + 1) & 0xffff;
    var frame = new Uint8Array(FRAME_HDR + payload.length);
    frame[0] = FRAME_MAGIC_0;
    frame[1] = FRAME_MAGIC_1;
    frame[2] = (this._seq >> 8) & 0xff;
    frame[3] = this._seq & 0xff;
    frame[4] = (payload.length >> 8) & 0xff;
    frame[5] = payload.length & 0xff;
    frame.set(payload, FRAME_HDR);

    if (window.__qwenpawWsPolyfillDebug !== false) {
      try {
        var hex = "";
        var m = Math.min(20, frame.length);
        for (var k = 0; k < m; k++) {
          hex += ("0" + frame[k].toString(16)).slice(-2);
        }
        console.log(
          "%c[ws-polyfill]%c flush seq=" + this._seq + " payload=" + payload.length +
            "B frame=" + frame.length + "B hex=" + hex,
          "color:#60a5fa;font-weight:bold",
          "color:inherit"
        );
      } catch (e) {
        /* ignore */
      }
    }

    // 立即发出，不等待前一个请求的响应。
    // 「串行等待」会让每一帧都付一次完整的网关往返 —— 实测累积成 20-40 秒延迟。
    // 顺序改由帧序号保证，服务端按序号判定。
    var self = this;
    var url = INPUT_PATH + "?sid=" + encodeURIComponent(this._sid);
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/octet-stream" },
      body: frame,
    }).catch(function () {
      /* 上行失败不致命，VNC 侧会自行超时 */
    });
  };

  /**
   * 建立上行长连接：一个 POST，请求体是 ReadableStream，帧随产随推。
   *
   * 为什么需要：noVNC 是"收到一帧就请求下一帧"的节奏，若每帧都发一个独立 POST
   * 并在客户端串行等待响应，每一帧都要付一次完整的网关往返代价（实测累积成
   * 20-40 秒延迟）。改成一个长 POST 后，整条上行只有一个 HTTP 请求。
   *
   * 浏览器不支持 duplex 流式请求体时静默退化为逐帧独立 POST。
   */
  PseudoWebSocket.prototype._openUpstream = function () {
    if (typeof ReadableStream !== "function") return;
    var self = this;
    try {
      this._upStream = new ReadableStream({
        start: function (c) {
          self._upCtrl = c;
        },
      });
    } catch (e) {
      return;
    }

    var url = INPUT_PATH + "?sid=" + encodeURIComponent(this._sid);
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/octet-stream" },
      body: this._upStream,
      duplex: "half",
    })
      .then(function (r) {
        if (window.__qwenpawWsPolyfillDebug !== false) {
          console.log(
            "%c[ws-polyfill]%c upstream stream ended status=" + r.status,
            "color:#fbbf24;font-weight:bold",
            "color:inherit"
          );
        }
        self._upCtrl = null;
      })
      .catch(function (e) {
        if (window.__qwenpawWsPolyfillDebug !== false) {
          console.log(
            "%c[ws-polyfill]%c upstream stream unavailable -> per-frame POST: " + e,
            "color:#f87171;font-weight:bold",
            "color:inherit"
          );
        }
        self._upCtrl = null;
        self._upStream = null;
      });
  };

  PseudoWebSocket.prototype.close = function (code, reason) {
    if (this._closed) return;
    this.readyState = CLOSING;

    // 关闭前把积压的输入发出去
    if (this._flushTimer !== null) {
      clearTimeout(this._flushTimer);
      this._flushTimer = null;
    }
    this._flush();
    // 收起上行长连接
    if (this._upCtrl) {
      try {
        this._upCtrl.close();
      } catch (e) {
        /* ignore */
      }
      this._upCtrl = null;
    }

    var self = this;
    var url = STREAM_PATH + "?sid=" + encodeURIComponent(this._sid);
    try {
      fetch(url, { method: "DELETE", credentials: "same-origin", cache: "no-store" }).catch(
        function () {}
      );
    } catch (e) {
      /* ignore */
    }

    if (this._abort) {
      try {
        this._abort.abort();
      } catch (e) {
        /* ignore */
      }
    }

    this._closed = true;
    this.readyState = CLOSED;
    // 异步投递 close，给调用方留出注册时机
    Promise.resolve().then(function () {
      self._fire(
        "close",
        new CloseEvent("close", {
          code: typeof code === "number" ? code : 1000,
          reason: reason || "",
          wasClean: true,
        })
      );
    });
  };

  PseudoWebSocket.prototype.addEventListener = function (type, fn) {
    if (typeof fn !== "function") return;
    (this._listeners[type] || (this._listeners[type] = [])).push(fn);
  };

  PseudoWebSocket.prototype.removeEventListener = function (type, fn) {
    var list = this._listeners[type];
    if (!list) return;
    var i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  };

  PseudoWebSocket.prototype.dispatchEvent = function (ev) {
    if (ev && ev.type) this._fire(ev.type, ev);
    return true;
  };

  /**
   * 替换 window.WebSocket：
   *  - 目标路径 → PseudoWebSocket（HTTP 流式）
   *  - 其他 URL → 原生实现，行为完全不变
   */
  function PatchedWebSocket(url, protocols) {
    if (shouldHandle(url)) {
      return new PseudoWebSocket(url, protocols);
    }
    return protocols === undefined ? new Native(url) : new Native(url, protocols);
  }

  PatchedWebSocket.prototype = Native.prototype;
  PatchedWebSocket.CONNECTING = CONNECTING;
  PatchedWebSocket.OPEN = OPEN;
  PatchedWebSocket.CLOSING = CLOSING;
  PatchedWebSocket.CLOSED = CLOSED;

  window.WebSocket = PatchedWebSocket;
  window.__qwenpawWsPolyfill = {
    active: true,
    handledPath: WS_PATH,
    PseudoWebSocket: PseudoWebSocket,
  };
})();
