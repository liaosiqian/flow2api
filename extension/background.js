let ws = null;
let reconnectTimeout = null;
let heartbeatInterval = null;

const DEFAULT_SETTINGS = {
  serverUrl: "ws://127.0.0.1:8000/captcha_ws",
  apiKey: "han1234",
  routeKey: "",
  clientLabel: ""
};

const STORAGE_WORKER_TAB_ID = "extensionWorkerTabId";
const DEFAULT_WORKER_PAGE_URL = "https://labs.google/fx/tools/flow";
const USE_PERSISTENT_WORKER_TAB = true;
const AUTO_RECYCLE_ON_FAILURE = true;
const RECAPTCHA_SETTLE_MS = 3000;

const runtimeState = {
  wsStatus: "idle",
  lastError: "",
  workerTabId: null,
  captchaJobsSucceeded: 0,
  captchaJobsFailed: 0,
  events: [],
};

const EVENTS_MAX = 50;

function pushEvent(type, detail, level) {
  runtimeState.events.push({
    ts: Date.now(),
    type,
    detail: String(detail || "").slice(0, 200),
    level: level || "info"
  });
  if (runtimeState.events.length > EVENTS_MAX) {
    runtimeState.events = runtimeState.events.slice(-EVENTS_MAX);
  }
}

function getSettings() {
  return new Promise((resolve) => {
    chrome.storage.local.get(DEFAULT_SETTINGS, (stored) => {
      resolve({
        serverUrl: (stored.serverUrl || DEFAULT_SETTINGS.serverUrl).trim(),
        apiKey: (stored.apiKey || "").trim(),
        routeKey: (stored.routeKey || "").trim(),
        clientLabel: (stored.clientLabel || "").trim()
      });
    });
  });
}

function closeSocket() {
  if (heartbeatInterval) clearInterval(heartbeatInterval);
  heartbeatInterval = null;
  if (reconnectTimeout) clearTimeout(reconnectTimeout);
  reconnectTimeout = null;
  if (ws) {
    try { ws.close(); } catch (e) {}
    ws = null;
  }
  runtimeState.wsStatus = "closed";
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function waitForTabReady(tabId, timeoutMs = 15000) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(onUpdated);
      clearTimeout(timer);
      resolve();
    };
    const onUpdated = (updatedTabId, changeInfo) => {
      if (updatedTabId === tabId && changeInfo.status === "complete") finish();
    };
    const timer = setTimeout(finish, timeoutMs);
    chrome.tabs.onUpdated.addListener(onUpdated);
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError) { finish(); return; }
      if (tab && tab.status === "complete") finish();
    });
  });
}

function persistWorkerTabId(tabId) {
  chrome.storage.local.set({ [STORAGE_WORKER_TAB_ID]: tabId });
}

function isLabsFlowSurface(url) {
  if (!url) return false;
  try {
    const u = new URL(url);
    return u.hostname === "labs.google" && u.pathname.startsWith("/fx/");
  } catch (_) { return false; }
}

async function ensurePersistentWorkerTab() {
  let tabId = runtimeState.workerTabId;
  if (tabId != null) {
    const tab = await new Promise((resolve) => {
      chrome.tabs.get(tabId, (t) => {
        if (chrome.runtime.lastError) resolve(null);
        else resolve(t);
      });
    });
    if (!tab) {
      tabId = null;
      runtimeState.workerTabId = null;
      persistWorkerTabId(null);
    } else {
      const currentUrl = tab.url || tab.pendingUrl || "";
      if (!isLabsFlowSurface(currentUrl)) {
        await new Promise((r) => { chrome.tabs.update(tabId, { url: DEFAULT_WORKER_PAGE_URL }, () => r()); });
        await waitForTabReady(tabId);
        await sleep(RECAPTCHA_SETTLE_MS);
      } else {
        await waitForTabReady(tabId);
      }
      return tabId;
    }
  }
  console.log("[Flow2API] Creating persistent worker tab:", DEFAULT_WORKER_PAGE_URL);
  const newTab = await chrome.tabs.create({ url: DEFAULT_WORKER_PAGE_URL, active: false });
  tabId = newTab.id;
  runtimeState.workerTabId = tabId;
  persistWorkerTabId(tabId);
  await waitForTabReady(tabId);
  await sleep(RECAPTCHA_SETTLE_MS);
  pushEvent("worker_tab_created", "Worker tab created");
  return tabId;
}

async function recyclePersistentWorkerTab(reason) {
  const oldId = runtimeState.workerTabId;
  if (oldId != null) {
    try { await chrome.tabs.remove(oldId); } catch (_) {}
  }
  runtimeState.workerTabId = null;
  persistWorkerTabId(null);
  console.log("[Flow2API] Recycling worker tab:", reason);
  const newTab = await chrome.tabs.create({ url: DEFAULT_WORKER_PAGE_URL, active: false });
  runtimeState.workerTabId = newTab.id;
  persistWorkerTabId(newTab.id);
  await waitForTabReady(newTab.id);
  await sleep(RECAPTCHA_SETTLE_MS);
  pushEvent("worker_tab_recycled", reason);
  return newTab.id;
}

async function generateTokenWithPersistentTab(action) {
  try {
    let tabId = await ensurePersistentWorkerTab();
    const result = await executeRecaptchaInTab(tabId, action);
    if (result.success) {
      runtimeState.lastError = "";
      return result;
    }
    runtimeState.lastError = result.error;
    if (AUTO_RECYCLE_ON_FAILURE) {
      await recyclePersistentWorkerTab("captcha_failure: " + result.error);
    }
    return result;
  } catch (err) {
    const msg = err.message || "unknown_error";
    runtimeState.lastError = msg;
    if (AUTO_RECYCLE_ON_FAILURE && runtimeState.workerTabId != null) {
      try { await recyclePersistentWorkerTab("captcha_exception: " + msg); } catch (_) {}
    }
    return { success: false, error: msg };
  }
}

async function generateTokenInFreshTab(action) {
  let newTabId = null;
  try {
    const newTab = await chrome.tabs.create({ url: DEFAULT_WORKER_PAGE_URL, active: false });
    newTabId = newTab.id;
    await waitForTabReady(newTabId);
    await sleep(RECAPTCHA_SETTLE_MS);
    const result = await executeRecaptchaInTab(newTabId, action);
    if (result.success) runtimeState.lastError = "";
    else runtimeState.lastError = result.error;
    return result;
  } catch (err) {
    runtimeState.lastError = err.message || "unknown_error";
    return { success: false, error: err.message || "unknown_error" };
  } finally {
    if (newTabId) {
      try { await chrome.tabs.remove(newTabId); } catch (_) {}
    }
  }
}

async function executeRecaptchaInTab(tabId, action) {
  const scriptTimeoutMs = action === "VIDEO_GENERATION" ? 30000 : 20000;
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: async (action, timeoutMs) => {
        return new Promise((resolve, reject) => {
          let settled = false;
          const finish = (fn, value) => { if (settled) return; settled = true; fn(value); };
          try {
            function run() {
              grecaptcha.enterprise.ready(function() {
                grecaptcha.enterprise.execute("6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV", { action })
                  .then(token => finish(resolve, token))
                  .catch(err => finish(reject, err.message || "reCAPTCHA evaluation failed"));
              });
            }
            if (typeof grecaptcha !== "undefined" && grecaptcha.enterprise) {
              run();
            } else {
              const s = document.createElement("script");
              s.src = "https://www.google.com/recaptcha/enterprise.js?render=6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV";
              s.onload = run;
              s.onerror = () => finish(reject, "Failed to load enterprise.js");
              document.head.appendChild(s);
            }
            setTimeout(() => finish(reject, "Timeout generating reCAPTCHA"), timeoutMs);
          } catch (e) { finish(reject, e.message); }
        });
      },
      args: [action, scriptTimeoutMs]
    });
    if (results && results[0] && results[0].result) {
      return { success: true, token: results[0].result };
    }
    return { success: false, error: "No result from script execution" };
  } catch (e) {
    return { success: false, error: e.message || "Script execution failed" };
  }
}

async function generateTokenForCaptcha(action) {
  if (USE_PERSISTENT_WORKER_TAB) {
    return generateTokenWithPersistentTab(action);
  }
  return generateTokenInFreshTab(action);
}

async function connectWS() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  const settings = await getSettings();
  runtimeState.wsStatus = "connecting";
  pushEvent("connect_start", "Connecting to " + (settings.serverUrl || DEFAULT_SETTINGS.serverUrl));

  const url = new URL(settings.serverUrl || DEFAULT_SETTINGS.serverUrl);
  if (settings.apiKey) url.searchParams.set("key", settings.apiKey);
  if (settings.routeKey) url.searchParams.set("route_key", settings.routeKey);
  if (settings.clientLabel) url.searchParams.set("client_label", settings.clientLabel);

  const socket = new WebSocket(url.toString());
  ws = socket;

  socket.onopen = () => {
    if (socket !== ws) return;
    console.log("[Flow2API] WebSocket connected:", url.toString());
    runtimeState.wsStatus = "open";
    pushEvent("connect_open", "WebSocket connected");
    socket.send(JSON.stringify({
      type: "register",
      route_key: settings.routeKey,
      client_label: settings.clientLabel
    }));
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    heartbeatInterval = setInterval(() => {
      if (socket === ws && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "ping" }));
      }
    }, 20000);
  };

  let tokenQueue = Promise.resolve();

  socket.onmessage = async (event) => {
    if (socket !== ws) return;
    let data;
    try { data = JSON.parse(event.data); } catch (e) { return; }

    if (data.type === "register_ack") {
      console.log("[Flow2API] Registered, route_key:", data.route_key || "(empty)");
      pushEvent("register_ack", "Registered successfully");
      return;
    }

    if (data.type === "get_token") {
      tokenQueue = tokenQueue.then(() => handleGetToken(data)).catch(err => {
        console.error("[Flow2API] Queue Error:", err);
      });
      return;
    }

    if (data.type === "submit_generation") {
      tokenQueue = tokenQueue.then(() => handleGenerationRequest(data, "submit_generation")).catch(err => {
        console.error("[Flow2API] submit_generation error:", err);
      });
      return;
    }
    if (data.type === "poll_generation") {
      tokenQueue = tokenQueue.then(() => handleGenerationRequest(data, "poll_generation")).catch(err => {
        console.error("[Flow2API] poll_generation error:", err);
      });
      return;
    }
  };

  socket.onclose = () => {
    if (socket !== ws) return;
    console.log("[Flow2API] WebSocket closed. Reconnecting in 2s...");
    runtimeState.wsStatus = "closed";
    pushEvent("connect_close", "WebSocket closed, reconnect scheduled", "warn");
    ws = null;
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    if (reconnectTimeout) clearTimeout(reconnectTimeout);
    reconnectTimeout = setTimeout(connectWS, 2000);
  };

  socket.onerror = (e) => {
    if (socket !== ws) return;
    console.log("[Flow2API] WebSocket Error", e);
    runtimeState.wsStatus = "error";
    runtimeState.lastError = "websocket_error";
    pushEvent("connect_error", "WebSocket transport error", "error");
  };
}

async function handleGetToken(data) {
  const action = data.action || "IMAGE_GENERATION";
  const result = await generateTokenForCaptcha(action);

  if (result.success) runtimeState.captchaJobsSucceeded++;
  else runtimeState.captchaJobsFailed++;

  if (!ws || ws.readyState !== WebSocket.OPEN) return;

  if (result.success) {
    ws.send(JSON.stringify({
      req_id: data.req_id,
      status: "success",
      token: result.token
    }));
  } else {
    ws.send(JSON.stringify({
      req_id: data.req_id,
      status: "error",
      error: result.error || "unknown_error"
    }));
  }
}

// === Generation Proxy ===

const ALLOWED_CORS_HEADERS = new Set([
  "authorization", "content-type", "accept", "accept-language",
  "user-agent", "referer",
  "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
  "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
]);

function filterHeadersForLabsGoogleCors(rawHeaders) {
  const out = {};
  if (!rawHeaders || typeof rawHeaders !== "object") return out;
  for (const [k, v] of Object.entries(rawHeaders)) {
    if (v == null || v === "") continue;
    const key = String(k).trim();
    if (!key || !ALLOWED_CORS_HEADERS.has(key.toLowerCase())) continue;
    out[key] = String(v);
  }
  if (!out.Referer && !out.referer) out["Referer"] = "https://labs.google/";
  return out;
}

async function executeHttpRequestInTab(tabId, request) {
  const scriptTimeoutMs = Math.max(15000, Math.min(120000, Number(request.timeout_ms) || 60000));
  const requestForPage = { ...request, headers: filterHeadersForLabsGoogleCors(request.headers || {}) };
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: async (req, timeoutMs) => {
        const ctl = new AbortController();
        const timer = setTimeout(() => ctl.abort(), timeoutMs);
        try {
          const method = String(req.method || "POST").toUpperCase();
          const headers = req.headers && typeof req.headers === "object" ? { ...req.headers } : {};
          const hasJson = req.json_data && typeof req.json_data === "object";
          const init = { method, headers, signal: ctl.signal };
          if (hasJson) {
            if (!init.headers["Content-Type"] && !init.headers["content-type"]) init.headers["Content-Type"] = "application/json";
            init.body = JSON.stringify(req.json_data);
          }
          const resp = await fetch(String(req.url || ""), init);
          const text = await resp.text();
          let parsed = null;
          try { parsed = text ? JSON.parse(text) : null; } catch (_) {}
          return { ok: !!resp.ok, status: Number(resp.status) || 0, response_text: String(text || ""), response_json: parsed };
        } catch (e) {
          return { ok: false, status: 0, error: String((e && e.message) || e || "request_failed"), response_text: "", response_json: null };
        } finally { clearTimeout(timer); }
      },
      args: [requestForPage, scriptTimeoutMs],
    });
    const payload = results && results[0] ? results[0].result : null;
    if (!payload || typeof payload !== "object") return { success: false, error: "empty_extension_http_response" };
    if (!payload.ok) {
      return { success: false, error: payload.error || ("HTTP " + (payload.status || 0)),
        response_status: Number(payload.status) || 0, response_text: String(payload.response_text || ""), response_json: payload.response_json || null };
    }
    return { success: true, response_status: Number(payload.status) || 200,
      response_text: String(payload.response_text || ""), response_json: payload.response_json || null };
  } catch (e) {
    return { success: false, error: String((e && e.message) || e || "script_execution_failed") };
  }
}

async function executeGenerationHttpRequest(request) {
  if (USE_PERSISTENT_WORKER_TAB) {
    const tabId = await ensurePersistentWorkerTab();
    return executeHttpRequestInTab(tabId, request);
  }
  let tempTabId = null;
  try {
    const tab = await chrome.tabs.create({ url: DEFAULT_WORKER_PAGE_URL, active: false });
    tempTabId = tab && tab.id ? tab.id : null;
    if (!tempTabId) return { success: false, error: "worker_tab_create_failed" };
    await waitForTabReady(tempTabId);
    await sleep(RECAPTCHA_SETTLE_MS);
    return await executeHttpRequestInTab(tempTabId, request);
  } finally {
    if (tempTabId) { try { await chrome.tabs.remove(tempTabId); } catch (_) {} }
  }
}

async function handleGenerationRequest(data, commandType) {
  const request = {
    url: String(data.url || "").trim(),
    method: String(data.method || "POST").trim().toUpperCase(),
    headers: data.headers && typeof data.headers === "object" ? data.headers : {},
    json_data: data.json_data && typeof data.json_data === "object" ? data.json_data : {},
    timeout_ms: Number(data.timeout_ms) || 60000,
  };
  if (!request.url) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ req_id: data.req_id, type: commandType + "_result", status: "error", error: "missing_url" }));
    }
    return;
  }
  console.log("[Flow2API] Generation proxy:", commandType, request.method, request.url.substring(0, 80));
  const result = await executeGenerationHttpRequest(request);
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (result.success) {
    ws.send(JSON.stringify({
      req_id: data.req_id, type: commandType + "_result", status: "success",
      response_status: result.response_status, response_text: result.response_text, response_json: result.response_json,
    }));
    pushEvent("generation_ok", commandType + " " + (result.response_status || 200));
  } else {
    ws.send(JSON.stringify({
      req_id: data.req_id, type: commandType + "_result", status: "error",
      error: result.error || "generation_request_failed",
      response_status: result.response_status || 0, response_text: result.response_text || "", response_json: result.response_json || null,
    }));
    pushEvent("generation_error", commandType + " " + (result.error || "failed"), "error");
  }
}

async function validateStoredWorkerTab() {
  const stored = await new Promise(r => chrome.storage.local.get(STORAGE_WORKER_TAB_ID, r));
  const tabId = stored[STORAGE_WORKER_TAB_ID];
  if (tabId == null) return;
  const tab = await new Promise(r => chrome.tabs.get(tabId, t => {
    if (chrome.runtime.lastError) r(null);
    else r(t);
  }));
  if (tab) {
    runtimeState.workerTabId = tabId;
    console.log("[Flow2API] Restored worker tab:", tabId);
  } else {
    persistWorkerTabId(null);
  }
}

chrome.tabs.onRemoved.addListener((tabId) => {
  if (runtimeState.workerTabId != null && tabId === runtimeState.workerTabId) {
    runtimeState.workerTabId = null;
    persistWorkerTabId(null);
    pushEvent("worker_tab_removed", "Worker tab closed", "warn");
  }
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== "local") return;
  if (changes.routeKey || changes.serverUrl || changes.apiKey || changes.clientLabel) {
    console.log("[Flow2API] Settings changed, reconnecting...");
    pushEvent("settings_changed", "Settings changed, reconnecting");
    closeSocket();
    connectWS();
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || !message.type) return;
  if (message.type === "ping") {
    sendResponse({ pong: true, wsStatus: runtimeState.wsStatus });
    return;
  }
  if (message.type === "get_status") {
    sendResponse({
      success: true,
      state: { ...runtimeState }
    });
    return true;
  }
  if (message.type === "reconnect_now") {
    pushEvent("manual_reconnect", "Manual reconnect triggered");
    closeSocket();
    connectWS().then(() => sendResponse({ success: true })).catch(err => sendResponse({ success: false, error: err.message }));
    return true;
  }
  if (message.type === "test_token") {
    pushEvent("test_token", "Test token started (" + (message.action || "IMAGE_GENERATION") + ")");
    generateTokenForCaptcha(message.action || "IMAGE_GENERATION")
      .then(result => sendResponse(result))
      .catch(err => sendResponse({ success: false, error: err.message }));
    return true;
  }
});

validateStoredWorkerTab().then(() => {
  pushEvent("startup", "Background worker started");
  connectWS();
});
