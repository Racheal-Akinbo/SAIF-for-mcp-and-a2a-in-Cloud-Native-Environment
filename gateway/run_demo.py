"""Starts the MCP backend, A2A backend, and gateway all in one process, then
exercises the gateway with:
  1. unified success calls through each backend
  2. a caller using an invalid gateway token (backend tokens are never
     exposed to callers, so this also implicitly proves credential isolation)
  3. a genuine replay attack: capture a valid request, replay the exact same
     bytes, confirm the gateway's nonce check rejects the second attempt --
     something neither raw MCP nor raw A2A did in the earlier tests.
"""

import asyncio
import json
import os
import sys
import uuid

import httpx
import uvicorn

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from mcp_app.server import app as mcp_app  # noqa: E402
from a2a_app.server import app as a2a_app  # noqa: E402
from gateway.app import app as gateway_app, GATEWAY_TOKEN  # noqa: E402

RESULTS_PATH = os.path.join(PROJECT_ROOT, "results", "gateway_run.json")


async def wait_up(url: str, method="GET", timeout=5.0):
    async with httpx.AsyncClient() as c:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                if method == "GET":
                    r = await c.get(url)
                else:
                    r = await c.post(url, json={})
                if r.status_code < 500:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError(f"{url} did not come up in time")


async def main():
    servers = []
    for asgi_app, host, port in [
        (mcp_app, "127.0.0.1", 8001),
        (a2a_app, "127.0.0.1", 8002),
        (gateway_app, "127.0.0.1", 8000),
    ]:
        config = uvicorn.Config(asgi_app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        servers.append((server, task))

    await wait_up("http://127.0.0.1:8001/mcp", method="POST")
    await wait_up("http://127.0.0.1:8002/.well-known/agent.json")
    await wait_up("http://127.0.0.1:8000/order-status", method="POST")
    print("[gateway-demo] all three services up")

    results = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        good_headers = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}

        # 1. unified success through MCP backend
        rid = str(uuid.uuid4())
        r = await client.post("http://127.0.0.1:8000/order-status",
                               json={"order_id": "ORD-1001", "backend": "mcp"},
                               headers={**good_headers, "X-Request-Id": rid})
        print(f"[gateway-demo] via MCP: {r.status_code} {r.json()}")
        results.append({"case": "unified_success_mcp", "status": r.status_code, "body": r.json()})

        # 2. unified success through A2A backend
        rid2 = str(uuid.uuid4())
        r = await client.post("http://127.0.0.1:8000/order-status",
                               json={"order_id": "ORD-1002", "backend": "a2a"},
                               headers={**good_headers, "X-Request-Id": rid2})
        print(f"[gateway-demo] via A2A: {r.status_code} {r.json()}")
        results.append({"case": "unified_success_a2a", "status": r.status_code, "body": r.json()})

        # 3. invalid gateway token -- caller never had the real backend token to begin with
        r = await client.post("http://127.0.0.1:8000/order-status",
                               json={"order_id": "ORD-1001", "backend": "mcp"},
                               headers={"Authorization": "Bearer wrong-token", "X-Request-Id": str(uuid.uuid4())})
        print(f"[gateway-demo] bad gateway token: {r.status_code} {r.json()}")
        results.append({"case": "invalid_gateway_token", "status": r.status_code, "body": r.json()})

        # 4. REPLAY ATTACK: resend request #1's exact body and request-id
        r = await client.post("http://127.0.0.1:8000/order-status",
                               json={"order_id": "ORD-1001", "backend": "mcp"},
                               headers={**good_headers, "X-Request-Id": rid})  # reused rid!
        print(f"[gateway-demo] replay of request 1: {r.status_code} {r.json()}")
        results.append({"case": "replay_attack", "status": r.status_code, "body": r.json()})

    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)

    for server, task in servers:
        server.should_exit = True
    await asyncio.gather(*(t for _, t in servers))
    print("[gateway-demo] done, all services stopped")


if __name__ == "__main__":
    asyncio.run(main())
