"""Runs the MCP server and client in a single process for a self-contained demo.

Avoids relying on a background server process surviving across tool calls --
starts uvicorn in an asyncio task, waits for readiness, runs the client
scenarios, then tears the server down cleanly.
"""

import asyncio
import json
import os
import sys

import httpx2
import uvicorn

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
from mcp_app.server import app as mcp_asgi_app  # noqa: E402
from mcp_app.client import run_all_scenarios  # noqa: E402

HOST, PORT = "127.0.0.1", 8001
RESULTS_PATH = os.path.join(PROJECT_ROOT, "results", "mcp_run.json")


async def wait_until_up(url: str, timeout: float = 5.0):
    async with httpx2.AsyncClient() as c:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                await c.post(url, json={})
                return
            except Exception:
                await asyncio.sleep(0.1)
    raise RuntimeError("MCP server did not come up in time")


async def main():
    config = uvicorn.Config(mcp_asgi_app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    await wait_until_up(f"http://{HOST}:{PORT}/mcp")
    print("[mcp-demo] server is up, running client scenarios...")

    results = await run_all_scenarios()

    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)

    server.should_exit = True
    await server_task
    print("[mcp-demo] done, server stopped")


if __name__ == "__main__":
    asyncio.run(main())
