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
        print(f"[gateway] !! backend call failed (backend={backend}, order_id={order_id}): {type(exc).__name__}: {exc}")
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
