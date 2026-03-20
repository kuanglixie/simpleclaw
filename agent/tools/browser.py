"""Browser tool implementations.

Each function sends a command to the Chrome extension via the BrowserServer
WebSocket and awaits the result. These are async (unlike other tools) because
they wait on the extension to execute the action and report back.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..browser_server import BrowserServer

_server: BrowserServer | None = None


def set_browser_server(server: BrowserServer) -> None:
    global _server
    _server = server


def _require_server() -> BrowserServer:
    if _server is None:
        raise RuntimeError("Browser server not initialized")
    if not _server.connected:
        raise RuntimeError(
            "No browser extension connected. "
            "Open Chrome and ensure the SimpleClaw extension is running."
        )
    return _server


async def browser_list_tabs() -> str:
    srv = _require_server()
    tabs = srv.list_tabs()
    if not tabs:
        return "No pinned tabs. Pin a tab in the Chrome extension first."
    lines = [f"Pinned tabs ({len(tabs)}):"]
    for t in tabs:
        lines.append(f"  tab_id={t.tab_id}  {t.title}  ({t.url})")
    return "\n".join(lines)


async def browser_read_page(tab_id: int) -> str:
    srv = _require_server()
    result = await srv.send_action(tab_id, "read_page")
    if not result.get("success"):
        return f"Error: {result.get('error', 'Failed to read page')}"
    return result.get("page_state", "(empty page state)")


async def browser_click(tab_id: int, element_id: int) -> str:
    srv = _require_server()
    result = await srv.send_action(tab_id, "click", element_id=element_id)
    if not result.get("success"):
        return f"Error: {result.get('error', 'Click failed')}"
    page_state = result.get("page_state", "")
    return f"Clicked element [{element_id}]. Page state after click:\n{page_state}"


async def browser_type(tab_id: int, element_id: int, text: str) -> str:
    srv = _require_server()
    result = await srv.send_action(
        tab_id, "type", element_id=element_id, text=text,
    )
    if not result.get("success"):
        return f"Error: {result.get('error', 'Type failed')}"
    return f"Typed \"{text[:80]}\" into element [{element_id}]."


async def browser_select(tab_id: int, element_id: int, value: str) -> str:
    srv = _require_server()
    result = await srv.send_action(
        tab_id, "select", element_id=element_id, value=value,
    )
    if not result.get("success"):
        return f"Error: {result.get('error', 'Select failed')}"
    return f"Selected \"{value}\" in element [{element_id}]."


async def browser_scroll(tab_id: int, direction: str) -> str:
    srv = _require_server()
    result = await srv.send_action(tab_id, "scroll", direction=direction)
    if not result.get("success"):
        return f"Error: {result.get('error', 'Scroll failed')}"
    page_state = result.get("page_state", "")
    return f"Scrolled {direction}. Page state:\n{page_state}"


async def browser_refresh(tab_id: int) -> str:
    srv = _require_server()
    result = await srv.send_action(tab_id, "refresh")
    if not result.get("success"):
        return f"Error: {result.get('error', 'Refresh failed')}"
    page_state = result.get("page_state", "")
    return f"Page refreshed. Page state:\n{page_state}"


async def browser_navigate(tab_id: int, url: str) -> str:
    srv = _require_server()
    result = await srv.send_action(tab_id, "navigate", url=url)
    if not result.get("success"):
        return f"Error: {result.get('error', 'Navigate failed')}"
    page_state = result.get("page_state", "")
    return f"Navigated to {url}. Page state:\n{page_state}"


async def browser_eval(tab_id: int, code: str) -> str:
    srv = _require_server()
    result = await srv.send_action(tab_id, "eval", code=code)
    if not result.get("success"):
        return f"Error: {result.get('error', 'eval failed')}"
    eval_result = result.get("evalResult", "")
    page_state = result.get("page_state", "")
    return f"Eval result: {eval_result}\n\nPage state:\n{page_state}"


async def browser_click_text(tab_id: int, text: str) -> str:
    srv = _require_server()
    result = await srv.send_action(tab_id, "click_text", text=text)
    if not result.get("success"):
        return f"Error: {result.get('error', 'click_text failed')}"
    page_state = result.get("page_state", "")
    matched = result.get("matchedText", "")
    return f"Clicked element matching '{text}' (matched: '{matched}'). Page state:\n{page_state}"


async def browser_keyboard(tab_id: int, key: str, **modifiers: bool) -> str:
    srv = _require_server()
    result = await srv.send_action(
        tab_id, "keyboard", key=key, **modifiers,
    )
    if not result.get("success"):
        return f"Error: {result.get('error', 'Keyboard action failed')}"
    page_state = result.get("page_state", "")
    return f"Pressed {key}. Page state:\n{page_state}"


async def browser_insert_text(tab_id: int, text: str) -> str:
    """Insert text at the current cursor via CDP Input.insertText.

    Use this after focusing an element (e.g. via browser_click or
    browser_cdp_click). Works reliably on React/Slack/modern web
    apps where browser_type may fail due to synthetic event rejection.
    """
    srv = _require_server()
    result = await srv.send_action(tab_id, "insertText", text=text)
    if not result.get("success"):
        return f"Error: {result.get('error', 'insertText failed')}"
    page_state = result.get("page_state", "")
    return f"Inserted \"{text[:80]}\" via CDP. Page state:\n{page_state}"


async def browser_cdp_click(tab_id: int, element_id: int) -> str:
    """Click an element using CDP trusted mouse events.

    More reliable than browser_click on React/Slack where synthetic
    events (isTrusted=false) are ignored. The element is found by
    the content script, then clicked via CDP Input.dispatchMouseEvent.
    """
    srv = _require_server()
    result = await srv.send_action(tab_id, "cdp_click", element_id=element_id)
    if not result.get("success"):
        return f"Error: {result.get('error', 'cdp_click failed')}"
    page_state = result.get("page_state", "")
    return f"CDP-clicked element [{element_id}]. Page state:\n{page_state}"


async def browser_cdp_type(tab_id: int, element_id: int, text: str) -> str:
    """Click an element to focus it, then type text via CDP Input.insertText.

    Combines a trusted CDP click (to focus the element) with
    Input.insertText for reliable text entry. This is the most
    reliable way to type into Slack message inputs, React comboboxes,
    and other modern framework components. Set clear=false in args
    to append instead of replacing existing content.
    """
    srv = _require_server()
    result = await srv.send_action(
        tab_id, "cdp_type", element_id=element_id, text=text,
    )
    if not result.get("success"):
        return f"Error: {result.get('error', 'cdp_type failed')}"
    page_state = result.get("page_state", "")
    return f"CDP-typed \"{text[:80]}\" into element [{element_id}]. Page state:\n{page_state}"


async def browser_slack_messages(tab_id: int) -> str:
    """Extract structured messages from the current Slack view.

    Returns a formatted list of messages with sender, text, time,
    replies count, isMention flag, and isApp flag. Use this instead
    of browser_read_page when you need to process Slack messages
    programmatically (e.g. for inbox triage).
    """
    import json as _json
    srv = _require_server()
    result = await srv.send_action(tab_id, "slack_messages")
    if not result.get("success"):
        return f"Error: {result.get('error', 'slack_messages failed')}"
    raw = result.get("messages") or result.get("page_state") or "[]"
    messages = _json.loads(raw)
    if not messages:
        return "No messages found in the current view."
    lines = [f"Found {len(messages)} messages:\n"]
    for i, m in enumerate(messages):
        mention_tag = " [MENTIONS YOU]" if m.get("isMention") else ""
        app_tag = " [APP]" if m.get("isApp") else ""
        reply_tag = f" ({m['replies']})" if m.get("replies") else ""
        lines.append(
            f"  {i+1}. {m['sender']}{app_tag} ({m.get('time','')}):{mention_tag}{reply_tag}\n"
            f"     {m['text'][:300]}"
        )
    return "\n".join(lines)


async def browser_slack_unreads(tab_id: int) -> str:
    """Extract unread DMs and channels from the Slack sidebar.

    Returns structured data about which DMs and channels have unread
    messages, plus the Activity badge mention count. Use this to
    decide which conversations to check first.
    """
    import json as _json
    srv = _require_server()
    result = await srv.send_action(tab_id, "slack_unreads")
    if not result.get("success"):
        return f"Error: {result.get('error', 'slack_unreads failed')}"
    raw = result.get("unreads") or result.get("page_state") or "{}"
    unreads = _json.loads(raw)
    lines = ["Slack unreads summary:"]
    lines.append(f"  Activity mentions: {unreads.get('mentions', 0)}")
    lines.append(f"  Total unread: {unreads.get('totalUnread', 0)}")
    dms = unreads.get("dms", [])
    if dms:
        lines.append(f"\n  Unread DMs ({len(dms)}):")
        for dm in dms:
            lines.append(f"    - {dm['name']} (id: {dm['channelId']})")
    channels = unreads.get("channels", [])
    if channels:
        lines.append(f"\n  Unread channels ({len(channels)}):")
        for ch in channels:
            lines.append(f"    - #{ch['name']} (id: {ch['channelId']})")
    if not dms and not channels and unreads.get("mentions", 0) == 0:
        lines.append("\n  All caught up! No unread messages.")
    return "\n".join(lines)


async def browser_wait(seconds: float) -> str:
    seconds = min(seconds, 10)
    await asyncio.sleep(seconds)
    return f"Waited {seconds}s."


BROWSER_TOOL_NAMES = frozenset({
    "browser_list_tabs", "browser_read_page", "browser_click",
    "browser_type", "browser_select", "browser_scroll",
    "browser_refresh", "browser_navigate", "browser_eval",
    "browser_click_text", "browser_keyboard", "browser_wait",
    "browser_insert_text", "browser_cdp_click", "browser_cdp_type",
    "browser_slack_messages", "browser_slack_unreads",
})

_DISPATCH = {
    "browser_list_tabs": lambda _args: browser_list_tabs(),
    "browser_read_page": lambda args: browser_read_page(
        tab_id=int(args["tab_id"]),
    ),
    "browser_click": lambda args: browser_click(
        tab_id=int(args["tab_id"]), element_id=int(args["element_id"]),
    ),
    "browser_type": lambda args: browser_type(
        tab_id=int(args["tab_id"]), element_id=int(args["element_id"]),
        text=args.get("text", ""),
    ),
    "browser_select": lambda args: browser_select(
        tab_id=int(args["tab_id"]), element_id=int(args["element_id"]),
        value=args.get("value", ""),
    ),
    "browser_scroll": lambda args: browser_scroll(
        tab_id=int(args["tab_id"]), direction=args.get("direction", "down"),
    ),
    "browser_refresh": lambda args: browser_refresh(
        tab_id=int(args["tab_id"]),
    ),
    "browser_navigate": lambda args: browser_navigate(
        tab_id=int(args["tab_id"]), url=args.get("url", ""),
    ),
    "browser_eval": lambda args: browser_eval(
        tab_id=int(args["tab_id"]), code=args.get("code", ""),
    ),
    "browser_click_text": lambda args: browser_click_text(
        tab_id=int(args["tab_id"]), text=args.get("text", ""),
    ),
    "browser_keyboard": lambda args: browser_keyboard(
        tab_id=int(args["tab_id"]), key=args.get("key", ""),
        metaKey=bool(args.get("metaKey", False)),
        ctrlKey=bool(args.get("ctrlKey", False)),
        shiftKey=bool(args.get("shiftKey", False)),
        altKey=bool(args.get("altKey", False)),
    ),
    "browser_wait": lambda args: browser_wait(
        seconds=float(args.get("seconds", 2)),
    ),
    "browser_insert_text": lambda args: browser_insert_text(
        tab_id=int(args["tab_id"]), text=args.get("text", ""),
    ),
    "browser_cdp_click": lambda args: browser_cdp_click(
        tab_id=int(args["tab_id"]), element_id=int(args["element_id"]),
    ),
    "browser_cdp_type": lambda args: browser_cdp_type(
        tab_id=int(args["tab_id"]), element_id=int(args["element_id"]),
        text=args.get("text", ""),
    ),
    "browser_slack_messages": lambda args: browser_slack_messages(
        tab_id=int(args["tab_id"]),
    ),
    "browser_slack_unreads": lambda args: browser_slack_unreads(
        tab_id=int(args["tab_id"]),
    ),
}


async def execute(name: str, args: dict) -> str:
    handler = _DISPATCH.get(name)
    if handler is None:
        return f"Error: unknown browser tool '{name}'"
    return await handler(args)
