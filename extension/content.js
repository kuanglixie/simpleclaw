/**
 * SimpleClaw content script.
 *
 * Injected into pinned tabs. Handles two concerns:
 *   1. Capture page state: interactive elements with numeric IDs + visible text.
 *   2. Execute actions: click, type, select, scroll, refresh, navigate.
 *
 * Communicates with background.js via chrome.runtime messaging.
 *
 * Design: Uses a replaceable handler on `window` so re-injection (version upgrades)
 * replaces behavior without adding duplicate listeners.
 */

(function() {
const VERSION = 8;
if (window.__simpleclaw_version >= VERSION) return;
window.__simpleclaw_version = VERSION;

const INTERACTIVE_SELECTOR = [
  "a[href]",
  "button",
  "input:not([type=hidden])",
  "textarea",
  "select",
  "[role=button]",
  "[role=link]",
  "[role=menuitem]",
  "[role=tab]",
  "[contenteditable=true]",
  "[onclick]",
  "[role=option]",
  "[role=listbox]",
  "[role=combobox]",
  "[role=treeitem]",
  "[role=textbox]",
  "[data-qa]",
].join(", ");

const MAX_TEXT_LENGTH = 30000;
const MAX_ELEMENTS = 500;

// Selectors for sidebar/nav elements that should be deprioritized.
// These are common in Slack and similar apps that have dense sidebars.
const LOW_PRIORITY_CONTAINERS = [
  "[data-qa=channel-sidebar]",
  "[data-qa=tab_rail_desktop]",
  "[role=tree]",
  "nav",
];

function isVisible(el) {
  if (!el.offsetParent && el.tagName !== "BODY" && el.tagName !== "HTML") return false;
  const style = getComputedStyle(el);
  if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
  const rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

function isInLowPriorityContainer(el) {
  for (const sel of LOW_PRIORITY_CONTAINERS) {
    if (el.closest(sel)) return true;
  }
  return false;
}

function isInViewport(el) {
  const rect = el.getBoundingClientRect();
  const vw = window.innerWidth || document.documentElement.clientWidth;
  const vh = window.innerHeight || document.documentElement.clientHeight;
  return rect.top < vh && rect.bottom > 0 && rect.left < vw && rect.right > 0;
}

function elementPriority(el) {
  const tag = el.tagName.toLowerCase();
  const role = el.getAttribute("role") || "";
  const qa = el.getAttribute("data-qa") || "";

  // Highest priority: message inputs, text editors, send buttons
  if (role === "textbox" || qa.includes("message_input") || qa.includes("texty_input")) return 0;
  if (qa.includes("send_button") || qa.includes("texty_send")) return 0;
  if (tag === "textarea" || (tag === "input" && !["hidden", "range"].includes(el.type))) return 1;
  if (el.isContentEditable) return 1;
  if (role === "combobox" || role === "listbox") return 1;

  // Medium priority: buttons, links, interactive in main content
  if (isInLowPriorityContainer(el)) return 8;

  // Default priority for everything else in the main content area
  return 4;
}

function getLabel(el) {
  const tag = el.tagName.toLowerCase();
  const type = el.getAttribute("type") || "";
  const role = el.getAttribute("role") || "";
  const ariaLabel = el.getAttribute("aria-label") || "";
  const placeholder = el.getAttribute("placeholder") || "";
  const name = el.getAttribute("name") || "";
  const id = el.id || "";
  const dataQa = el.getAttribute("data-qa") || "";
  const text = (el.textContent || "").trim().slice(0, 80);
  const value = el.value || "";
  const href = el.href || "";

  let label = tag;
  if (id) label += `#${id}`;
  if (role && role !== tag) label += `[role=${role}]`;

  const details = [];
  if (type) details.push(`type: ${type}`);
  if (ariaLabel) details.push(`aria: "${ariaLabel}"`);
  if (dataQa) details.push(`qa: "${dataQa}"`);
  if (placeholder) details.push(`placeholder: "${placeholder}"`);
  if (name) details.push(`name: "${name}"`);
  if (value && tag !== "a") details.push(`value: "${value.slice(0, 60)}"`);
  if (text && tag !== "input" && tag !== "textarea") details.push(`"${text}"`);
  if (href && tag === "a") {
    try { details.push(`-> ${new URL(href).pathname}`); }
    catch { details.push(`-> ${href.slice(0, 80)}`); }
  }

  return details.length > 0 ? `${label} (${details.join(", ")})` : label;
}

function getElementBounds(el) {
  const rect = el.getBoundingClientRect();
  return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
}

function capturePageState() {
  const allInteractive = document.querySelectorAll(INTERACTIVE_SELECTOR);
  const candidates = [];
  for (const el of allInteractive) {
    if (!isVisible(el)) continue;
    candidates.push({ el, label: getLabel(el), priority: elementPriority(el) });
  }

  // Sort by priority (lower = more important), then by DOM order within same priority
  candidates.sort((a, b) => a.priority - b.priority);

  const elements = [];
  let idx = 1;
  for (const c of candidates) {
    if (idx > MAX_ELEMENTS) break;
    elements.push({ idx: idx++, el: c.el, label: c.label });
  }

  const lines = [`URL: ${location.href}`, `Title: ${document.title}`, "---"];
  for (const { idx, label } of elements) lines.push(`[${idx}] ${label}`);
  lines.push("---");
  const fullText = (document.body.innerText || "");
  if (fullText.length > MAX_TEXT_LENGTH) {
    lines.push(`Visible text (last ${MAX_TEXT_LENGTH} chars):\n${fullText.slice(-MAX_TEXT_LENGTH)}`);
  } else {
    lines.push(`Visible text:\n${fullText}`);
  }

  return { text: lines.join("\n"), url: location.href, title: document.title, elementCount: elements.length };
}

function getVisibleElements() {
  const allInteractive = document.querySelectorAll(INTERACTIVE_SELECTOR);
  const candidates = [];
  for (const el of allInteractive) {
    if (!isVisible(el)) continue;
    candidates.push({ el, priority: elementPriority(el) });
  }
  candidates.sort((a, b) => a.priority - b.priority);
  const elements = [];
  let idx = 1;
  for (const c of candidates) {
    if (idx > MAX_ELEMENTS) break;
    elements.push({ idx: idx++, el: c.el });
  }
  return elements;
}

function findElement(elementId) {
  const match = getVisibleElements().find((e) => e.idx === elementId);
  if (!match) throw new Error(`Element [${elementId}] not found or not visible`);
  return match.el;
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function dispatchClick(el) {
  const rect = el.getBoundingClientRect();
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  const opts = { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y };
  el.dispatchEvent(new PointerEvent("pointerdown", { ...opts, pointerId: 1 }));
  el.dispatchEvent(new MouseEvent("mousedown", opts));
  el.dispatchEvent(new PointerEvent("pointerup", { ...opts, pointerId: 1 }));
  el.dispatchEvent(new MouseEvent("mouseup", opts));
  el.dispatchEvent(new MouseEvent("click", opts));
  el.click();
}

// Determine whether an element requires CDP (trusted events) for text input.
// React/Slack ignore synthetic events; we need CDP Input.insertText for those.
function needsCDPForType(el) {
  const role = el.getAttribute("role") || "";
  const qa = el.getAttribute("data-qa") || "";
  if (role === "textbox" || role === "combobox") return true;
  if (qa.includes("texty_input") || qa.includes("message_input")) return true;
  if (qa.includes("composer_page__destination")) return true;
  // Heuristic: contenteditable divs in modern frameworks typically need CDP
  if (el.isContentEditable && el.tagName !== "TEXTAREA" && el.tagName !== "INPUT") return true;
  return false;
}

// ---- Slack-specific extractors ----

function extractSlackMessages() {
  const containers = document.querySelectorAll("[data-qa=message_container]");
  const messages = [];
  for (const container of containers) {
    const senderEl = container.querySelector("[data-qa=message_sender_name]");
    const textEl = container.querySelector("[data-qa=message-text]") ||
                   container.querySelector("[data-qa=block-kit-renderer]");
    const timestampEl = container.querySelector("[data-qa=timestamp_label]");
    const replyBar = container.querySelector("[data-qa=reply_bar_count]");
    const attachmentEl = container.querySelector("[data-qa=message_attachment_v2]");
    const isApp = !!container.querySelector("[data-qa=message_sender_app_badge]");
    const listItem = container.closest("[role=listitem]");

    const sender = senderEl?.textContent?.trim() || "unknown";
    const text = textEl?.textContent?.trim() || attachmentEl?.textContent?.trim() || "";
    const time = timestampEl?.textContent?.trim() || "";
    const replies = replyBar?.textContent?.trim() || "";
    const msgId = listItem?.id || "";

    if (!text && !sender) continue;

    const isMention = text.includes("@jing") || text.includes("@Jing");

    messages.push({ sender, text: text.slice(0, 500), time, replies, isApp, isMention, msgId });
  }
  return messages;
}

function extractSlackUnreads() {
  const result = { dms: [], channels: [], mentions: 0, totalUnread: 0 };

  // Parse sidebar for unread indicators
  const sidebarItems = document.querySelectorAll("[data-qa=channel-sidebar-channel]");
  for (const item of sidebarItems) {
    const nameEl = item.querySelector("[data-qa^=channel_sidebar_name_]");
    const name = nameEl?.textContent?.trim() || "";
    const treeItem = item.closest("[role=treeitem]");
    const channelId = treeItem?.id || "";
    const hasUnread = item.closest("[data-qa=virtual-list-item]")?.querySelector(".p-channel_sidebar__channel--unread") !== null
                    || item.parentElement?.classList?.contains("p-channel_sidebar__channel--unread")
                    || item.querySelector("[data-qa=channel_sidebar_name_you]") !== null;

    // Check if it has bold text (unread indicator in Slack)
    const isBold = nameEl ? getComputedStyle(nameEl).fontWeight >= 700 : false;

    if (!name || !channelId) continue;

    // DMs have IDs starting with 'D', channels with 'C'
    const isDM = channelId.startsWith("D");
    const section = item.closest("[data-qa^=channel_sidebar__section]")?.closest("[role=treeitem]");
    const sectionLabel = section?.getAttribute("aria-label") || "";

    if (isDM || sectionLabel.includes("Direct Message")) {
      if (isBold || hasUnread) {
        result.dms.push({ name, channelId, unread: true });
        result.totalUnread++;
      }
    } else if (channelId.startsWith("C")) {
      if (isBold || hasUnread) {
        result.channels.push({ name, channelId, unread: true });
        result.totalUnread++;
      }
    }
  }

  // Check Activity badge for mention count
  const activityTab = document.querySelector("[data-qa=tab_rail_activity_button]");
  if (activityTab) {
    const badgeText = activityTab.textContent?.replace("Activity", "").trim();
    if (badgeText && !isNaN(parseInt(badgeText))) {
      result.mentions = parseInt(badgeText);
    }
  }

  return result;
}

// The main action handler -- replaced on each version upgrade.
window.__simpleclaw_execute = async function(action) {
  const type = action.action;

  if (type === "read_page") {
    const state = capturePageState();
    return { success: true, ...state, page_state: state.text };
  }

  if (type === "click") {
    const el = findElement(action.element_id);
    el.scrollIntoView({ block: "center", behavior: "instant" });
    await sleep(100);
    el.focus();
    dispatchClick(el);
    await sleep(500);
    const state = capturePageState();
    return { success: true, ...state, page_state: state.text };
  }

  if (type === "type") {
    const el = findElement(action.element_id);
    el.scrollIntoView({ block: "center", behavior: "instant" });

    // For React/Slack components, delegate to CDP via background.js
    if (needsCDPForType(el)) {
      const bounds = getElementBounds(el);
      return {
        needsCDP: true,
        cdpAction: "type",
        x: bounds.x,
        y: bounds.y,
        text: action.text,
      };
    }

    el.focus();
    if (el.isContentEditable) {
      el.textContent = "";
      document.execCommand("insertText", false, action.text);
    } else {
      try {
        const setter =
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set ||
          Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
        if (setter) setter.call(el, action.text);
        else el.value = action.text;
      } catch { el.value = action.text; }
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    await sleep(200);
    const state = capturePageState();
    return { success: true, ...state, page_state: state.text };
  }

  if (type === "keyboard") {
    const key = action.key || "";
    const opts = {
      bubbles: true, cancelable: true, key,
      code: action.code || (key.length === 1 ? `Key${key.toUpperCase()}` : key),
      metaKey: !!action.metaKey, ctrlKey: !!action.ctrlKey,
      shiftKey: !!action.shiftKey, altKey: !!action.altKey,
    };
    const target = document.activeElement || document.body;
    target.dispatchEvent(new KeyboardEvent("keydown", opts));
    target.dispatchEvent(new KeyboardEvent("keyup", opts));
    if (key.length === 1 && !action.metaKey && !action.ctrlKey) {
      target.dispatchEvent(new InputEvent("input", { bubbles: true, data: key, inputType: "insertText" }));
    }
    await sleep(300);
    const state = capturePageState();
    return { success: true, ...state, page_state: state.text };
  }

  // get_bounds: return element center coordinates for CDP actions
  if (type === "get_bounds") {
    const el = findElement(action.element_id);
    el.scrollIntoView({ block: "center", behavior: "instant" });
    await sleep(100);
    const bounds = getElementBounds(el);
    return { success: true, ...bounds };
  }

  if (type === "click_text") {
    const target = action.text || "";
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    let bestEl = null, bestLen = Infinity;
    while (walker.nextNode()) {
      const el = walker.currentNode;
      const txt = (el.textContent || "").trim();
      if (txt.includes(target) && txt.length < bestLen) {
        const rect = el.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0 && rect.top >= 0) {
          const style = getComputedStyle(el);
          if (style.display !== "none" && style.visibility !== "hidden") {
            bestEl = el; bestLen = txt.length;
          }
        }
      }
    }
    if (!bestEl) throw new Error(`No visible element containing text "${target}" found`);
    bestEl.scrollIntoView({ block: "center", behavior: "instant" });
    await sleep(100);
    bestEl.focus();
    dispatchClick(bestEl);
    await sleep(500);
    const state = capturePageState();
    return { success: true, ...state, page_state: state.text, matchedText: (bestEl.textContent || "").trim().slice(0, 120) };
  }

  if (type === "select") {
    const el = findElement(action.element_id);
    const opt = Array.from(el.options || []).find(
      (o) => o.value === action.value || o.textContent.trim() === action.value
    );
    if (!opt) throw new Error(`Option "${action.value}" not found`);
    el.value = opt.value;
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { success: true };
  }

  // ---- Slack-specific actions ----

  if (type === "slack_messages") {
    const messages = extractSlackMessages();
    const json = JSON.stringify(messages);
    return { success: true, messages: json, page_state: json };
  }

  if (type === "slack_unreads") {
    const unreads = extractSlackUnreads();
    const json = JSON.stringify(unreads);
    return { success: true, unreads: json, page_state: json };
  }

  if (type === "scroll") {
    const amount = action.direction === "up" ? -600 : 600;
    window.scrollBy({ top: amount, behavior: "smooth" });
    await sleep(400);
    const state = capturePageState();
    return { success: true, ...state, page_state: state.text };
  }

  if (type === "refresh") {
    location.reload();
    return { success: true, page_state: "(page reloading...)" };
  }

  if (type === "navigate") {
    location.href = action.url;
    return { success: true, page_state: "(navigating...)" };
  }

  throw new Error(`Unknown action type: ${type}`);
};

// Register listener only once; subsequent versions just replace __simpleclaw_execute.
if (!window.__simpleclaw_listener_registered) {
  window.__simpleclaw_listener_registered = true;
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "capture_state") {
      try {
        const state = capturePageState();
        sendResponse({ success: true, ...state, page_state: state.text });
      } catch (err) {
        sendResponse({ success: false, error: err.message });
      }
      return true;
    }
    if (message.type === "execute") {
      window.__simpleclaw_execute(message)
        .then((result) => sendResponse({ request_id: message.request_id, ...result }))
        .catch((err) => sendResponse({ request_id: message.request_id, success: false, error: err.message }));
      return true;
    }
  });
}
})();
