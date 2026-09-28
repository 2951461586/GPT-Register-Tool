// sentinel_bootstrap.js — 在加载真·sdk.js 之前铺好浏览器环境 shim。
// 只提供 sdk.js 实际会读到的对象/方法，值对齐 Edge 150 / zh-CN。
(function () {
  "use strict";
  var g = globalThis;

  // sdk 里的“自我防篡改”陷阱会对函数源码跑灾难性回溯正则 (((.+)+)+)+$。
  // 真浏览器 V8 上瞬间命中；goja/regexp2 会指数级卡死。这里对该固定模式短路，
  // 返回与真浏览器一致的结果（非空串必命中→返回 0），不影响 token 计算。
  var _search = String.prototype.search;
  String.prototype.search = function (re) {
    try {
      var s = (re && re.source) ? re.source : String(re);
      if (s.indexOf("(((.+)+)+)+") !== -1) return this.length ? 0 : -1;
    } catch (e) {}
    return _search.call(this, re);
  };

  // goja 抛 TypeError 用旧版措辞 "Cannot read property 'x' of undefined/null"，
  // V8/Chrome 用 "Cannot read properties of undefined/null (reading 'x')"。
  // collector 会把访问 undefined 属性时的错误串作为指纹熵记录（如 clientBootstrap），
  // 措辞不一致会暴露非 V8 引擎。覆写 Error.toString 把 goja 文案改写成 V8 文案。
  var _errToString = Error.prototype.toString;
  Error.prototype.toString = function () {
    var s = _errToString.call(this);
    try {
      s = s.replace(/Cannot read property '([^']*)' of undefined/g, "Cannot read properties of undefined (reading '$1')")
           .replace(/Cannot read property '([^']*)' of null/g, "Cannot read properties of null (reading '$1')");
    } catch (e) {}
    return s;
  };

  // sdk 里有基于 setInterval 的反调试轮询（每隔几秒检测 debugger）。
  // 在 eventloop 下这个循环定时器会让事件循环永不清空 → loop 卡死。
  // 一次性 token 计算不需要它，直接置空；setTimeout 保留（dx VM 会用）。
  g.setInterval = function () { return 0; };
  g.clearInterval = function () {};

  // crypto.getRandomValues（uuid v4 生成 sentinel deviceId 时需要）
  g.crypto = g.crypto || {};
  if (typeof g.crypto.getRandomValues !== "function") {
    g.crypto.getRandomValues = function (arr) {
      for (var i = 0; i < arr.length; i++) arr[i] = (Math.random() * 256) | 0;
      return arr;
    };
  }
  if (typeof g.crypto.randomUUID !== "function") {
    g.crypto.randomUUID = function () {
      var b = new Uint8Array(16);
      g.crypto.getRandomValues(b);
      b[6] = (b[6] & 0x0f) | 0x40;
      b[8] = (b[8] & 0x3f) | 0x80;
      var h = [];
      for (var i = 0; i < 16; i++) h.push((b[i] + 0x100).toString(16).slice(1));
      return h[0] + h[1] + h[2] + h[3] + "-" + h[4] + h[5] + "-" + h[6] + h[7] +
        "-" + h[8] + h[9] + "-" + h[10] + h[11] + h[12] + h[13] + h[14] + h[15];
    };
  }

  // window / self 自引用
  g.window = g;
  g.self = g;
  g.top = g;
  g.parent = g;

  // ---- Date 覆写：输出与当前出口国家一致的时区 ----
  var _DateToString = Date.prototype.toString;
  Date.prototype.toString = function () {
    try {
      var timezone = g.__TIMEZONE__ || "America/Chicago";
      var source = new Date(this.getTime());
      var parts = new Intl.DateTimeFormat("en-US", {
        timeZone: timezone,
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
        hourCycle: "h23"
      }).formatToParts(source).reduce(function (out, part) {
        if (part.type !== "literal") out[part.type] = Number(part.value);
        return out;
      }, {});
      var localAsUtc = Date.UTC(
        parts.year, parts.month - 1, parts.day,
        parts.hour, parts.minute, parts.second
      );
      var offsetMinutes = Math.round((localAsUtc - source.getTime()) / 60000);
      var d = new Date(source.getTime() + offsetMinutes * 60000);
      var wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()];
      var mo = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()];
      function p(n) { return (n < 10 ? "0" : "") + n; }
      var sign = offsetMinutes >= 0 ? "+" : "-";
      var offset = Math.abs(offsetMinutes);
      return wd + " " + mo + " " + p(d.getUTCDate()) + " " + d.getUTCFullYear() +
        " " + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" + p(d.getUTCSeconds()) +
        " GMT" + sign + p(Math.floor(offset / 60)) + p(offset % 60) + " (" + timezone + ")";
    } catch (e) {
      return _DateToString.call(this);
    }
  };

  // ---- 构造原生方法：toString() 返回 "function name() { [native code] }" ----
  function nativeFn(name) {
    var f = function () {};
    try { Object.defineProperty(f, "name", { value: name, configurable: true }); } catch (e) {}
    f.toString = function () { return "function " + name + "() { [native code] }"; };
    return f;
  }

  // Navigator 原型：一批 Chromium/Edge 150 上真实存在的方法 + 若干属性 getter。
  var navProtoMethods = [
    "clearOriginJoinedAdInterestGroups", "joinAdInterestGroup", "leaveAdInterestGroup",
    "runAdAuction", "createAuctionNonce", "deprecatedReplaceInURN", "deprecatedURNToURL",
    "updateAdInterestGroups", "canShare", "share", "requestMIDIAccess",
    "requestMediaKeySystemAccess", "getGamepads", "getBattery", "sendBeacon", "vibrate",
    "registerProtocolHandler", "unregisterProtocolHandler", "setAppBadge", "clearAppBadge",
    "getInstalledRelatedApps", "javaEnabled", "taintEnabled", "getUserMedia",
    "webkitGetUserMedia"
  ];
  var NavigatorProto = {};
  navProtoMethods.forEach(function (m) { NavigatorProto[m] = nativeFn(m); });
  // 少量属性(有些 Tt() 随机命中时返回其值的字符串形态)
  NavigatorProto.cookieEnabled = true;
  NavigatorProto.onLine = true;
  NavigatorProto.pdfViewerEnabled = true;
  NavigatorProto.webdriver = false;

  var navigator = Object.create(NavigatorProto);
  navigator.userAgent = g.__UA__;
  navigator.appVersion = g.__UA__.replace("Mozilla/", "");
  navigator.appName = "Netscape";
  navigator.appCodeName = "Mozilla";
  navigator.platform = (g.__PLATFORM__ || "Win32").replace(/\s+/g, "");
  navigator.product = "Gecko";
  navigator.productSub = "20030107";
  navigator.vendor = "Google Inc.";
  navigator.vendorSub = "";
  navigator.language = g.__LANGUAGE__ || "en-US";
  navigator.languages = g.__LANGUAGES__ || [navigator.language, "en-US", "en"];
  navigator.hardwareConcurrency = g.__CORES__;
  navigator.deviceMemory = 8;
  navigator.maxTouchPoints = 0;
  navigator.doNotTrack = null;
  // 常见 WebAPI 子对象 (collector_dx 会探测)
  var noop = function () {};
  var noopPromise = function () { return Promise.resolve(); };
  navigator.connection = { effectiveType: "4g", rtt: 50, downlink: 10, saveData: false, type: "wifi" };
  navigator.plugins = { length: 5, item: noop, namedItem: noop, refresh: noop };
  navigator.mimeTypes = { length: 4, item: noop, namedItem: noop };
  navigator.permissions = { query: function () { return Promise.resolve({ state: "prompt" }); } };
  navigator.mediaDevices = { enumerateDevices: function () { return Promise.resolve([]); }, getUserMedia: noopPromise };
  navigator.credentials = { get: noopPromise, store: noopPromise, create: noopPromise, preventSilentAccess: noopPromise };
  navigator.serviceWorker = { controller: null, ready: Promise.resolve({ active: null }), register: noopPromise, getRegistrations: function () { return Promise.resolve([]); } };
  navigator.storage = { estimate: function () { return Promise.resolve({ quota: 2e11, usage: 5e7 }); }, getDirectory: noopPromise, persisted: function () { return Promise.resolve(false); }, persist: function () { return Promise.resolve(false); } };
  navigator.clipboard = { read: noopPromise, readText: noopPromise, write: noopPromise, writeText: noopPromise };
  navigator.locks = { request: noopPromise, query: function () { return Promise.resolve({ held: [], pending: [] }); } };
  navigator.mediaCapabilities = { decodingInfo: function () { return Promise.resolve({ supported: true, smooth: true, powerEfficient: true }); } };
  (function () {
    var m = String(g.__UA__ || "").match(/Chrome\/(\d+)\./);
    var ver = m ? m[1] : "136";
    var plat = (g.__PLATFORM_LABEL__ || "Windows");
    navigator.userAgentData = { brands: [{ brand: "Google Chrome", version: ver }, { brand: "Chromium", version: ver }, { brand: "Not.A/Brand", version: "99" }], mobile: false, platform: plat, getHighEntropyValues: function () { return Promise.resolve({ platform: plat, platformVersion: "10.0.0", fullVersionList: [{ brand: "Google Chrome", version: ver }, { brand: "Chromium", version: ver }, { brand: "Not.A/Brand", version: "99" }] }); } };
  })();
  navigator.scheduling = { isInputPending: function () { return false; } };
  navigator.ink = { requestPresenter: noopPromise };
  navigator.gpu = { requestAdapter: noopPromise };
  navigator.hid = { getDevices: function () { return Promise.resolve([]); }, requestDevice: noopPromise };
  navigator.serial = { getPorts: function () { return Promise.resolve([]); }, requestPort: noopPromise };
  navigator.usb = { getDevices: function () { return Promise.resolve([]); }, requestDevice: noopPromise };
  navigator.bluetooth = { getAvailability: function () { return Promise.resolve(false); }, requestDevice: noopPromise };
  navigator.xr = { isSessionSupported: function () { return Promise.resolve(false); }, requestSession: noopPromise };
  navigator.wakeLock = { request: noopPromise };
  navigator.geolocation = { getCurrentPosition: noop, watchPosition: noop, clearWatch: noop };
  navigator.mediaSession = { metadata: null, playbackState: "none", setActionHandler: noop };
  g.navigator = navigator;

  // ---- screen ----
  g.screen = {
    width: (g.__SCREEN_W__ || 1920), height: (g.__SCREEN_H__ || 1080), availWidth: (g.__SCREEN_W__ || 1920), availHeight: (g.__AVAIL_H__ || (((g.__SCREEN_W__ || 1920)) - 48)),
    colorDepth: 24, pixelDepth: 24, availLeft: 0, availTop: 0
  };
  g.innerWidth = (g.__SCREEN_W__ || 1920); g.innerHeight = (g.__AVAIL_H__ || (((g.__SCREEN_W__ || 1920)) - 48));
  g.outerWidth = (g.__SCREEN_W__ || 1920); g.outerHeight = (g.__AVAIL_H__ || (((g.__SCREEN_W__ || 1920)) - 48));
  g.devicePixelRatio = 1;
  g.navigator.maxTouchPoints = (g.__MAX_TOUCH__ || 0);

  // ---- history / location ----
  // collector 会记录当前页 URL 作为指纹熵。真实注册流在 /about-you 页触发 create_account
  // 的 sentinel；Go 侧可用 __PAGE_URL__ 按具体 flow 覆写，默认对齐 about-you。
  g.history = { length: 2, scrollRestoration: "auto", state: null };
  var _pageUrl = g.__PAGE_URL__ || "https://auth.openai.com/about-you";
  var _parsedPageUrl = new URL(_pageUrl);
  g.location = {
    href: _pageUrl, origin: _parsedPageUrl.origin,
    protocol: _parsedPageUrl.protocol, host: _parsedPageUrl.host, hostname: _parsedPageUrl.hostname,
    port: _parsedPageUrl.port, pathname: _parsedPageUrl.pathname,
    search: _parsedPageUrl.search, hash: _parsedPageUrl.hash,
    assign: function () {}, replace: function () {}, reload: function () {}, toString: function () { return _pageUrl; }
  };

  // ---- performance ----
  // collector 会读导航性能条目的 name（= 当前页 URL）作为指纹熵，真实页在此产出
  // "https://auth.openai.com/about-you"。缺 getEntriesByType 时该测点为空 → 少一项。
  var _t0 = Date.now();
  var _navEntry = {
    name: _pageUrl, entryType: "navigation", startTime: 0, duration: 420.5, type: "navigate",
    initiatorType: "navigation", nextHopProtocol: "h2", redirectCount: 0,
    domComplete: 410.2, domContentLoadedEventEnd: 300.1, domInteractive: 280.4,
    loadEventEnd: 418.9, responseEnd: 190.3, responseStart: 150.7, requestStart: 60.2,
    fetchStart: 5.1, connectEnd: 40.8, connectStart: 20.3, domainLookupEnd: 15.2,
    domainLookupStart: 10.1, secureConnectionStart: 25.6, transferSize: 18240, encodedBodySize: 17010, decodedBodySize: 61200
  };
  g.performance = {
    timeOrigin: _t0,
    now: function () { return (Date.now() - _t0) + Math.random(); },
    memory: { jsHeapSizeLimit: 4294705152, totalJSHeapSize: 35000000, usedJSHeapSize: 22000000 },
    getEntries: function () { return [_navEntry]; },
    getEntriesByType: function (t) { return t === "navigation" ? [_navEntry] : []; },
    getEntriesByName: function (n) { return _navEntry.name === n ? [_navEntry] : []; },
    mark: function () {}, measure: function () {}, clearMarks: function () {}, clearMeasures: function () {},
    clearResourceTimings: function () {}, setResourceTimingBufferSize: function () {}
  };

  // ---- localStorage ----
  // collector_dx 会枚举 Object.keys(localStorage) 作为指纹熵。真实 auth 页在
  // statsig 客户端 SDK 初始化后会写入一批 statsig.* 键；goja 环境无该 SDK，
  // 需按真实结构预置这些键，且方法要不可枚举，否则 keys 会枚举出方法名。
  (function () {
    function digits(n) { var s = ""; for (var i = 0; i < n; i++) s += (Math.random() * 10) | 0; if (s[0] === "0") s = "1" + s.slice(1); return s; }
    function hex(n) { var h = "0123456789abcdef", s = ""; for (var i = 0; i < n; i++) s += h[(Math.random() * 16) | 0]; return s; }
    var stable = digits(9);
    var ls = {};
    var seed = [
      "statsig.cached.evaluations." + digits(8),
      "statsig.cached.evaluations." + digits(9),
      "statsig.cached.evaluations." + digits(10),
      "statsig.last_modified_time.evaluations",
      hex(16),
      "statsig.cached.evaluations." + digits(10),
      "statsig.session_id." + stable,
      "statsig.cached.evaluations." + digits(10),
      "statsig.cached.evaluations." + digits(10),
      "statsig.cached.evaluations." + digits(10),
      "statsig.stable_id." + stable
    ];
    seed.forEach(function (k) { ls[k] = "1"; });
    if (g.__SEED_DID_KEY__ && g.__SEED_DID_VAL__) ls[g.__SEED_DID_KEY__] = g.__SEED_DID_VAL__;
    function def(name, fn) { Object.defineProperty(ls, name, { value: fn, enumerable: false, writable: true, configurable: true }); }
    def("getItem", function (k) { return Object.prototype.hasOwnProperty.call(ls, k) ? ls[k] : null; });
    def("setItem", function (k, v) { ls[k] = "" + v; });
    def("removeItem", function (k) { delete ls[k]; });
    def("clear", function () { Object.keys(ls).forEach(function (k) { delete ls[k]; }); });
    def("key", function (i) { return Object.keys(ls)[i] || null; });
    Object.defineProperty(ls, "length", { get: function () { return Object.keys(ls).length; }, enumerable: false, configurable: true });
    g.localStorage = ls;
  })();

  // ---- document（sdk 读 scripts / documentElement / cookie / keys / body 文本量测指纹）----
  var sdkScript = { src: g.__SDK_URL__, getAttribute: function () { return null; }, type: "text/javascript" };
  var docElement = { getAttribute: function () { return null; }, tagName: "HTML", lang: g.__LANGUAGE__ || "en-US" };
  var _cookie = g.__COOKIE_HEADER__ || ((g.__SEED_DID_KEY__ && g.__SEED_DID_VAL__) ? (g.__SEED_DID_KEY__ + "=" + g.__SEED_DID_VAL__) : "");

  // 可写 style 壳：collector 的文本指纹会 Reflect.set(style, "fontFamily"/"fontSize"/... )
  function makeStyle() {
    var st = {};
    st.setProperty = function (k, v) { st[k] = "" + v; };
    st.getPropertyValue = function (k) { return Object.prototype.hasOwnProperty.call(st, k) ? st[k] : ""; };
    st.removeProperty = function (k) { var v = st[k]; delete st[k]; return v; };
    st.cssText = "";
    return st;
  }

  // DOM 元素壳：createElement("div") 后设 style/innerText，appendChild 到 body，
  // 再 getBoundingClientRect()（结果会被 JSON.stringify），最后 removeChild。
  function makeElement(tag) {
    var el = {
      tagName: ("" + tag).toUpperCase(),
      nodeName: ("" + tag).toUpperCase(),
      nodeType: 1,
      style: makeStyle(),
      innerText: "", textContent: "", innerHTML: "",
      className: "", id: "", ariaHidden: null, hidden: false,
      clientWidth: 0, clientHeight: 0, offsetWidth: 0, offsetHeight: 0,
      scrollWidth: 0, scrollHeight: 0, offsetLeft: 0, offsetTop: 0,
      children: [], childNodes: [], parentNode: null,
      setAttribute: function () {}, getAttribute: function () { return null; },
      removeAttribute: function () {}, hasAttribute: function () { return false; },
      addEventListener: function () {}, removeEventListener: function () {},
      appendChild: function (c) { this.children.push(c); this.childNodes.push(c); if (c) c.parentNode = this; return c; },
      removeChild: function (c) {
        var i = this.children.indexOf(c);
        if (i >= 0) { this.children.splice(i, 1); this.childNodes.splice(i, 1); }
        if (c) c.parentNode = null;
        return c;
      },
      insertBefore: function (c) { this.children.push(c); this.childNodes.push(c); return c; },
      remove: function () {},
      querySelector: function () { return null; },
      querySelectorAll: function () { return []; },
      getClientRects: function () { return [this.getBoundingClientRect()]; },
      getBoundingClientRect: function () {
        // 无真实排版：按文本长度给出稳定的亚像素宽度（Impact 15px 近似），
        // 结果仅作为指纹熵被服务端记录。
        var t = "" + (this.innerText || "");
        var w = t.length ? (t.length * 7.34 + 0.421875) : 0;
        var h = t.length ? 17.5 : 0;
        return { x: 8, y: 8, width: w, height: h, top: 8, right: 8 + w, bottom: 8 + h, left: 8 };
      }
    };
    return el;
  }

  var bodyEl = makeElement("body");
  var headEl = makeElement("head");
  g.document = {
    scripts: [sdkScript],
    documentElement: docElement,
    body: bodyEl,
    head: headEl,
    getElementsByTagName: function (t) {
      t = String(t).toLowerCase();
      if (t === "script") return [sdkScript];
      if (t === "body") return [bodyEl];
      if (t === "head") return [headEl];
      return [];
    },
    getElementById: function () { return null; },
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    createElement: function (tag) { return makeElement(tag || "div"); },
    createTextNode: function (t) { return { nodeType: 3, textContent: "" + t }; },
    referrer: "",
    location: g.location,
    URL: _pageUrl,
    documentURI: _pageUrl,
    baseURI: _pageUrl,
    title: "",
    readyState: "complete",
    hidden: false,
    visibilityState: "visible",
    characterSet: "UTF-8",
    contentType: "text/html",
    get cookie() { return _cookie; },
    set cookie(v) { _cookie = "" + v; }
  };

  // ---- __reactRouterContext ----
  // collector 会 try-读 ctx.state.loaderData.root.clientBootstrap 的地理字段。
  // 真实 auth 注册页此处 root 为 undefined，读其属性会连环抛错并被作为指纹熵记录。
  // 保持 root 缺失（与真实同形态、同测点数量），错误文案由上方 Error.toString
  // 覆写统一改成 V8 措辞，避免暴露非 V8 引擎。
  g.__reactRouterContext = { state: { loaderData: {} } };

  // ---- requestIdleCallback 保持 undefined → sdk 走 setTimeout 兜底 ----
  // ---- 常见空壳，避免 sdk 里偶发访问抛错 ----
  g.addEventListener = function () {};
  g.removeEventListener = function () {};
  g.dispatchEvent = function () { return true; };
  g.matchMedia = function () { return { matches: false, addListener: function () {}, removeListener: function () {}, addEventListener: function () {}, removeEventListener: function () {} }; };
  if (typeof g.console === "undefined") g.console = { log: function () {}, warn: function () {}, error: function () {} };

  // ---- 更多 collector_dx 常见探测对象 ----
  g.chrome = { runtime: { id: undefined, connect: function () {}, sendMessage: function () {} }, csi: function () { return {}; }, loadTimes: function () { return {}; } };
  g.caches = { open: function () { return Promise.resolve({ match: function () { return Promise.resolve(); } }); }, keys: function () { return Promise.resolve([]); }, has: function () { return Promise.resolve(false); }, match: function () { return Promise.resolve(); } };
  g.indexedDB = { open: function () { return { result: null, onerror: null, onsuccess: null }; }, databases: function () { return Promise.resolve([]); } };
  g.speechSynthesis = { getVoices: function () { return []; }, speak: function () {}, cancel: function () {}, pause: function () {}, resume: function () {} };
  g.visualViewport = { width: 1920, height: 945, offsetLeft: 0, offsetTop: 0, scale: 1, pageLeft: 0, pageTop: 0, addEventListener: function () {} };
  g.origin = "https://auth.openai.com";
  g.isSecureContext = true;
  g.crossOriginIsolated = false;
  g.originAgentCluster = false;
  g.credentialless = false;
  g.scheduler = { postTask: function (fn) { return Promise.resolve(fn()); } };
  g.trustedTypes = { createPolicy: function () { return { createHTML: function (s) { return s; } }; } };
  g.cookieStore = { get: function () { return Promise.resolve(null); }, getAll: function () { return Promise.resolve([]); }, set: function () { return Promise.resolve(); } };
  g.AbortController = function () { this.signal = { aborted: false }; this.abort = function () { this.signal.aborted = true; }; };
  g.AbortSignal = { abort: function () { return { aborted: true }; }, timeout: function () { return { aborted: false }; } };
  g.URL = g.URL || function (u) { this.href = u; this.toString = function () { return u; }; };
  g.URLSearchParams = g.URLSearchParams || function () { this.get = function () { return null; }; this.set = function () {}; this.toString = function () { return ""; }; };
  g.Blob = g.Blob || function () { this.size = 0; this.type = ""; };
  g.File = g.File || function () { this.name = ""; this.size = 0; };
  g.FileReader = g.FileReader || function () { this.readAsText = function () {}; this.readAsArrayBuffer = function () {}; };
  g.FormData = g.FormData || function () { this.append = function () {}; this.get = function () { return null; }; };
  g.Headers = g.Headers || function () { this.get = function () { return null; }; this.set = function () {}; };
  g.Request = g.Request || function (u) { this.url = u; this.method = "GET"; };
  g.Response = g.Response || function () { this.ok = true; this.status = 200; this.json = function () { return Promise.resolve({}); }; };
  g.MutationObserver = g.MutationObserver || function () { this.observe = function () {}; this.disconnect = function () {}; this.takeRecords = function () { return []; }; };
  g.IntersectionObserver = g.IntersectionObserver || function () { this.observe = function () {}; this.disconnect = function () {}; this.unobserve = function () {}; };
  g.ResizeObserver = g.ResizeObserver || function () { this.observe = function () {}; this.disconnect = function () {}; this.unobserve = function () {}; };
  g.PerformanceObserver = g.PerformanceObserver || function () { this.observe = function () {}; this.disconnect = function () {}; };
  g.MessageChannel = g.MessageChannel || function () { this.port1 = { postMessage: function () {}, onmessage: null }; this.port2 = { postMessage: function () {}, onmessage: null }; };
  g.BroadcastChannel = g.BroadcastChannel || function () { this.postMessage = function () {}; this.close = function () {}; };
  g.Worker = g.Worker || function () { this.postMessage = function () {}; this.terminate = function () {}; };
  g.SharedWorker = g.SharedWorker || function () { this.port = { postMessage: function () {} }; };
  g.WebSocket = g.WebSocket || function () { this.send = function () {}; this.close = function () {}; this.readyState = 3; };
  g.XMLHttpRequest = g.XMLHttpRequest || function () { this.open = function () {}; this.send = function () {}; this.setRequestHeader = function () {}; this.status = 0; this.responseText = ""; };
  g.Image = g.Image || function () { this.src = ""; this.onload = null; this.onerror = null; };
  g.HTMLElement = g.HTMLElement || function () {};
  g.HTMLDocument = g.HTMLDocument || function () {};
  g.Event = g.Event || function (t) { this.type = t; this.bubbles = false; this.cancelable = false; };
  g.CustomEvent = g.CustomEvent || function (t) { this.type = t; this.detail = null; };
  g.DOMParser = g.DOMParser || function () { this.parseFromString = function () { return { documentElement: g.document.documentElement }; }; };
  g.getComputedStyle = g.getComputedStyle || function () { return { getPropertyValue: function () { return ""; } }; };
  g.requestAnimationFrame = g.requestAnimationFrame || function (fn) { return setTimeout(fn, 16); };
  g.cancelAnimationFrame = g.cancelAnimationFrame || function (id) { clearTimeout(id); };
  g.queueMicrotask = g.queueMicrotask || function (fn) { Promise.resolve().then(fn); };
  g.structuredClone = g.structuredClone || function (v) { return JSON.parse(JSON.stringify(v)); };

  // ---- __oai_so_*：会话行为遥测累加器 ----
  // create_account 需要第二个 sentinel 头 openai-sentinel-so-token，其内容由
  // /sentinel/req 里的 so.collector_dx + so.snapshot_dx 两段 DX 程序读取一批
  // window.__oai_so_* 累加器算出（真实页面通过 keydown/pointermove/click/scroll/
  // wheel 事件监听器实时累计）。协议模式无真实事件，这里按“人在 about-you 页停留
  // ~8 秒、输入姓名+生日、移动鼠标、点击”预置自洽的人类行为值，保证 collector
  // 不读到 undefined 且产出结构完整的 so-token。
  (function () {
    var nowP = (g.performance && g.performance.now) ? g.performance.now() : 1500;
    var t0 = nowP - 8085.5;              // 进页到采集约 8 秒
    // keydown 事件序列（输入姓名 "Anna Wilson" + 生日 ~ 12 次按键）
    var kd = [];
    var keys = ["A","n","n","a"," ","W","i","l","s","o","n","2"];
    var kt = t0 + 1200;
    for (var i = 0; i < keys.length; i++) {
      kt += 140 + Math.random() * 90;
      kd.push({ ctrlKey: false, metaKey: false, altKey: false, shiftKey: (i === 0 || i === 5), key: keys[i], type: "keydown", t: kt });
    }
    // pointermove 事件序列（鼠标移动，累计路径长度）
    var pm = [];
    var px = 640, py = 360, pt = t0 + 300, htot = 0, pcnt = 0;
    for (var j = 0; j < 30; j++) {
      var dx = (Math.random() - 0.5) * 40, dy = (Math.random() - 0.5) * 30;
      px += dx; py += dy; pt += 60 + Math.random() * 80;
      htot += Math.hypot(dx, dy); pcnt++;
      pm.push({ clientX: Math.round(px), clientY: Math.round(py), type: "pointermove", t: pt });
    }
    g.__oai_so_t0 = t0;
    g.__oai_so_h = kd;          // keydown 事件数组
    g.__oai_so_hi = kd.length;
    g.__oai_so_hp = 0;
    g.__oai_so_hw = 0;
    g.__oai_so_s = 0;
    g.__oai_so_k = kd.length;   // 按键计数
    g.__oai_so_kp = 0;
    g.__oai_so_we = 0;          // wheel 事件数
    g.__oai_so_wb = 0;
    g.__oai_so_wl = 0;
    g.__oai_so_fs = 0;
    g.__oai_so_fs2 = 0;
    g.__oai_so_fn = 0;
    g.__oai_so_p = pm;          // pointer 事件数组
    g.__oai_so_pc = pcnt;       // pointer 计数
    g.__oai_so_i = [];          // input 事件数组
    g.__oai_so_m = pm.length;   // mousemove 计数
    g.__oai_so_ht = htot;       // 鼠标路径总长（hypot 累加）
    g.__oai_so_hc = pcnt;       // hypot 计数
    g.__oai_so_bc = 2;          // click 计数
    g.__oai_so_bm = 0;
    g.__oai_so_ss = 0;          // scroll 累加
    g.__oai_so_ss2 = 0;
    g.__oai_so_sn = 0;          // scroll 计数
    g.__oai_so_cs = 0;          // click 累加
    g.__oai_so_cs2 = 0;
    g.__oai_so_cn = 2;          // click 计数
    g.__oai_so_st = 0;          // scrollTop
    g.__oai_so_sw = 0;
    g.__oai_so_sp = 0;
    g.__oai_so_spt = 0;
    g.__oai_so_sx0 = 640;
    g.__oai_so_sy0 = 360;
    g.__oai_so_lx = Math.round(px);
    g.__oai_so_ly = Math.round(py);
  })();
})();
