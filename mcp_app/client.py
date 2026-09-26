"""MCP client agent: discovers the get_order_status tool over MCP and calls it.

Runs three scenarios, each timed and logged to results/mcp_run.json:
  1. happy_path   - valid token, valid order id
  2. no_auth      - token omitted entirely (expect: rejected before reaching the tool)
  3. bad_order    - valid token, nonexistent order id (expect: tool-level error, not a crash)
"""

import asyncio
import json
import os
import sys
import time

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.store import VALID_TOKEN  # noqa: E402

MCP_URL = "http://127.0.0.1:8001/mcp"


async def run_scenario(name: str, token: str | None, order_id: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    start = time.perf_counter()
    result = {"scenario": name, "protocol": "MCP"}

    try:
        async with httpx2.AsyncClient(headers=headers, timeout=10.0) as http_client:
            async with streamable_http_client(MCP_URL, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    discover_start = time.perf_counter()
                    tools = await session.list_tools()
                    discover_ms = (time.perf_counter() - discover_start) * 1000

                    call_start = time.perf_counter()
                    call_result = await session.call_tool("get_order_status", {"order_id": order_id})
                    call_ms = (time.perf_counter() - call_start) * 1000

                    payload_text = json.dumps([c.model_dump() for c in call_result.content])
                    result.update({
                        "outcome": "error" if call_result.is_error else "success",
                        "discovered_tools": [t.name for t in tools.tools],
                        "discover_ms": round(discover_ms, 2),
                        "call_ms": round(call_ms, 2),
                        "payload_bytes": len(payload_text.encode()),
                        "response": payload_text,
                    })
    except Exception as exc:  # auth rejection surfaces as a transport-level exception here
        result.update({
            "outcome": "rejected",
            "error_type": type(exc).__name__,
            "error": str(exc)[:200],
        })

    result["total_ms"] = round((time.perf_counter() - start) * 1000, 2)
    return result


async def run_all_scenarios() -> list[dict]:
    scenarios = [
        ("happy_path", VALID_TOKEN, "ORD-1001"),
        ("no_auth", None, "ORD-1001"),
        ("bad_order", VALID_TOKEN, "ORD-9999"),
    ]
    results = []
    for name, token, order_id in scenarios:
        r = await run_scenario(name, token, order_id)
        print(f"[mcp-client] {name}: {r['outcome']} ({r['total_ms']} ms)")
        results.append(r)
    return results


RESULTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results", "mcp_run.json"
)


async def main():
    results = await run_all_scenarios()
    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
