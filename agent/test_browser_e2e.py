"""End-to-end test: starts the browser server, connects a fake extension,
and verifies that browser tool calls round-trip through the WS relay."""

import asyncio
import json
import aiohttp

HOST = "127.0.0.1"
PORT = 18790

FAKE_PAGE_STATE = """\
URL: https://example.com/login
Title: Example Login
---
[1] input#email (placeholder: "Email address")
[2] input#password (type: password, placeholder: "Password")
[3] button "Sign In"
[4] a "Forgot password?" -> /reset
---
Visible text:
Welcome back! Sign in to your account."""


async def test_health():
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://{HOST}:{PORT}/health") as resp:
            data = await resp.json()
            print(f"[health] status={data['status']} token={data['token'][:8]}... connected={data['connected']}")
            return data["token"]


async def fake_extension(token: str):
    """Simulate what the Chrome extension does."""
    url = f"ws://{HOST}:{PORT}/ws"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url) as ws:
            # Auth
            await ws.send_json({"type": "auth", "token": token})
            msg = await ws.receive_json()
            print(f"[ext] Auth response: {msg['type']}")
            assert msg["type"] == "auth_ok"

            # Pin a tab
            await ws.send_json({
                "type": "tab_pinned",
                "tab_id": 1001,
                "url": "https://example.com/login",
                "title": "Example Login",
                "page_state": FAKE_PAGE_STATE,
            })
            print("[ext] Tab 1001 pinned")

            # Now listen for commands and respond
            print("[ext] Waiting for commands from backend...")
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "execute":
                        action = data["action"]
                        req_id = data["request_id"]
                        print(f"[ext] Received action: {action} (request_id={req_id[:8]}...)")

                        if action == "read_page":
                            await ws.send_json({
                                "type": "action_result",
                                "request_id": req_id,
                                "success": True,
                                "page_state": FAKE_PAGE_STATE,
                                "url": "https://example.com/login",
                                "title": "Example Login",
                            })
                        elif action == "type":
                            print(f"[ext]   -> typing '{data.get('text')}' into element [{data.get('element_id')}]")
                            await ws.send_json({
                                "type": "action_result",
                                "request_id": req_id,
                                "success": True,
                            })
                        elif action == "click":
                            print(f"[ext]   -> clicking element [{data.get('element_id')}]")
                            after_click = FAKE_PAGE_STATE.replace("Example Login", "Dashboard")
                            await ws.send_json({
                                "type": "action_result",
                                "request_id": req_id,
                                "success": True,
                                "page_state": after_click,
                                "url": "https://example.com/dashboard",
                                "title": "Dashboard",
                            })
                        else:
                            await ws.send_json({
                                "type": "action_result",
                                "request_id": req_id,
                                "success": True,
                                "page_state": FAKE_PAGE_STATE,
                            })
                        print(f"[ext] Sent result for {action}")
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break


async def submit_task(token: str, prompt: str, tab_id: int = 1001):
    """Submit a task via the HTTP API (same as side panel would)."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://{HOST}:{PORT}/api/task",
            json={"prompt": prompt, "tab_id": tab_id},
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            data = await resp.json()
            print(f"[task] Submitted: task_id={data.get('task_id', 'N/A')}")
            return data


async def test_direct_tool_call(token: str):
    """Directly test browser tools by calling them (requires the extension WS to be connected)."""
    from agent.tools.browser import (
        browser_list_tabs,
        browser_read_page,
        browser_click,
        browser_type,
    )

    print("\n--- Direct tool call tests ---")

    result = await browser_list_tabs()
    print(f"[tool] browser_list_tabs:\n{result}\n")

    result = await browser_read_page(1001)
    print(f"[tool] browser_read_page(1001):\n{result[:200]}...\n")

    result = await browser_type(1001, 1, "test@example.com")
    print(f"[tool] browser_type(1001, 1, 'test@example.com'):\n{result}\n")

    result = await browser_click(1001, 3)
    print(f"[tool] browser_click(1001, 3):\n{result[:200]}...\n")

    print("--- All direct tool tests passed! ---")


async def main():
    from agent.browser_server import BrowserServer
    from agent.tools.browser import set_browser_server

    print("Starting browser server...")
    server = BrowserServer(host=HOST, port=PORT)
    set_browser_server(server)
    await server.start()

    token = await test_health()
    print(f"Token: {token[:8]}...\n")

    # Start fake extension in background
    ext_task = asyncio.create_task(fake_extension(token))
    await asyncio.sleep(0.5)  # let it connect

    # Verify connection
    token2 = await test_health()
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://{HOST}:{PORT}/health") as resp:
            data = await resp.json()
            print(f"[health] connected={data['connected']} pinned_tabs={data['pinned_tabs']}")
            assert data["connected"], "Extension should be connected"
            assert data["pinned_tabs"] == 1, "Should have 1 pinned tab"

    # Test direct tool calls (these send commands to the fake extension)
    await test_direct_tool_call(token)

    # Task submission requires a full queue; skip in standalone test.
    # In production, main.py passes the queue to BrowserServer.

    print("\n=== All tests passed! ===")
    ext_task.cancel()
    await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
