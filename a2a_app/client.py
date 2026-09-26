"""A2A client agent: discovers the remote agent's Agent Card, then sends a
task via JSON-RPC 'message/send' to invoke the get_order_status skill.

Mirrors mcp_app/client.py exactly in scenario structure so results are
directly comparable:
  1. happy_path   - valid token, valid order id
  2. no_auth      - token omitted entirely
  3. bad_order    - valid token, nonexistent order id
"""

import asyncio
import json
import os
import sys
import time
import uuid

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.store import VALID_TOKEN  # noqa: E402

BASE_URL = "http://127.0.0.1:8002"


async def run_scenario(name: str, token: str | None, order_id: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    start = time.perf_counter()
    result = {"scenario": name, "protocol": "A2A"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Step 1: discover the Agent Card (unauthenticated, public metadata)
            discover_start = time.perf_counter()
            card_resp = await client.get(f"{BASE_URL}/.well-known/agent.json")
            card_resp.raise_for_status()
            card = card_resp.json()
            discover_ms = (time.perf_counter() - discover_start) * 1000

            # Step 2: send a task invoking the discovered skill
            rpc_request = {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "data", "data": {"order_id": order_id}}],
                    }
                },
            }

            call_start = time.perf_counter()
            resp = await client.post(f"{BASE_URL}/a2a", json=rpc_request, headers=headers)
            call_ms = (time.perf_counter() - call_start) * 1000

            payload_text = resp.text

            if resp.status_code == 401:
                result.update({
                    "outcome": "rejected",
                    "http_status": resp.status_code,
                    "error": resp.json().get("error", {}).get("message"),
                })
            else:
                rpc_response = resp.json()
                task_state = rpc_response.get("result", {}).get("status", {}).get("state")
                result.update({
                    "outcome": "success" if task_state == "completed" else "error",
                    "task_state": task_state,
                    "discovered_skills": [s["id"] for s in card.get("skills", [])],
                    "discover_ms": round(discover_ms, 2),
                    "call_ms": round(call_ms, 2),
                    "payload_bytes": len(payload_text.encode()),
                    "response": payload_text,
                })
    except Exception as exc:
        result.update({
            "outcome": "error",
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
        print(f"[a2a-client] {name}: {r['outcome']} ({r['total_ms']} ms)")
        results.append(r)
    return results


RESULTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results", "a2a_run.json"
)


async def main():
    results = await run_all_scenarios()
    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
