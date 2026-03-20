"""WebSocket server for Chrome extension browser relay.

Binds to 127.0.0.1 only. The extension connects, pins tabs, and
executes browser actions on behalf of the Gemini agent loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4

from aiohttp import web, WSMsgType

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")
logger = logging.getLogger("browser_server")

ACTION_TIMEOUT_SECONDS = 30


@dataclass
class PinnedTab:
    tab_id: int
    url: str
    title: str
    page_state: str


@dataclass
class _PendingAction:
    future: asyncio.Future
    tab_id: int
    action: str


class BrowserServer:
    """Lightweight WS server that bridges Gemini tool calls to a Chrome extension."""

    def __init__(self, host: str, port: int, token: str = "", queue: Any = None) -> None:
        self.host = host
        self.port = port
        self.token = token
        self._require_auth = bool(token)
        self.queue = queue
        if self._require_auth:
            logger.info("Browser server token auth enabled")
        else:
            logger.info("Browser server running without token auth (loopback only)")
        self._ws: Optional[web.WebSocketResponse] = None
        self._authed = False
        self._pinned_tabs: dict[int, PinnedTab] = {}
        self._pending: dict[str, _PendingAction] = {}
        self._runner: Optional[web.AppRunner] = None
        self._log_subscribers: list[asyncio.Queue] = []

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed and self._authed

    def list_tabs(self) -> list[PinnedTab]:
        return list(self._pinned_tabs.values())

    def get_tab(self, tab_id: int) -> Optional[PinnedTab]:
        return self._pinned_tabs.get(tab_id)

    def subscribe_logs(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._log_subscribers.append(q)
        return q

    def unsubscribe_logs(self, q: asyncio.Queue) -> None:
        self._log_subscribers = [s for s in self._log_subscribers if s is not q]

    def _broadcast_log(self, text: str) -> None:
        for q in self._log_subscribers:
            try:
                q.put_nowait(text)
            except asyncio.QueueFull:
                pass

    # ---- public API used by tool executor ----

    async def send_action(
        self, tab_id: int, action: str, **params: Any
    ) -> dict:
        if not self.connected:
            raise RuntimeError("No browser extension connected")
        if tab_id not in self._pinned_tabs and action not in ("wait",):
            raise ValueError(f"Tab {tab_id} is not pinned")

        request_id = str(uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = _PendingAction(
            future=future, tab_id=tab_id, action=action,
        )

        msg = {"type": "execute", "request_id": request_id,
               "tab_id": tab_id, "action": action, **params}
        await self._ws.send_json(msg)  # type: ignore[union-attr]

        desc = f"{action}"
        if "element_id" in params:
            desc += f" element [{params['element_id']}]"
        if "text" in params:
            desc += f" \"{params['text'][:60]}\""
        self._broadcast_log(f"→ {desc} on tab {tab_id}")

        try:
            result = await asyncio.wait_for(future, timeout=ACTION_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise TimeoutError(f"Browser action '{action}' timed out after {ACTION_TIMEOUT_SECONDS}s")
        finally:
            self._pending.pop(request_id, None)

        if result.get("success"):
            self._broadcast_log(f"✓ {action} succeeded")
        else:
            self._broadcast_log(f"✗ {action} failed: {result.get('error', 'unknown')}")

        if "page_state" in result and tab_id in self._pinned_tabs:
            self._pinned_tabs[tab_id].page_state = result["page_state"]
            if "url" in result:
                self._pinned_tabs[tab_id].url = result["url"]
            if "title" in result:
                self._pinned_tabs[tab_id].title = result["title"]

        return result

    # ---- server lifecycle ----

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_post("/api/task", self._handle_task_submit)
        app.router.add_get("/api/tabs", self._handle_list_tabs)
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/api/reload", self._handle_reload)
        app.router.add_post("/api/execute", self._handle_execute)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info("Browser WS server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        for pa in self._pending.values():
            pa.future.cancel()
        self._pending.clear()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._runner:
            await self._runner.cleanup()

    # ---- WebSocket handler ----

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)

        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = ws
        self._authed = False
        self._pinned_tabs.clear()
        logger.info("Extension connected from %s", request.remote)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_message(ws, json.loads(msg.data))
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            if self._ws is ws:
                self._authed = False
                self._ws = None
                for pa in self._pending.values():
                    pa.future.cancel()
                self._pending.clear()
                self._pinned_tabs.clear()
                logger.info("Extension disconnected")

        return ws

    async def _on_message(
        self, ws: web.WebSocketResponse, data: dict,
    ) -> None:
        msg_type = data.get("type", "")

        if msg_type == "auth":
            if not self._require_auth or data.get("token") == self.token:
                self._authed = True
                await ws.send_json({"type": "auth_ok"})
                logger.info("Extension authenticated")
            else:
                await ws.send_json({"type": "auth_fail"})
                logger.warning("Extension auth failed")
            return

        if not self._authed:
            await ws.send_json({"type": "error", "message": "Not authenticated"})
            return

        if msg_type == "tab_pinned":
            tab = PinnedTab(
                tab_id=data["tab_id"], url=data.get("url", ""),
                title=data.get("title", ""), page_state=data.get("page_state", ""),
            )
            self._pinned_tabs[tab.tab_id] = tab
            self._broadcast_log(f"📌 Tab pinned: {tab.title} ({tab.url})")
            logger.info("Tab pinned: %d %s", tab.tab_id, tab.url)

        elif msg_type == "tab_unpinned":
            tab_id = data["tab_id"]
            removed = self._pinned_tabs.pop(tab_id, None)
            if removed:
                self._broadcast_log(f"📌 Tab unpinned: {removed.title}")
            logger.info("Tab unpinned: %d", tab_id)

        elif msg_type == "tab_updated":
            tab_id = data["tab_id"]
            if tab_id in self._pinned_tabs:
                tab = self._pinned_tabs[tab_id]
                tab.url = data.get("url", tab.url)
                tab.title = data.get("title", tab.title)
                tab.page_state = data.get("page_state", tab.page_state)

        elif msg_type == "action_result":
            request_id = data.get("request_id", "")
            pending = self._pending.get(request_id)
            if pending and not pending.future.done():
                pending.future.set_result(data)

        elif msg_type == "task":
            await self._enqueue_from_extension(data)

    # ---- HTTP handlers ----

    async def _handle_task_submit(self, request: web.Request) -> web.Response:
        data = await request.json()
        if self._require_auth:
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if token != self.token:
                return web.json_response({"error": "unauthorized"}, status=401)
        task_id = await self._enqueue_from_extension(data)
        return web.json_response({"task_id": task_id})

    async def _handle_list_tabs(self, request: web.Request) -> web.Response:
        if self._require_auth:
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if token != self.token:
                return web.json_response({"error": "unauthorized"}, status=401)
        tabs = [
            {"tab_id": t.tab_id, "url": t.url, "title": t.title}
            for t in self._pinned_tabs.values()
        ]
        return web.json_response({"tabs": tabs, "connected": self.connected})

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "token": self.token,
            "connected": self.connected,
            "pinned_tabs": len(self._pinned_tabs),
        })

    async def _handle_execute(self, request: web.Request) -> web.Response:
        """Run an action on a pinned tab and return the result."""
        data = await request.json()
        tab_id = data.get("tab_id")
        action = data.get("action")
        if not tab_id or not action:
            return web.json_response(
                {"error": "tab_id and action are required"}, status=400,
            )
        params = {k: v for k, v in data.items() if k not in ("tab_id", "action")}
        try:
            result = await self.send_action(int(tab_id), action, **params)
            return web.json_response(result)
        except Exception as exc:
            return web.json_response(
                {"success": False, "error": str(exc)}, status=500,
            )

    async def _handle_reload(self, _request: web.Request) -> web.Response:
        """Tell the extension to reload itself (picks up new background.js / content.js)."""
        if self._ws and not self._ws.closed:
            await self._ws.send_json({"type": "reload"})
            return web.json_response({"ok": True, "message": "Reload signal sent"})
        return web.json_response({"ok": False, "error": "Extension not connected"}, status=503)

    # ---- internal helpers ----

    async def _enqueue_from_extension(self, data: dict) -> str:
        if self.queue is None:
            raise RuntimeError("Queue not configured on BrowserServer")

        from .models import Task, TaskStatus, new_task_id

        prompt = data.get("prompt", "")
        tab_id = data.get("tab_id")

        if tab_id and tab_id in self._pinned_tabs:
            tab = self._pinned_tabs[tab_id]
            prompt = (
                f"[Browser context] The user is on: {tab.url} ({tab.title})\n"
                f"Page state:\n{tab.page_state}\n\n"
                f"User instruction: {prompt}"
            )

        task_id = new_task_id()
        task = Task(
            id=task_id,
            source="browser",
            prompt=prompt,
            status=TaskStatus.PENDING,
            session_key="browser:main",
        )
        await self.queue.enqueue(task)
        self._broadcast_log(f"Task queued: {data.get('prompt', '')[:80]}")
        return task_id
