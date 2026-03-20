/**
 * SimpleClaw background service worker.
 *
 * Maintains a WebSocket connection to the simplebot backend.
 * Manages pinned tabs: toolbar icon toggles pin, badge shows state.
 * Routes commands from backend to content scripts and results back.
 *
 * Action routing strategy (CDP-first for reliability):
 *   - eval, click_text, keyboard, insertText, cdp_click, cdp_type → CDP directly
 *   - type → content script finds element, returns coords if CDP needed, else handles inline
 *   - click → content script (synthetic), with CDP fallback via needsCDP
 *   - read_page, scroll, select → content script
 *   - refresh, navigate → chrome.tabs API
 */

const DEFAULT_HOST = "127.0.0.1";
const DEFAULT_PORT = 18790;
const RECONNECT_DELAY_MS = 3000;
const BADGE_ON = "ON";
const BADGE_OFF = "";

let ws = null;
let wsConnected = false;
let pinnedTabs = new Set();
let debuggerAttached = new Set();
let config = { host: DEFAULT_HOST, port: DEFAULT_PORT, token: "" };

// ---- Config persistence ----

async function loadConfig() {
  const stored = await chrome.storage.local.get(["host", "port", "token"]);
  if (stored.host) config.host = stored.host;
  if (stored.port) config.port = stored.port;
  if (stored.token) config.token = stored.token;
}

async function saveConfig(newConfig) {
  Object.assign(config, newConfig);
  await chrome.storage.local.set(config);
}

function wsUrl() {
  return `ws://${config.host}:${config.port}/ws`;
}

function healthUrl() {
  return `http://${config.host}:${config.port}/health`;
}

async function fetchToken() {
  try {
    const resp = await fetch(healthUrl());
    const data = await resp.json();
    config.token = data.token || "__no_auth__";
    await chrome.storage.local.set({ token: config.token });
    config.backendReachable = true;
  } catch {
    config.backendReachable = false;
  }
}

// ---- WebSocket connection ----

async function connectWs() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  await fetchToken();
  if (!config.backendReachable) {
    scheduleReconnect();
    return;
  }

  try {
    ws = new WebSocket(wsUrl());
  } catch (err) {
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "auth", token: config.token }));
  };

  ws.onmessage = async (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch {
      return;
    }

    if (data.type === "auth_ok") {
      wsConnected = true;
      broadcastToSidePanel({ type: "ws_status", connected: true });

      for (const tabId of pinnedTabs) {
        await announceTab(tabId);
      }

      if (pinnedTabs.size === 0) {
        await autoDiscoverTabs();
      }
      return;
    }

    if (data.type === "auth_fail") {
      wsConnected = false;
      broadcastToSidePanel({ type: "ws_status", connected: false, error: "Auth failed" });
      ws.close();
      return;
    }

    if (data.type === "reload") {
      chrome.runtime.reload();
      return;
    }

    if (data.type === "execute") {
      await handleExecute(data);
      return;
    }
  };

  ws.onclose = () => {
    wsConnected = false;
    config.token = "";
    broadcastToSidePanel({ type: "ws_status", connected: false });
    scheduleReconnect();
  };

  ws.onerror = () => {
    wsConnected = false;
  };
}

function scheduleReconnect() {
  setTimeout(connectWs, RECONNECT_DELAY_MS);
}

function sendToBackend(data) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(data));
  }
}

// ---- Tab pin/unpin ----

async function togglePin(tabId) {
  if (pinnedTabs.has(tabId)) {
    await unpinTab(tabId);
  } else {
    await pinTab(tabId);
  }
}

async function pinTab(tabId) {
  pinnedTabs.add(tabId);
  await updateBadge(tabId, true);

  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"],
    });
  } catch {
    // May already be injected via manifest
  }

  try {
    await attachDebugger(tabId);
  } catch {
    // User may need to accept the debugger prompt
  }

  await announceTab(tabId);
  broadcastToSidePanel({ type: "tabs_changed", tabs: await getTabList() });
}

async function unpinTab(tabId) {
  pinnedTabs.delete(tabId);
  await updateBadge(tabId, false);
  await detachDebugger(tabId);
  sendToBackend({ type: "tab_unpinned", tab_id: tabId });
  broadcastToSidePanel({ type: "tabs_changed", tabs: await getTabList() });
}

async function announceTab(tabId) {
  if (!wsConnected) return;

  try {
    const [response] = await chrome.tabs.sendMessage(tabId, { type: "capture_state" })
      .then((r) => [r])
      .catch(() => [null]);

    if (response && response.success) {
      sendToBackend({
        type: "tab_pinned",
        tab_id: tabId,
        url: response.url,
        title: response.title,
        page_state: response.page_state,
      });
    } else {
      const tab = await chrome.tabs.get(tabId);
      sendToBackend({
        type: "tab_pinned",
        tab_id: tabId,
        url: tab.url || "",
        title: tab.title || "",
        page_state: "",
      });
    }
  } catch {
    // Tab might be closed or inaccessible
  }
}

async function updateBadge(tabId, isPinned) {
  try {
    await chrome.action.setBadgeText({ text: isPinned ? BADGE_ON : BADGE_OFF, tabId });
    await chrome.action.setBadgeBackgroundColor({ color: isPinned ? "#4285f4" : "#999", tabId });
  } catch {
    // Tab might not exist
  }
}

// ---- Execute actions from backend ----

async function handleExecute(data) {
  const { request_id, tab_id, action } = data;

  if (!pinnedTabs.has(tab_id)) {
    sendToBackend({
      type: "action_result",
      request_id,
      success: false,
      error: `Tab ${tab_id} is not pinned`,
    });
    return;
  }

  // ---- CDP-only actions ----
  // These bypass the content script entirely for maximum reliability.

  if (action === "refresh" || action === "navigate") {
    try {
      if (action === "refresh") {
        await chrome.tabs.reload(tab_id);
      } else {
        await chrome.tabs.update(tab_id, { url: data.url });
      }
      await waitForTabLoad(tab_id, 10000);
      await injectContentScript(tab_id);
      await sleep(500);
      const state = await captureTabState(tab_id);
      sendToBackend({
        type: "action_result", request_id,
        success: true,
        page_state: state.page_state || "", url: state.url || "", title: state.title || "",
      });
    } catch (err) {
      sendToBackend({
        type: "action_result", request_id,
        success: false, error: err.message,
      });
    }
    broadcastToSidePanel({ type: "action_log", text: `${action} on tab ${tab_id}` });
    return;
  }

  if (action === "eval") {
    try {
      await attachDebugger(tab_id);
      const evalResult = await cdpSend(tab_id, "Runtime.evaluate", {
        expression: data.code || "",
        returnByValue: true,
      });
      const value = evalResult?.result?.value;
      const errDesc = evalResult?.exceptionDetails?.exception?.description;
      if (errDesc) {
        sendToBackend({
          type: "action_result", request_id,
          success: false, error: `eval error: ${errDesc}`,
        });
      } else {
        await sleep(300);
        const state = await captureTabState(tab_id);
        sendToBackend({
          type: "action_result", request_id,
          success: true, page_state: state.page_state || "",
          url: state.url || "", title: state.title || "",
          evalResult: String(value ?? ""),
        });
      }
    } catch (err) {
      sendToBackend({
        type: "action_result", request_id,
        success: false, error: `eval error: ${err.message}`,
      });
    }
    broadcastToSidePanel({ type: "action_log", text: `eval on tab ${tab_id}` });
    return;
  }

  if (action === "click_text") {
    try {
      const result = await cdpClickText(tab_id, data.text || "");
      await sleep(500);
      const state = await captureTabState(tab_id);
      sendToBackend({
        type: "action_result", request_id,
        success: true,
        page_state: state.page_state || "", url: state.url || "", title: state.title || "",
        matchedText: result.matched || "",
      });
    } catch (err) {
      sendToBackend({
        type: "action_result", request_id,
        success: false, error: `click_text error: ${err.message}`,
      });
    }
    broadcastToSidePanel({ type: "action_log", text: `click_text "${data.text}" on tab ${tab_id}` });
    return;
  }

  if (action === "keyboard") {
    try {
      await cdpKeyboard(tab_id, {
        key: data.key || "", code: data.code || "",
        metaKey: !!data.metaKey, ctrlKey: !!data.ctrlKey,
        shiftKey: !!data.shiftKey, altKey: !!data.altKey,
      });
      await sleep(500);
      const state = await captureTabState(tab_id);
      sendToBackend({
        type: "action_result", request_id,
        success: true,
        page_state: state.page_state || "", url: state.url || "", title: state.title || "",
      });
    } catch (err) {
      sendToBackend({
        type: "action_result", request_id,
        success: false, error: `CDP keyboard error: ${err.message}`,
      });
    }
    broadcastToSidePanel({ type: "action_log", text: `keyboard ${data.key} on tab ${tab_id}` });
    return;
  }

  // insertText: CDP Input.insertText for reliable text entry in React/custom inputs.
  // Inserts text at the current cursor position without needing element coordinates.
  if (action === "insertText") {
    try {
      await attachDebugger(tab_id);
      await cdpSend(tab_id, "Input.insertText", { text: data.text || "" });
      await sleep(500);
      const state = await captureTabState(tab_id);
      sendToBackend({
        type: "action_result", request_id,
        success: true,
        page_state: state.page_state || "", url: state.url || "", title: state.title || "",
      });
    } catch (err) {
      sendToBackend({
        type: "action_result", request_id,
        success: false, error: `CDP insertText error: ${err.message}`,
      });
    }
    broadcastToSidePanel({ type: "action_log", text: `insertText on tab ${tab_id}` });
    return;
  }

  // cdp_click: CDP-based trusted click using element coordinates from content script.
  // Use when synthetic clicks from content script are ignored (e.g. React/Slack).
  if (action === "cdp_click") {
    try {
      // Ask content script for element coordinates
      const bounds = await chrome.tabs.sendMessage(tab_id, {
        type: "execute", action: "get_bounds", element_id: data.element_id,
      });
      if (!bounds.success) throw new Error(bounds.error || "Could not get element bounds");
      await cdpClick(tab_id, bounds.x, bounds.y);
      await sleep(500);
      const state = await captureTabState(tab_id);
      sendToBackend({
        type: "action_result", request_id,
        success: true,
        page_state: state.page_state || "", url: state.url || "", title: state.title || "",
      });
    } catch (err) {
      sendToBackend({
        type: "action_result", request_id,
        success: false, error: `cdp_click error: ${err.message}`,
      });
    }
    broadcastToSidePanel({ type: "action_log", text: `cdp_click [${data.element_id}] on tab ${tab_id}` });
    return;
  }

  // cdp_type: CDP-based trusted click-to-focus + Input.insertText.
  // The most reliable way to type into React/Slack/modern web app inputs.
  if (action === "cdp_type") {
    try {
      const bounds = await chrome.tabs.sendMessage(tab_id, {
        type: "execute", action: "get_bounds", element_id: data.element_id,
      });
      if (!bounds.success) throw new Error(bounds.error || "Could not get element bounds");
      await cdpClick(tab_id, bounds.x, bounds.y);
      await sleep(200);
      // Clear existing content if requested
      if (data.clear !== false) {
        await cdpSelectAll(tab_id);
        await sleep(50);
      }
      await cdpSend(tab_id, "Input.insertText", { text: data.text || "" });
      await sleep(500);
      const state = await captureTabState(tab_id);
      sendToBackend({
        type: "action_result", request_id,
        success: true,
        page_state: state.page_state || "", url: state.url || "", title: state.title || "",
      });
    } catch (err) {
      sendToBackend({
        type: "action_result", request_id,
        success: false, error: `cdp_type error: ${err.message}`,
      });
    }
    broadcastToSidePanel({ type: "action_log", text: `cdp_type [${data.element_id}] on tab ${tab_id}` });
    return;
  }

  // ---- Content script actions (with CDP fallback) ----

  try {
    const result = await chrome.tabs.sendMessage(tab_id, {
      type: "execute",
      request_id,
      action,
      element_id: data.element_id,
      text: data.text,
      value: data.value,
      direction: data.direction,
      url: data.url,
      key: data.key,
      code: data.code,
      metaKey: data.metaKey,
      ctrlKey: data.ctrlKey,
      shiftKey: data.shiftKey,
      altKey: data.altKey,
    });

    // Content script returns needsCDP=true when element requires trusted events
    if (result.needsCDP) {
      try {
        if (result.cdpAction === "click") {
          await cdpClick(tab_id, result.x, result.y);
          await sleep(500);
        } else if (result.cdpAction === "type") {
          await cdpClick(tab_id, result.x, result.y);
          await sleep(200);
          await cdpSelectAll(tab_id);
          await sleep(50);
          await cdpSend(tab_id, "Input.insertText", { text: result.text || "" });
          await sleep(500);
        }
        const state = await captureTabState(tab_id);
        sendToBackend({
          type: "action_result", request_id,
          success: true,
          page_state: state.page_state || "", url: state.url || "", title: state.title || "",
        });
      } catch (err) {
        sendToBackend({
          type: "action_result", request_id,
          success: false, error: `CDP ${result.cdpAction} error: ${err.message}`,
        });
      }
    } else {
      sendToBackend({
        type: "action_result",
        request_id,
        ...result,
        success: result.success !== false,
      });
    }
  } catch (err) {
    sendToBackend({
      type: "action_result",
      request_id,
      success: false,
      error: `Content script error: ${err.message}`,
    });
  }

  broadcastToSidePanel({ type: "action_log", text: `${action} on tab ${tab_id}` });
}

async function captureTabState(tabId) {
  try {
    return await chrome.tabs.sendMessage(tabId, { type: "capture_state" });
  } catch {
    return { success: false, page_state: "", url: "", title: "" };
  }
}

async function injectContentScript(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"],
    });
  } catch {
    // already injected or tab not accessible
  }
}

function waitForTabLoad(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }, timeoutMs);

    function listener(updatedTabId, changeInfo) {
      if (updatedTabId === tabId && changeInfo.status === "complete") {
        clearTimeout(timeout);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ---- CDP (Chrome DevTools Protocol) helpers ----
// Dispatches trusted events (isTrusted=true) that React/Slack apps require.

async function attachDebugger(tabId) {
  if (debuggerAttached.has(tabId)) return;
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
    debuggerAttached.add(tabId);
  } catch (e) {
    if (!e.message?.includes("already attached")) throw e;
    debuggerAttached.add(tabId);
  }
}

async function detachDebugger(tabId) {
  if (!debuggerAttached.has(tabId)) return;
  try {
    await chrome.debugger.detach({ tabId });
  } catch { /* may already be detached */ }
  debuggerAttached.delete(tabId);
}

function cdpSend(tabId, method, params = {}) {
  return chrome.debugger.sendCommand({ tabId }, method, params);
}

function keyModifiers(opts) {
  let m = 0;
  if (opts.altKey) m |= 1;
  if (opts.ctrlKey) m |= 2;
  if (opts.metaKey) m |= 4;
  if (opts.shiftKey) m |= 8;
  return m;
}

const KEY_TO_CODE = {
  Enter: "Enter", Escape: "Escape", Tab: "Tab", Backspace: "Backspace",
  ArrowUp: "ArrowUp", ArrowDown: "ArrowDown", ArrowLeft: "ArrowLeft", ArrowRight: "ArrowRight",
  Delete: "Delete", Home: "Home", End: "End", PageUp: "PageUp", PageDown: "PageDown",
};

async function cdpClick(tabId, x, y) {
  await attachDebugger(tabId);
  await cdpSend(tabId, "Input.dispatchMouseEvent", {
    type: "mousePressed", x, y, button: "left", clickCount: 1,
  });
  await sleep(30);
  await cdpSend(tabId, "Input.dispatchMouseEvent", {
    type: "mouseReleased", x, y, button: "left", clickCount: 1,
  });
}

async function cdpType(tabId, x, y, text) {
  await cdpClick(tabId, x, y);
  await sleep(100);
  await cdpSend(tabId, "Input.insertText", { text });
}

async function cdpSelectAll(tabId) {
  await attachDebugger(tabId);
  const isMac = true; // SimpleClaw currently targets macOS
  const modifiers = isMac ? 4 : 2; // metaKey for Mac, ctrlKey for others
  await cdpSend(tabId, "Input.dispatchKeyEvent", {
    type: "keyDown", key: "a", code: "KeyA", modifiers,
    windowsVirtualKeyCode: 65, nativeVirtualKeyCode: 65,
  });
  await cdpSend(tabId, "Input.dispatchKeyEvent", {
    type: "keyUp", key: "a", code: "KeyA", modifiers,
    windowsVirtualKeyCode: 65, nativeVirtualKeyCode: 65,
  });
}

async function cdpKeyboard(tabId, opts) {
  await attachDebugger(tabId);
  const key = opts.key || "";
  const code = opts.code || KEY_TO_CODE[key] || (key.length === 1 ? `Key${key.toUpperCase()}` : key);
  const modifiers = keyModifiers(opts);

  await cdpSend(tabId, "Input.dispatchKeyEvent", {
    type: "keyDown",
    key, code, modifiers,
    windowsVirtualKeyCode: key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0,
    nativeVirtualKeyCode: key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0,
  });

  // Dispatch char event for printable characters without modifiers
  if (key.length === 1 && !modifiers) {
    await cdpSend(tabId, "Input.dispatchKeyEvent", {
      type: "char",
      text: key, key, code,
      unmodifiedText: key,
      windowsVirtualKeyCode: key.charCodeAt(0),
      nativeVirtualKeyCode: key.charCodeAt(0),
    });
  }

  await sleep(30);
  await cdpSend(tabId, "Input.dispatchKeyEvent", {
    type: "keyUp",
    key, code, modifiers,
    windowsVirtualKeyCode: key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0,
    nativeVirtualKeyCode: key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0,
  });
}

async function cdpClickText(tabId, text) {
  await attachDebugger(tabId);
  const js = `
    (function() {
      const target = ${JSON.stringify(text)};
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
      let bestEl = null, bestLen = Infinity;
      while (walker.nextNode()) {
        const el = walker.currentNode;
        const txt = (el.textContent || "").trim();
        if (txt.includes(target) && txt.length < bestLen) {
          const rect = el.getBoundingClientRect();
          if (rect.width > 0 && rect.height > 0 && rect.top >= 0 && rect.left >= 0) {
            const style = getComputedStyle(el);
            if (style.display !== "none" && style.visibility !== "hidden") {
              bestEl = el;
              bestLen = txt.length;
            }
          }
        }
      }
      if (!bestEl) return JSON.stringify({ error: "No element with text: " + target });
      bestEl.scrollIntoView({ block: "center", behavior: "instant" });
      const rect = bestEl.getBoundingClientRect();
      return JSON.stringify({
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
        matched: (bestEl.textContent || "").trim().substring(0, 120),
      });
    })()
  `;
  const evalResult = await cdpSend(tabId, "Runtime.evaluate", { expression: js, returnByValue: true });
  const parsed = JSON.parse(evalResult.result.value);
  if (parsed.error) throw new Error(parsed.error);
  await sleep(100);
  await cdpClick(tabId, parsed.x, parsed.y);
  return parsed;
}

chrome.debugger.onDetach.addListener((source) => {
  if (source.tabId) debuggerAttached.delete(source.tabId);
});

// ---- Tab lifecycle ----

chrome.tabs.onRemoved.addListener((tabId) => {
  if (pinnedTabs.has(tabId)) {
    pinnedTabs.delete(tabId);
    sendToBackend({ type: "tab_unpinned", tab_id: tabId });
    broadcastToSidePanel({ type: "tabs_changed", tabs: [...pinnedTabs] });
  }
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo) => {
  if (pinnedTabs.has(tabId) && changeInfo.status === "complete") {
    await injectContentScript(tabId);
    await sleep(300);
    const state = await captureTabState(tabId);
    if (state && state.success) {
      sendToBackend({
        type: "tab_updated",
        tab_id: tabId,
        url: state.url,
        title: state.title,
        page_state: state.page_state,
      });
    }
  }
});

// ---- Toolbar icon click: toggle pin ----

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false }).catch(() => {});

chrome.action.onClicked.addListener(async (tab) => {
  if (tab.id) {
    await togglePin(tab.id);
  }
});

// ---- Side panel communication ----

function broadcastToSidePanel(msg) {
  chrome.runtime.sendMessage(msg).catch(() => {});
}

async function getTabList() {
  const tabs = [];
  for (const tabId of pinnedTabs) {
    try {
      const tab = await chrome.tabs.get(tabId);
      tabs.push({ tab_id: tabId, url: tab.url, title: tab.title });
    } catch {
      pinnedTabs.delete(tabId);
    }
  }
  return tabs;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "reload_extension") {
    sendResponse({ ok: true });
    chrome.runtime.reload();
    return false;
  }

  if (message.type === "get_status") {
    getTabList().then((tabs) => {
      sendResponse({
        connected: wsConnected,
        tabs,
        config: { host: config.host, port: config.port },
      });
    });
    return true;
  }

  if (message.type === "pin_active_tab") {
    chrome.tabs.query({ active: true, currentWindow: true }, async (tabs) => {
      if (tabs[0] && tabs[0].id) {
        await pinTab(tabs[0].id);
        sendResponse({ ok: true, tab_id: tabs[0].id });
      } else {
        sendResponse({ ok: false, error: "No active tab" });
      }
    });
    return true;
  }

  if (message.type === "submit_task") {
    sendToBackend({
      type: "task",
      prompt: message.prompt,
      tab_id: message.tab_id || null,
    });
    sendResponse({ ok: true });
    return false;
  }

  if (message.type === "update_config") {
    config.token = "";
    saveConfig(message.config).then(() => {
      if (ws) ws.close();
      connectWs();
      sendResponse({ ok: true });
    });
    return true;
  }
});

// ---- Auto-discover tabs on known domains ----

const AUTO_PIN_PATTERNS = [
  /^https:\/\/app\.slack\.com\//,
  /^https:\/\/.*\.slack\.com\//,
];

async function autoDiscoverTabs() {
  try {
    const tabs = await chrome.tabs.query({});
    for (const tab of tabs) {
      if (tab.url && AUTO_PIN_PATTERNS.some((re) => re.test(tab.url))) {
        if (!pinnedTabs.has(tab.id)) {
          await pinTab(tab.id);
        }
      }
    }
  } catch {
    // Tabs API may not be available in all contexts
  }
}

// ---- Keep-alive: prevent service worker from sleeping ----

chrome.alarms.create("keepalive", { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "keepalive") {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      connectWs();
    }
  }
});

// ---- Startup ----

loadConfig().then(connectWs);
