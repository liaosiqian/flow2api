let ws = null;
let reconnectTimeout = null;
let heartbeatInterval = null;
const recentCaptchaTabs = new Map();

const DEFAULT_SETTINGS = {
    serverUrl: "ws://127.0.0.1:8000/captcha_ws",
    apiKey: "",
    routeKey: "",
    clientLabel: ""
};

function getSettings() {
    return new Promise((resolve) => {
        chrome.storage.local.get(DEFAULT_SETTINGS, (stored) => {
            resolve({
                serverUrl: (stored.serverUrl || DEFAULT_SETTINGS.serverUrl).trim(),
                apiKey: (DEFAULT_SETTINGS.apiKey || stored.apiKey || "").trim(),
                routeKey: (DEFAULT_SETTINGS.routeKey || stored.routeKey || "").trim(),
                clientLabel: (DEFAULT_SETTINGS.clientLabel || stored.clientLabel || "").trim()
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
        try {
            ws.close();
        } catch (e) {
            console.log("[Flow2API] Close socket error", e);
        }
        ws = null;
    }
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function waitForTabReady(tabId, timeoutMs = 12000) {
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
            if (updatedTabId === tabId && changeInfo.status === "complete") {
                finish();
            }
        };
        const timer = setTimeout(finish, timeoutMs);

        chrome.tabs.onUpdated.addListener(onUpdated);
        chrome.tabs.get(tabId, (tab) => {
            if (chrome.runtime.lastError) {
                finish();
                return;
            }
            if (tab && tab.status === "complete") {
                finish();
            }
        });
    });
}

function getRouteKey(data) {
    return String((data && (data.route_key || data.routeKey || data.client_label || data.clientLabel)) || "").trim();
}

async function removeTabQuietly(tabId) {
    if (!tabId) return;
    try {
        await chrome.tabs.remove(tabId);
    } catch (e) {}
}

function rememberCaptchaTab(routeKey, tabId) {
    if (!routeKey || !tabId) return;
    const previous = recentCaptchaTabs.get(routeKey);
    if (previous && previous.tabId && previous.tabId !== tabId) {
        removeTabQuietly(previous.tabId);
    }
    recentCaptchaTabs.set(routeKey, {
        tabId,
        createdAt: Date.now(),
        inUse: false
    });
    setTimeout(() => {
        const current = recentCaptchaTabs.get(routeKey);
        if (!current || current.tabId !== tabId || current.inUse) return;
        recentCaptchaTabs.delete(routeKey);
        removeTabQuietly(tabId);
    }, 90000);
}

async function takeCaptchaTab(routeKey) {
    if (!routeKey) return null;
    const entry = recentCaptchaTabs.get(routeKey);
    if (!entry || !entry.tabId || entry.inUse) return null;
    if (Date.now() - entry.createdAt > 90000) {
        recentCaptchaTabs.delete(routeKey);
        await removeTabQuietly(entry.tabId);
        return null;
    }
    try {
        await chrome.tabs.get(entry.tabId);
    } catch (e) {
        recentCaptchaTabs.delete(routeKey);
        return null;
    }
    entry.inUse = true;
    recentCaptchaTabs.delete(routeKey);
    return entry.tabId;
}

async function connectWS() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

    const settings = await getSettings();
    const url = new URL(settings.serverUrl || DEFAULT_SETTINGS.serverUrl);
    if (settings.apiKey) {
        url.searchParams.set("key", settings.apiKey);
    }
    if (settings.routeKey) {
        url.searchParams.set("route_key", settings.routeKey);
    }
    if (settings.clientLabel) {
        url.searchParams.set("client_label", settings.clientLabel);
    }

    ws = new WebSocket(url.toString());

    ws.onopen = () => {
        console.log("[Flow2API] Background connected to WebSocket", url.toString());
        ws.send(JSON.stringify({
            type: "register",
            route_key: settings.routeKey,
            client_label: settings.clientLabel
        }));
        if (heartbeatInterval) clearInterval(heartbeatInterval);
        heartbeatInterval = setInterval(() => {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: "ping" }));
            }
        }, 20000);
    };

    let tokenQueue = Promise.resolve();
    let generationQueue = Promise.resolve();

    ws.onmessage = async (event) => {
        let data;
        try {
            data = JSON.parse(event.data);
        } catch (e) {
            return;
        }

        if (data.type === "register_ack") {
            console.log("[Flow2API] Registered route key:", data.route_key || "(empty)");
            return;
        }

        if (data.type === "get_token") {
            tokenQueue = tokenQueue.then(() => handleGetToken(data)).catch(err => {
                console.error("[Flow2API] Queue Error:", err);
            });
        }

        if (data.type === "submit_generation" || data.type === "poll_generation") {
            generationQueue = generationQueue.then(() => handleGenerationRequest(data)).catch(err => {
                console.error("[Flow2API] Generation Queue Error:", err);
            });
        }

        if (data.type === "open_labs_refresh") {
            handleOpenLabsRefresh(data).catch(err => {
                console.error("[Flow2API] OpenLabsRefresh Error:", err);
            });
        }

        if (data.type === "refresh_session") {
            handleRefreshSession(data).catch(err => {
                console.error("[Flow2API] RefreshSession Error:", err);
            });
        }
    };

    ws.onclose = () => {
        console.log("[Flow2API] WebSocket Closed. Reconnecting in 2s...");
        ws = null;
        if (heartbeatInterval) clearInterval(heartbeatInterval);
        if (reconnectTimeout) clearTimeout(reconnectTimeout);
        reconnectTimeout = setTimeout(connectWS, 2000);
    };

    ws.onerror = (e) => {
        console.log("[Flow2API] WebSocket Error", e);
    };
}

async function handleGetToken(data) {
    let newTabId = null;
    let keepTabForGeneration = false;
    try {
        console.log("[Flow2API] Auto-opening fresh Google Labs tab to avoid token expiry...");
        const newTab = await chrome.tabs.create({ url: "https://labs.google/fx/tools/flow", active: false });
        newTabId = newTab.id;

        await waitForTabReady(newTabId);
        await sleep(1200);

        let successResponse = null;
        let lastErrorMsg = "No response from tab.";
        const scriptTimeoutMs = data.action === "VIDEO_GENERATION" ? 30000 : 20000;

        try {
            const results = await chrome.scripting.executeScript({
                target: { tabId: newTabId },
                world: "MAIN",
                func: async (action, timeoutMs) => {
                    return new Promise((resolve, reject) => {
                        let settled = false;
                        const finish = (fn, value) => {
                            if (settled) return;
                            settled = true;
                            fn(value);
                        };
                        try {
                            function run() {
                                grecaptcha.enterprise.ready(function() {
                                    grecaptcha.enterprise.execute("6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV", { action: action })
                                        .then(token => finish(resolve, token))
                                        .catch(err => finish(reject, err.message || "reCAPTCHA evaluation failed internally"));
                                });
                            }

                            if (typeof grecaptcha !== "undefined" && grecaptcha.enterprise) {
                                run();
                            } else {
                                const s = document.createElement("script");
                                s.src = "https://www.google.com/recaptcha/enterprise.js?render=6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV";
                                s.onload = run;
                                s.onerror = () => finish(reject, "Failed to load enterprise.js via network");
                                document.head.appendChild(s);
                            }

                            setTimeout(() => finish(reject, "Timeout generating reCAPTCHA locally"), timeoutMs);
                        } catch (e) {
                            finish(reject, e.message);
                        }
                    });
                },
                args: [data.action || "IMAGE_GENERATION", scriptTimeoutMs]
            });

            if (results && results[0] && results[0].result) {
                successResponse = { status: "success", token: results[0].result };
            }
        } catch (e) {
            lastErrorMsg = e.message || "Script execution failed";
        }

        if (successResponse) {
            const routeKey = getRouteKey(data);
            if (routeKey) {
                rememberCaptchaTab(routeKey, newTabId);
                keepTabForGeneration = true;
            }
            ws.send(JSON.stringify({
                req_id: data.req_id,
                status: successResponse.status,
                token: successResponse.token
            }));
        } else {
            ws.send(JSON.stringify({
                req_id: data.req_id,
                status: "error",
                error: "Extension script failed: " + lastErrorMsg
            }));
        }
    } catch (err) {
        ws.send(JSON.stringify({
            req_id: data.req_id,
            status: "error",
            error: err.message
        }));
    } finally {
        if (newTabId && !keepTabForGeneration) {
            try {
                await chrome.tabs.remove(newTabId);
                console.log("[Flow2API] Closed temporary token tab.");
            } catch (e) {
                console.log("[Flow2API] Error closing tab:", e);
            }
        }
    }
}

async function handleGenerationRequest(data) {
    const FLOW_URL = "https://labs.google/fx/tools/flow";
    let newTabId = null;
    let reusedCaptchaTab = false;

    function sendResult(payload) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                req_id: data.req_id,
                ...payload
            }));
        }
    }

    try {
        const routeKey = getRouteKey(data);
        newTabId = await takeCaptchaTab(routeKey);
        if (newTabId) {
            reusedCaptchaTab = true;
            console.log("[Flow2API] Reusing captcha tab for generation:", routeKey || "(empty)");
            await waitForTabReady(newTabId, 5000);
            await sleep(200);
        } else {
            const tab = await chrome.tabs.create({ url: FLOW_URL, active: false });
            newTabId = tab.id;
            await waitForTabReady(newTabId, 20000);
            await sleep(800);
        }

        const results = await chrome.scripting.executeScript({
            target: { tabId: newTabId },
            world: "MAIN",
            func: async (request) => {
                const method = String(request.method || "POST").toUpperCase();
                const headers = request.headers && typeof request.headers === "object"
                    ? request.headers
                    : {};
                const hasBody = !["GET", "HEAD"].includes(method);
                const controller = new AbortController();
                const timeoutMs = Math.max(5000, Number(request.timeout_ms || 60000));
                const timer = setTimeout(() => controller.abort(), timeoutMs);

                try {
                    const fetchHeaders = { ...headers };
                    const response = await fetch(request.url, {
                        method,
                        headers: fetchHeaders,
                        body: hasBody ? JSON.stringify(request.json_data || {}) : undefined,
                        credentials: "include",
                        referrer: "https://labs.google/fx/tools/flow",
                        referrerPolicy: "strict-origin-when-cross-origin",
                        signal: controller.signal
                    });
                    const responseText = await response.text();
                    let responseJson = null;
                    try {
                        responseJson = responseText ? JSON.parse(responseText) : null;
                    } catch (e) {}
                    return {
                        ok: response.ok,
                        status: response.status,
                        statusText: response.statusText,
                        text: responseText,
                        json: responseJson
                    };
                } finally {
                    clearTimeout(timer);
                }
            },
            args: [{
                url: data.url,
                method: data.method || "POST",
                headers: data.headers || {},
                json_data: data.json_data || {},
                timeout_ms: Number(data.timeout_ms || data.timeout_seconds || 60000)
            }]
        });

        const result = results && results[0] && results[0].result;
        if (!result) {
            sendResult({ status: "error", error: "No generation response from browser context" });
            return;
        }

        sendResult({
            status: "success",
            response_status: result.status,
            response_text: result.text || "",
            response_json: result.json || null
        });
    } catch (err) {
        sendResult({
            status: "error",
            error: err && err.message ? err.message : String(err)
        });
    } finally {
        if (newTabId) {
            try {
                await chrome.tabs.remove(newTabId);
                if (reusedCaptchaTab) {
                    console.log("[Flow2API] Closed reused captcha generation tab.");
                }
            } catch (e) {}
        }
    }
}

async function handleOpenLabsRefresh(data) {
    const COOKIE_NAME = "__Secure-next-auth.session-token";
    const COOKIE_URL = "https://labs.google";
    const FLOW_URL = "https://labs.google/fx/tools/flow";
    let newTabId = null;

    function sendResult(status, sessionToken, error, extra) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                req_id: data.req_id,
                status: status,
                session_token: sessionToken || null,
                error: error || null,
                final_url: extra && extra.final_url || null,
                cookie_expires: extra && extra.cookie_expires || null
            }));
        }
    }

    try {
        const waitMs = Math.max(1000, Number(data.wait_ms || 12000));
        const reloadWaitMs = Math.max(1000, Number(data.reload_wait_ms || 12000));
        const tab = await chrome.tabs.create({
            url: FLOW_URL,
            active: data.active === true
        });
        newTabId = tab.id;
        await waitForTabReady(newTabId, 20000);
        await sleep(waitMs);
        await chrome.tabs.reload(newTabId);
        await waitForTabReady(newTabId, 20000);
        await sleep(reloadWaitMs);

        let finalUrl = null;
        try {
            const tabInfo = await chrome.tabs.get(newTabId);
            finalUrl = tabInfo && tabInfo.url || null;
        } catch (e) {}

        const cookie = await chrome.cookies.get({ url: COOKIE_URL, name: COOKIE_NAME });
        if (cookie && cookie.value) {
            console.log("[Flow2API] OpenLabsRefresh: got session token, len=" + cookie.value.length + ", final_url=" + (finalUrl || "-"));
            sendResult("success", cookie.value, null, {
                final_url: finalUrl,
                cookie_expires: cookie.expirationDate || null
            });
        } else {
            sendResult("error", null, "No session cookie after opening Labs", { final_url: finalUrl });
        }
    } catch (err) {
        console.error("[Flow2API] OpenLabsRefresh failed:", err);
        sendResult("error", null, err.message || String(err), null);
    } finally {
        if (newTabId && data.keep_open !== true) {
            try {
                await chrome.tabs.remove(newTabId);
            } catch (e) {}
        }
    }
}

async function handleRefreshSession(data) {
    const COOKIE_NAME = "__Secure-next-auth.session-token";
    const COOKIE_URL = "https://labs.google";
    let newTabId = null;

    function sendResult(status, sessionToken, error) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                req_id: data.req_id,
                status: status,
                session_token: sessionToken || null,
                error: error || null
            }));
        }
    }

    try {
        // Step 1: delete old session cookie
        await chrome.cookies.remove({ url: COOKIE_URL, name: COOKIE_NAME });
        console.log("[Flow2API] RefreshSession: deleted old session cookie");

        // Step 2: open tab to labs.google
        const tab = await chrome.tabs.create({
            url: "https://labs.google/fx/tools/flow",
            active: false
        });
        newTabId = tab.id;
        await waitForTabReady(newTabId, 10000);
        await sleep(2000);

        // Step 3: trigger Google OAuth via NextAuth sign-in
        const signinResults = await chrome.scripting.executeScript({
            target: { tabId: newTabId },
            world: "MAIN",
            func: async () => {
                try {
                    const csrfResp = await fetch("/fx/api/auth/csrf");
                    const csrfData = await csrfResp.json();
                    const csrfToken = csrfData.csrfToken;
                    if (!csrfToken) return JSON.stringify({ error: "no csrf token" });

                    const formData = new URLSearchParams();
                    formData.append("csrfToken", csrfToken);
                    formData.append("callbackUrl", "https://labs.google/fx/tools/flow");
                    formData.append("json", "true");

                    const resp = await fetch("/fx/api/auth/signin/google", {
                        method: "POST",
                        headers: { "Content-Type": "application/x-www-form-urlencoded" },
                        body: formData.toString(),
                    });
                    const body = await resp.json();
                    return JSON.stringify({ url: body.url || null });
                } catch (e) {
                    return JSON.stringify({ error: e.message });
                }
            },
            args: []
        });

        const signinResult = JSON.parse(signinResults[0].result);
        const oauthUrl = signinResult.url;

        if (!oauthUrl) {
            sendResult("error", null, "Failed to get OAuth URL: " + JSON.stringify(signinResult));
            return;
        }

        // Step 4: navigate to Google OAuth (auto-login via existing Google cookies)
        await chrome.tabs.update(newTabId, { url: oauthUrl });

        // Wait for redirect back to labs.google
        for (let i = 0; i < 20; i++) {
            await sleep(1500);
            try {
                const tabInfo = await chrome.tabs.get(newTabId);
                if (tabInfo.url && tabInfo.url.includes("labs.google") && !tabInfo.url.includes("accounts.google")) {
                    break;
                }
            } catch (e) {
                break;
            }
        }

        await sleep(2000);

        // Step 5: extract new session cookie
        const cookie = await chrome.cookies.get({ url: COOKIE_URL, name: COOKIE_NAME });
        if (cookie && cookie.value) {
            console.log("[Flow2API] RefreshSession: got new session token, len=" + cookie.value.length);
            sendResult("success", cookie.value, null);
        } else {
            sendResult("error", null, "No session cookie after OAuth flow");
        }
    } catch (err) {
        console.error("[Flow2API] RefreshSession failed:", err);
        sendResult("error", null, err.message);
    } finally {
        if (newTabId) {
            try {
                await chrome.tabs.remove(newTabId);
            } catch (e) {
                // tab may already be closed
            }
        }
    }
}

chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== "local") return;
    if (changes.routeKey || changes.serverUrl || changes.apiKey || changes.clientLabel) {
        console.log("[Flow2API] Extension settings changed, reconnecting WebSocket...");
        closeSocket();
        connectWS();
    }
});

// MV3: service worker is lazy — must register startup/install events to ensure activation
chrome.runtime.onStartup.addListener(() => {
    console.log("[Flow2API] Chrome started (onStartup), connecting WebSocket...");
    connectWS();
});

chrome.runtime.onInstalled.addListener(() => {
    console.log("[Flow2API] Extension installed/updated (onInstalled), connecting WebSocket...");
    connectWS();
});

chrome.alarms.create("keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "keepalive") {
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            console.log("[Flow2API] Keepalive: WebSocket not open, reconnecting...");
            connectWS();
        }
    }
});

connectWS();
