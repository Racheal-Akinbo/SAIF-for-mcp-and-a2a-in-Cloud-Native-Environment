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
from mcp.server.transport_security import TransportSecuritySettings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.store import lookup_order, VALID_TOKEN  # noqa: E402

server = MCPServer(name="order-status-mcp-server", version="1.0")


@server.tool(
    name="get_order_status", description="Look up the status of a customer order by ID"
)
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

        # Unauthenticated health check, deliberately exempt from the auth
        # gate below. Kubernetes readiness/liveness probes cannot supply a
        # bearer token, so probing the real (protected) /mcp endpoint always
        # returns 401 -- which Kubernetes correctly treats as "not ready,"
        # forever. This endpoint exists purely so kubelet has something
        # unauthenticated to check that still proves the process is alive.
        if scope.get("path") == "/healthz":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})
            return

        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode()

        if auth != f"Bearer {VALID_TOKEN}":
            print(
                f"[mcp-server] !! unauthorized request rejected (missing/invalid bearer token)"
            )
            body = b'{"error": "unauthorized", "detail": "missing or invalid bearer token"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


def build_app():
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,  # keep protection on - don't disable it
        allowed_hosts=[
            "mcp.mcp-a2a-demo.svc.cluster.local:8001",
            "mcp.mcp-a2a-demo.svc.cluster.local",
            "mcp:8001",
            "mcp",
            "localhost:8001",
            "127.0.0.1:8001",
        ],
        allowed_origins=[],
    )
    inner_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=security,
    )
    return BearerAuthMiddleware(inner_app)


app = build_app()

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8001"))
    print(
        f"[mcp-server] starting at {time.strftime('%X')} on {host}:{port}, tool=get_order_status"
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")
