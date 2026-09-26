"""Runs the A2A server and client in a single process, mirroring mcp_app/run_demo.py."""

import asyncio
import json
import os
import sys

import httpx
import uvicorn

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
from a2a_app.server import app as a2a_asgi_app  # noqa: E402
from a2a_app.client import run_all_scenarios  # noqa: E402

HOST, PORT = "127.0.0.1", 8002
RESULTS_PATH = os.path.join(PROJECT_ROOT, "results", "a2a_run.json")


async def wait_until_up(url: str, timeout: float = 5.0):
    async with httpx.AsyncClient() as c:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                r = await c.get(url)
                if r.status_code == 200:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError("A2A server did not come up in time")


async def main():
    config = uvicorn.Config(a2a_asgi_app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    await wait_until_up(f"http://{HOST}:{PORT}/.well-known/agent.json")
    print("[a2a-demo] server is up, running client scenarios...")

    results = await run_all_scenarios()

    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)

    server.should_exit = True
    await server_task
    print("[a2a-demo] done, server stopped")


if __name__ == "__main__":
    asyncio.run(main())
