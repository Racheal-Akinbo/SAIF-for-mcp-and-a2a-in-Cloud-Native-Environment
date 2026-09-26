"""A2A remote agent exposing 'get_order_status' as a skill.

Implemented directly against the A2A wire protocol (Agent Card at
/.well-known/agent.json + JSON-RPC 2.0 'message/send' over HTTP) rather than
the full a2a-sdk, which is oriented around a persistent-storage/task-queue
deployment model (DB migrations, task store, push notifications) that is
heavier than this comparison testbed needs. The wire format below matches
the public A2A spec: Agent Card discovery, JSON-RPC 2.0 envelope, and a
Task object with lifecycle states (submitted -> completed / failed).

Auth: same bearer-token gate as the MCP server, so the two protocols are
compared on equal footing rather than one being "more secure" purely
because it was configured more strictly.
"""

import os
import sys
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.store import lookup_order, VALID_TOKEN  # noqa: E402

app = FastAPI()

AGENT_CARD = {
    "name": "order-status-a2a-agent",
    "description": "Agent that reports order status for a given order id",
    "version": "1.0.0",
    "url": os.environ.get("A2A_PUBLIC_URL", "http://127.0.0.1:8002/a2a"),
    "capabilities": {"streaming": False, "pushNotifications": False},
    "securitySchemes": {
        "bearerAuth": {"type": "http", "scheme": "bearer"}
    },
    "security": [{"bearerAuth": []}],
    "skills": [
        {
            "id": "get_order_status",
            "name": "Get order status",
            "description": "Look up the status of a customer order by ID",
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        }
    ],
}


@app.get("/.well-known/agent.json")
async def agent_card():
    return AGENT_CARD


def check_auth(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    ok = auth == f"Bearer {VALID_TOKEN}"
    if not ok:
        print("[a2a-server] !! unauthorized request rejected (missing/invalid bearer token)")
    return ok


@app.post("/a2a")
async def jsonrpc_endpoint(request: Request):
    if not check_auth(request):
        return JSONResponse(
            status_code=401,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32001, "message": "Unauthorized: missing or invalid bearer token"},
            },
        )

    body = await request.json()
    method = body.get("method")
    req_id = body.get("id")

    if method != "message/send":
        return JSONResponse(
            status_code=400,
            content={"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}},
        )

    params = body.get("params", {})
    message = params.get("message", {})
    parts = message.get("parts", [])
    order_id = None
    for part in parts:
        if part.get("kind") == "data" and "order_id" in part.get("data", {}):
            order_id = part["data"]["order_id"]

    task_id = str(uuid.uuid4())

    if not order_id:
        print("[a2a-server] << task failed: order_id missing from message parts")
        task = {
            "id": task_id,
            "status": {"state": "failed", "message": "order_id missing from message parts"},
        }
    else:
        print(f"[a2a-server] >> skill invoked: get_order_status(order_id={order_id!r})")
        try:
            data = lookup_order(order_id)
            print(f"[a2a-server] << task completed: {data}")
            task = {
                "id": task_id,
                "status": {"state": "completed"},
                "artifacts": [
                    {"name": "order_status", "parts": [{"kind": "data", "data": data}]}
                ],
            }
        except KeyError as exc:
            print(f"[a2a-server] << task failed: {exc}")
            task = {
                "id": task_id,
                "status": {"state": "failed", "message": str(exc.args[0])},
            }

    return {"jsonrpc": "2.0", "id": req_id, "result": task}


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8002"))
    print(f"[a2a-server] starting at {time.strftime('%X')} on {host}:{port}, skill=get_order_status")
    uvicorn.run(app, host=host, port=port, log_level="warning")
