/**
 * SimpleClaw side panel UI.
 *
 * Shows connection status, pinned tabs, action log, and a chat input
 * for sending instructions to simplebot.
 */

const logEl = document.getElementById("log");
const statusDot = document.getElementById("statusDot");
const tabsBar = document.getElementById("tabsBar");
const tabSelect = document.getElementById("tabSelect");
const promptInput = document.getElementById("promptInput");
const inputForm = document.getElementById("inputForm");
const settingsToggle = document.getElementById("settingsToggle");
const settingsPanel = document.getElementById("settingsPanel");
const hostInput = document.getElementById("hostInput");
const portInput = document.getElementById("portInput");
const saveSettingsBtn = document.getElementById("saveSettings");

// ---- Logging ----

function addLog(text, cls = "action") {
  const el = document.createElement("div");
  el.className = `log-entry ${cls}`;
  el.textContent = text;
  logEl.appendChild(el);
  logEl.scrollTop = logEl.scrollHeight;
}

// ---- Status & tabs ----

function setConnected(connected) {
  statusDot.className = `status-dot ${connected ? "connected" : "disconnected"}`;
  statusDot.title = connected ? "Connected" : "Disconnected";
}

function renderTabs(tabs) {
  tabsBar.innerHTML = "";
  tabSelect.innerHTML = '<option value="">No tab</option>';

  if (!tabs || tabs.length === 0) {
    tabsBar.innerHTML = '<span style="font-size:11px;color:var(--text-muted)">No pinned tabs — click the toolbar icon on a tab to pin it</span>';
    return;
  }

  for (const tab of tabs) {
    // Chip in header
    const chip = document.createElement("span");
    chip.className = "tab-chip";
    chip.innerHTML = `<span class="dot"></span>${escapeHtml(tab.title || tab.url || `Tab ${tab.tab_id}`)}`;
    chip.title = tab.url || "";
    tabsBar.appendChild(chip);

    // Option in select
    const opt = document.createElement("option");
    opt.value = tab.tab_id;
    const label = (tab.title || tab.url || `Tab ${tab.tab_id}`).slice(0, 30);
    opt.textContent = label;
    tabSelect.appendChild(opt);
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ---- Messages from background ----

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "ws_status") {
    setConnected(message.connected);
    if (message.error) {
      addLog(`Connection error: ${message.error}`, "error");
    }
  }

  if (message.type === "tabs_changed") {
    renderTabs(message.tabs);
  }

  if (message.type === "action_log") {
    addLog(message.text);
  }
});

// ---- Submit task ----

inputForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const prompt = promptInput.value.trim();
  if (!prompt) return;

  const tabId = tabSelect.value ? parseInt(tabSelect.value, 10) : null;
  addLog(prompt, "user");

  chrome.runtime.sendMessage({
    type: "submit_task",
    prompt,
    tab_id: tabId,
  });

  promptInput.value = "";
});

// ---- Settings ----

settingsToggle.addEventListener("click", () => {
  settingsPanel.classList.toggle("hidden");
});

saveSettingsBtn.addEventListener("click", () => {
  const newConfig = {};
  if (hostInput.value.trim()) newConfig.host = hostInput.value.trim();
  if (portInput.value) newConfig.port = parseInt(portInput.value, 10);

  chrome.runtime.sendMessage({ type: "update_config", config: newConfig }, () => {
    addLog("Settings saved, reconnecting...", "system");
    settingsPanel.classList.add("hidden");
  });
});

// ---- Init ----

chrome.runtime.sendMessage({ type: "get_status" }, (response) => {
  if (chrome.runtime.lastError || !response) return;
  setConnected(response.connected);
  renderTabs(response.tabs);

  if (response.config) {
    hostInput.value = response.config.host || "";
    portInput.value = response.config.port || "";
  }
});

document.getElementById("pinActiveBtn").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "pin_active_tab" }, (resp) => {
    if (resp && resp.ok) {
      addLog(`Pinned tab ${resp.tab_id}`, "system");
    } else {
      addLog("Could not pin active tab", "error");
    }
  });
});

document.getElementById("reloadExtBtn")?.addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "reload_extension" });
});

addLog("Side panel opened. Pin tabs with the toolbar icon or the button above, then type instructions below.", "system");
