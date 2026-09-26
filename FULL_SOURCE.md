# Full source code: MCP vs A2A comparison + security gateway

Build order: `common/` → `mcp_app/` → `a2a_app/` → `gateway/` → `run_comparison.py`.

Local run:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 run_comparison.py
```

Containerized run:
```bash
docker compose up --build
```

---

## `requirements.txt`
*Dependencies*

```text
mcp
a2a-sdk
fastapi
uvicorn
httpx
pydantic

```

## `Dockerfile`
*Shared image for all three services, differentiated by command*

```dockerfile
# Single image shared by all three services (mcp, a2a, gateway).
# Which service actually runs is determined by the `command:` set per
# container in docker-compose.yml, not by anything baked into this image.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Containers must bind 0.0.0.0, not 127.0.0.1 -- otherwise the service is
# unreachable from other containers (or from the host via the published
# port), since 127.0.0.1 inside a container only means "this container."
ENV HOST=0.0.0.0

EXPOSE 8000 8001 8002

# Default command; each service in docker-compose.yml overrides this.
CMD ["python3", "gateway/app.py"]

```

## `docker-compose.yml`
*Orchestrates 3 containers on a real bridge network*

```yaml
# Brings up all three services as separate containers on a shared bridge
# network. This is the actual point of this file: inside the container
# network, the gateway reaches the MCP and A2A backends by their service
# names (http://mcp:8001, http://a2a:8002) -- real inter-container
# networking, not shared-localhost the way every previous test in this
# project ran. This is the first genuinely containerized test of the system.
#
# Run:   docker compose up --build
# Then:  open http://127.0.0.1:8000/ on the host -- the gateway's port is
#        published to the host, same dashboard as the non-container runs.
# Stop:  docker compose down

services:
  mcp:
    build: .
    command: python3 mcp_app/server.py
    environment:
      - HOST=0.0.0.0
      - PORT=8001
    ports:
      - "8001:8001"
    networks:
      - mcp-a2a-net

  a2a:
    build: .
    command: python3 a2a_app/server.py
    environment:
      - HOST=0.0.0.0
      - PORT=8002
      # Agent Card must advertise an address other containers can reach --
      # "a2a" is this container's DNS name on the compose network, not
      # 127.0.0.1 (which inside this container means only itself).
      - A2A_PUBLIC_URL=http://a2a:8002/a2a
    ports:
      - "8002:8002"
    networks:
      - mcp-a2a-net

  gateway:
    build: .
    command: python3 gateway/app.py
    environment:
      - HOST=0.0.0.0
      - PORT=8000
      - MCP_URL=http://mcp:8001/mcp
      - A2A_BASE=http://a2a:8002
    ports:
      - "8000:8000"
    depends_on:
      - mcp
      - a2a
    networks:
      - mcp-a2a-net

networks:
  mcp-a2a-net:
    driver: bridge

```

## `.dockerignore`
*Keeps venv/results out of the build context*

```text
venv/
__pycache__/
*.pyc
results/
.git/
.pytest_cache/
*.log

```

## `common/__init__.py`
*Package marker (empty)*

_(empty file — marks the directory as a Python package)_

## `common/store.py`
*Shared mock data + auth token used identically by both protocols*

```python
"""Shared mock backend used by both the MCP and A2A implementations.

Keeping the underlying data and business logic identical is what makes the
MCP-vs-A2A comparison fair: the only thing that differs between the two
apps is the *protocol* used to expose and invoke the same capability.
"""

ORDERS = {
    "ORD-1001": {"status": "shipped", "eta_days": 2, "carrier": "UPS"},
    "ORD-1002": {"status": "processing", "eta_days": 5, "carrier": None},
    "ORD-1003": {"status": "delivered", "eta_days": 0, "carrier": "FedEx"},
}

VALID_TOKEN = "demo-shared-secret-token"


def lookup_order(order_id: str) -> dict:
    if order_id not in ORDERS:
        raise KeyError(f"No such order: {order_id}")
    return {"order_id": order_id, **ORDERS[order_id]}

```

## `mcp_app/__init__.py`
*Package marker (empty)*

_(empty file — marks the directory as a Python package)_

## `mcp_app/server.py`
*MCP server: exposes get_order_status as a tool, container-aware via HOST/PORT env vars, with live trace logging*

```python
"""MCP server (Model Context Protocol) exposing a single tool: get_order_status.

Transport: streamable HTTP, so it is directly comparable to the A2A app,
which also runs over HTTP. Uses the official `mcp` SDK (v2.x).

Auth: a minimal bearer-token gate implemented as ASGI middleware. The mcp
SDK's full OAuth 2.1 provider (AuthSettings/TokenVerifier) requires standing
up an authorization-server issuer, which is overkill for this comparison
testbed -- but it is worth noting in the writeup that production MCP
deployments should use that full OAuth flow rather than a shared secret.
"""

import os
import sys
import time

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.store import lookup_order, VALID_TOKEN  # noqa: E402

server = MCPServer(name="order-status-mcp-server", version="1.0")


@server.tool(name="get_order_status", description="Look up the status of a customer order by ID")
def get_order_status(order_id: str) -> dict:
    print(f"[mcp-server] >> tool called: get_order_status(order_id={order_id!r})")
    try:
        result = lookup_order(order_id)
        print(f"[mcp-server] << success: {result}")
        return result
    except KeyError as exc:
        print(f"[mcp-server] << error: {exc}")
        raise


class BearerAuthMiddleware:
    """Minimal ASGI middleware enforcing 'Authorization: Bearer <token>'."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode()

        if auth != f"Bearer {VALID_TOKEN}":
            print(f"[mcp-server] !! unauthorized request rejected (missing/invalid bearer token)")
            body = b'{"error": "unauthorized", "detail": "missing or invalid bearer token"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


def build_app():
    inner_app = server.streamable_http_app(streamable_http_path="/mcp")
    return BearerAuthMiddleware(inner_app)


app = build_app()

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8001"))
    print(f"[mcp-server] starting at {time.strftime('%X')} on {host}:{port}, tool=get_order_status")
    uvicorn.run(app, host=host, port=port, log_level="warning")

```

## `mcp_app/client.py`
*MCP client: discovers and calls the tool, runs 3 test scenarios*

```python
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

```

## `mcp_app/run_demo.py`
*Runs the MCP server + client together in one process*

```python
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

```

## `a2a_app/__init__.py`
*Package marker (empty)*

_(empty file — marks the directory as a Python package)_

## `a2a_app/server.py`
*A2A agent: exposes get_order_status as a skill via Agent Card + JSON-RPC, container-aware, with live trace logging and the quote-format fix*

```python
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

```

## `a2a_app/client.py`
*A2A client: discovers the Agent Card, sends a task, runs 3 test scenarios*

```python
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

```

## `a2a_app/run_demo.py`
*Runs the A2A server + client together in one process*

```python
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

```

## `gateway/__init__.py`
*Package marker (empty)*

_(empty file — marks the directory as a Python package)_

## `gateway/app.py`
*Interoperability gateway: unified auth, unified errors, replay protection, live browser dashboard, live trace logging, container-aware backend URLs via env vars, and a pooled A2A HTTP client*

```python
"""Interoperability gateway: the missing piece from the architecture diagram.

Sits in front of both the MCP and A2A backends and provides three things
neither backend has on its own:

1. Single point of auth for callers (a "gateway token"), with the
   backend-specific tokens held only server-side and never exposed to the
   caller. Callers no longer need to know which downstream protocol is
   involved, or hold its credential.
2. A unified response/error schema, collapsing MCP's `isError` tool result
   and A2A's `failed` task state into one shape.
3. Replay protection (nonce + timestamp window) -- which, per the report's
   findings, *neither* MCP nor A2A implementation here provides on its own.
   This demonstrates the gateway closing a gap rather than just relaying it.

Caller-facing contract:
  POST /order-status
  Headers: Authorization: Bearer <GATEWAY_TOKEN>, X-Request-Id: <uuid>
  Body: {"order_id": "...", "backend": "mcp" | "a2a"}
"""

import os
import sys
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.store import VALID_TOKEN as BACKEND_TOKEN  # noqa: E402

app = FastAPI()

GATEWAY_TOKEN = "gateway-issued-token"  # what callers use -- distinct from BACKEND_TOKEN
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:8001/mcp")
A2A_BASE = os.environ.get("A2A_BASE", "http://127.0.0.1:8002")

# Persistent client for A2A calls, reused across requests instead of opened fresh
# each time. The live-validation test (see live_validation/live_validation_report.md,
# Section 5) found that constructing a new httpx.AsyncClient per request added a
# sustained ~1.2s cost per call on Windows; a shared, pooled client removes that
# per-call connection-setup cost.
_a2a_client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _startup():
    global _a2a_client
    _a2a_client = httpx.AsyncClient(timeout=10.0)


@app.on_event("shutdown")
async def _shutdown():
    if _a2a_client is not None:
        await _a2a_client.aclose()

# --- replay protection state (in-memory; a real deployment would use a
#     shared cache like Redis with TTL matching the window below) ---
SEEN_REQUEST_IDS: dict[str, float] = {}
REPLAY_WINDOW_SECONDS = 60


def check_replay(request_id: str) -> bool:
    """Returns True if this request_id has already been seen within the window."""
    now = time.time()
    # prune old entries
    for rid, ts in list(SEEN_REQUEST_IDS.items()):
        if now - ts > REPLAY_WINDOW_SECONDS:
            del SEEN_REQUEST_IDS[rid]

    if request_id in SEEN_REQUEST_IDS:
        return True
    SEEN_REQUEST_IDS[request_id] = now
    return False


def unauthorized(detail: str):
    return JSONResponse(status_code=401, content={"error": True, "code": "unauthorized", "message": detail})


async def call_mcp(order_id: str) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import httpx2

    headers = {"Authorization": f"Bearer {BACKEND_TOKEN}"}
    async with httpx2.AsyncClient(headers=headers, timeout=10.0) as hc:
        async with streamable_http_client(MCP_URL, http_client=hc) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                result = await s.call_tool("get_order_status", {"order_id": order_id})
                if result.is_error:
                    msg = result.content[0].text if result.content else "unknown MCP error"
                    return {"error": True, "code": "not_found", "message": msg, "source_protocol": "mcp"}
                import json as _json
                data = _json.loads(result.content[0].text)
                return {"error": False, "source_protocol": "mcp", **data}


async def call_a2a(order_id: str) -> dict:
    rpc_request = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "data", "data": {"order_id": order_id}}]}},
    }
    headers = {"Authorization": f"Bearer {BACKEND_TOKEN}"}
    resp = await _a2a_client.post(f"{A2A_BASE}/a2a", json=rpc_request, headers=headers)
    body = resp.json()
    task = body.get("result", {})
    state = task.get("status", {}).get("state")
    if state != "completed":
        msg = task.get("status", {}).get("message", "unknown A2A error")
        return {"error": True, "code": "not_found", "message": msg, "source_protocol": "a2a"}
    artifact_data = task["artifacts"][0]["parts"][0]["data"]
    return {"error": False, "source_protocol": "a2a", **artifact_data}


DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>MCP / A2A Gateway — Live Test Console</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#0f1115; color:#e6e6e6;
         max-width: 760px; margin: 40px auto; padding: 0 20px; }
  h1 { font-size: 20px; font-weight: 600; }
  .sub { color:#9aa0a6; font-size: 13px; margin-bottom: 28px; }
  .row { display:flex; gap:10px; margin-bottom: 14px; align-items:center; }
  label { width: 110px; font-size: 13px; color:#9aa0a6; }
  select, input { flex:1; background:#1a1d23; border:1px solid #2a2d35; color:#e6e6e6;
                  padding:8px 10px; border-radius:6px; font-size:14px; }
  button { background:#3b6fef; color:white; border:none; padding:9px 16px; border-radius:6px;
           font-size:14px; cursor:pointer; margin-right:8px; }
  button.secondary { background:#2a2d35; }
  button:hover { opacity:0.9; }
  #result { margin-top:20px; padding:16px; background:#1a1d23; border-radius:8px;
            border:1px solid #2a2d35; white-space:pre-wrap; font-family: ui-monospace, monospace;
            font-size:13px; min-height: 60px; }
  .ok { border-left:4px solid #3ecf8e; }
  .err { border-left:4px solid #ef4b4b; }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px;
           background:#2a2d35; color:#9aa0a6; margin-left:8px; }
  .log { margin-top:16px; font-size:12px; color:#9aa0a6; }
</style>
</head>
<body>
  <h1>Gateway live test console</h1>
  <div class="sub">Talks to this running gateway in real time — no batch scripts, immediate responses.</div>

  <div class="row">
    <label>Order ID</label>
    <select id="orderId">
      <option>ORD-1001</option>
      <option>ORD-1002</option>
      <option>ORD-1003</option>
      <option>ORD-9999</option>
    </select>
  </div>
  <div class="row">
    <label>Backend</label>
    <select id="backend">
      <option value="mcp">MCP</option>
      <option value="a2a">A2A</option>
    </select>
  </div>

  <div class="row">
    <button onclick="sendRequest(true)">Send request</button>
    <button class="secondary" onclick="replayLast()">Replay last request (attack test)</button>
    <button class="secondary" onclick="sendBadToken()">Send with wrong token</button>
  </div>

  <div id="result">Results appear here as soon as you send a request.</div>
  <div class="log" id="log"></div>

<script>
let lastRequestId = null;
let lastBody = null;

function logLine(msg) {
  const el = document.getElementById('log');
  const t = new Date().toLocaleTimeString();
  el.textContent = `[${t}] ${msg}\\n` + el.textContent;
}

async function send(headers, body) {
  const resultEl = document.getElementById('result');
  const start = performance.now();
  try {
    const res = await fetch('/order-status', { method: 'POST', headers, body: JSON.stringify(body) });
    const elapsed = (performance.now() - start).toFixed(1);
    const data = await res.json();
    resultEl.className = res.ok ? 'ok' : 'err';
    resultEl.textContent = `HTTP ${res.status}  (round-trip: ${elapsed} ms)\\n\\n` + JSON.stringify(data, null, 2);
    logLine(`${body.backend.toUpperCase()} ${body.order_id} -> HTTP ${res.status} in ${elapsed} ms`);
  } catch (e) {
    resultEl.className = 'err';
    resultEl.textContent = 'Request failed: ' + e;
    logLine('Request failed: ' + e);
  }
}

function sendRequest(newId) {
  const orderId = document.getElementById('orderId').value;
  const backend = document.getElementById('backend').value;
  if (newId) lastRequestId = crypto.randomUUID();
  lastBody = { order_id: orderId, backend };
  send({
    'Content-Type': 'application/json',
    'Authorization': 'Bearer gateway-issued-token',
    'X-Request-Id': lastRequestId
  }, lastBody);
}

function replayLast() {
  if (!lastRequestId) { logLine('Nothing to replay yet — send a request first.'); return; }
  logLine('Replaying previous request-id: ' + lastRequestId + ' (expect 409)');
  send({
    'Content-Type': 'application/json',
    'Authorization': 'Bearer gateway-issued-token',
    'X-Request-Id': lastRequestId   // reused on purpose
  }, lastBody);
}

function sendBadToken() {
  const orderId = document.getElementById('orderId').value;
  const backend = document.getElementById('backend').value;
  send({
    'Content-Type': 'application/json',
    'Authorization': 'Bearer wrong-token',
    'X-Request-Id': crypto.randomUUID()
  }, { order_id: orderId, backend });
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


@app.post("/order-status")
async def order_status(request: Request):
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {GATEWAY_TOKEN}":
        print("[gateway] !! rejected: invalid gateway token (backend never contacted)")
        return unauthorized("missing or invalid gateway token")

    request_id = request.headers.get("x-request-id")
    if not request_id:
        return JSONResponse(status_code=400, content={"error": True, "code": "missing_request_id",
                                                        "message": "X-Request-Id header is required"})
    if check_replay(request_id):
        print(f"[gateway] !! REPLAY DETECTED: request_id={request_id} was already processed -- blocking")
        return JSONResponse(status_code=409, content={"error": True, "code": "replay_detected",
                                                        "message": f"request_id {request_id} was already processed"})

    body = await request.json()
    order_id = body.get("order_id")
    backend = body.get("backend", "mcp")
    print(f"[gateway] >> routing order_id={order_id!r} to backend={backend.upper()} (request_id={request_id})")

    start = time.perf_counter()
    try:
        if backend == "mcp":
            result = await call_mcp(order_id)
        elif backend == "a2a":
            result = await call_a2a(order_id)
        else:
            return JSONResponse(status_code=400, content={"error": True, "code": "unknown_backend",
                                                            "message": f"backend must be 'mcp' or 'a2a', got {backend!r}"})
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": True, "code": "backend_unreachable",
                                                        "message": str(exc)[:200], "source_protocol": backend})

    result["gateway_latency_ms"] = round((time.perf_counter() - start) * 1000, 2)
    status_code = 404 if result.get("error") else 200
    print(f"[gateway] << responding HTTP {status_code} ({result['gateway_latency_ms']} ms)")
    return JSONResponse(status_code=status_code, content=result)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"[gateway] starting on {host}:{port}, mediating MCP ({MCP_URL}) and A2A ({A2A_BASE})")
    uvicorn.run(app, host=host, port=port, log_level="warning")

```

## `gateway/run_demo.py`
*Runs MCP + A2A + gateway together and tests the replay attack*

```python
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

```

## `run_comparison.py`
*Top-level orchestrator: runs all three demos and builds the comparison table*

```python
"""Runs both the MCP and A2A demos in sequence, then prints and saves a
side-by-side comparison of the results.

Each protocol runs in its own subprocess so their asyncio event loops and
ASGI servers don't collide.
"""

import json
import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable


def run(script_path: str, label: str):
    print(f"\n=== running {label} ===")
    proc = subprocess.run(
        [PYTHON, "-u", script_path],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # surface only the demo's own print lines, not the noisy httpx/uvicorn logs
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.startswith("[") and "HTTP Request" not in line:
            print(line)
    if proc.returncode != 0:
        print(f"!! {label} exited with code {proc.returncode}")
        print(proc.stderr[-2000:])


def load(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def main():
    run(os.path.join(PROJECT_ROOT, "mcp_app", "run_demo.py"), "MCP demo")
    run(os.path.join(PROJECT_ROOT, "a2a_app", "run_demo.py"), "A2A demo")
    run(os.path.join(PROJECT_ROOT, "gateway", "run_demo.py"), "Gateway demo")

    mcp_results = load(os.path.join(PROJECT_ROOT, "results", "mcp_run.json"))
    a2a_results = load(os.path.join(PROJECT_ROOT, "results", "a2a_run.json"))

    by_scenario = {}
    for r in mcp_results:
        by_scenario.setdefault(r["scenario"], {})["MCP"] = r
    for r in a2a_results:
        by_scenario.setdefault(r["scenario"], {})["A2A"] = r

    print("\n=== comparison ===")
    header = f"{'scenario':<12} {'protocol':<6} {'outcome':<10} {'total_ms':>10} {'payload_bytes':>14}"
    print(header)
    print("-" * len(header))
    rows = []
    for scenario, protos in by_scenario.items():
        for proto_name in ("MCP", "A2A"):
            r = protos.get(proto_name)
            if not r:
                continue
            row = {
                "scenario": scenario,
                "protocol": proto_name,
                "outcome": r.get("outcome"),
                "total_ms": r.get("total_ms"),
                "payload_bytes": r.get("payload_bytes"),
            }
            rows.append(row)
            print(f"{row['scenario']:<12} {row['protocol']:<6} {str(row['outcome']):<10} "
                  f"{row['total_ms']:>10} {str(row['payload_bytes']):>14}")

    out_path = os.path.join(PROJECT_ROOT, "results", "comparison.json")
    with open(out_path, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"\nSaved comparison table to {out_path}")


if __name__ == "__main__":
    main()

```
