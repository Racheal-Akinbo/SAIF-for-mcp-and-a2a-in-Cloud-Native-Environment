# MCP vs A2A: implementation notes and comparison findings

This document reports on a working testbed that executes the *same* task
("look up an order's status") once through MCP and once through A2A, with
identical auth gating and identical failure scenarios, so the two protocols
can be compared on equal footing rather than by reputation.

Code lives alongside this report: `mcp_app/`, `a2a_app/`, `common/store.py`
(shared mock data), and `run_comparison.py` (orchestrates both and produces
`results/comparison.json`).

## 1. What was built

| | MCP | A2A |
|---|---|---|
| Role in the stack | App-to-tool (client discovers and calls a tool) | Agent-to-agent (client sends a task to a peer agent) |
| Implementation | Official `mcp` Python SDK v2.1.1, streamable HTTP transport | Hand-built against the public A2A wire spec (Agent Card + JSON-RPC 2.0) |
| Capability exposed | One tool: `get_order_status(order_id)` | One skill: `get_order_status`, invoked via a `message/send` task |
| Auth | Bearer token, enforced by custom ASGI middleware in front of the MCP app | Bearer token, enforced in the JSON-RPC handler before dispatch |
| Discovery | `list_tools()` over an initialized MCP session | `GET /.well-known/agent.json` (Agent Card), unauthenticated |

**Why a hand-built A2A server instead of `a2a-sdk`:** the official SDK is
built around a persistent task store with DB migrations and a push-notification
model intended for long-running, resumable agent tasks. That's the right
design for production A2A deployments, but it's a heavier unit of comparison
than a single-call MCP tool invocation. Implementing directly against the
wire spec (Agent Card, JSON-RPC envelope, task lifecycle states) keeps the
comparison apples-to-apples: one HTTP round trip's worth of protocol
overhead on each side, not "SDK convenience feature" vs "raw call."

**Why a shared bearer token instead of full OAuth 2.1:** both `mcp`'s
`AuthSettings`/`TokenVerifier` machinery and A2A's OAuth support assume a
running authorization server (issuer metadata, token endpoint, etc.). Standing
one up doesn't change what the comparison is trying to show — how each
protocol behaves when a caller does vs. doesn't present a valid credential
— so a shared secret checked at the same layer on both sides isolates that
variable. This is flagged explicitly here so it doesn't get mistaken for a
security recommendation: **production deployments of either protocol should
use real OAuth 2.1 / mTLS, not a shared secret.**

## 2. Results

Three scenarios, run three times each (see `results/comparison.json` for the
raw numbers from the last run):

| Scenario | MCP outcome | MCP total (ms) | A2A outcome | A2A total (ms) |
|---|---|---|---|---|
| `happy_path` (valid token, valid order) | success | 35–49 | success | 23–24 |
| `no_auth` (token omitted) | rejected before tool executes | 21–27 | rejected (HTTP 401) before dispatch | 22–23 |
| `bad_order` (valid token, unknown order) | tool-level error, no crash | 37–48 | task state `failed`, no crash | 21–23 |

**Auth behavior — both fail closed correctly.** Omitting the token was
rejected in both protocols before any application logic ran: MCP's
middleware returned 401 at the transport layer (surfacing to the client as a
connection-level exception), and A2A's handler returned a structured
JSON-RPC error with a 401 status. Neither leaked whether `ORD-1001` exists
to an unauthenticated caller — a basic but real information-disclosure check.

**Bad input is handled as data, not as a fault, on both sides.** An unknown
order ID didn't crash either server; MCP represented it as an
`isError`-flagged tool result, A2A as a task in the `failed` state with a
message. This is a case where the two protocols converge on the same shape
of answer (structured failure, not an exception bubbling to the transport)
despite very different underlying models.

**The consistent latency gap is structural, not noise.** Across three runs,
MCP's authenticated calls were reliably ~1.5–2x slower than A2A's, even
though the underlying work (a dict lookup) is identical and both run
same-host with no real network latency. The cause is visible in the
per-phase timings: MCP's streamable-HTTP transport does session
initialization (`POST` init → `POST` notify → `GET` SSE stream open) before
the actual tool call, then a `DELETE` to terminate the session — five HTTP
exchanges per call in this implementation. A2A's `message/send` is a single
`POST` after an unauthenticated `GET` for the Agent Card, which a real
client would cache rather than re-fetch every call. **This is a protocol
design difference worth stating carefully**: MCP's session model exists to
support stateful, multi-turn tool interactions (subscriptions, sampling,
elicitation) that a single stateless request can't express — the overhead
buys capability that A2A's simpler single-shot task model here doesn't need
for *this* task. It would matter more for the multi-turn case.

## 3. Threat-model checks run

| Check | MCP result | A2A result |
|---|---|---|
| Missing/invalid token | Rejected pre-tool (401 at transport) | Rejected pre-dispatch (401 + JSON-RPC error) |
| Malformed/unknown resource ID | Structured error, no crash | Task `failed` state, no crash |
| Unauthenticated discovery | Tool list requires an initialized (authenticated) session | Agent Card is public by design — no credential exposed in it |

**Note on discovery asymmetry**: A2A's Agent Card is meant to be publicly
fetchable (that's the point — agents discover peers by URL). MCP's tool
list, in this implementation, sits behind the same session/auth boundary as
the tool call itself. That's a real design difference: A2A pushes you
toward "public capability description, gated invocation," while MCP as
configured here gates both. Neither is wrong, but a framework unifying the
two needs to decide, deliberately, whether MCP tool manifests should also be
made public — doing so by default would change the attack surface (schema
enumeration becomes possible pre-auth) in a way worth calling out in the
paper's threat model rather than leaving implicit.

Not run in this pass (documented as scope limits): TLS/mTLS enforcement
(both run over plain HTTP on localhost) and load/DoS behavior under
concurrent requests.

## 4. The gateway: closing the replay gap

A minimal interoperability gateway (`gateway/app.py`) now sits in front of
both backends and was validated directly against the gap identified above:

- **Confirmed the vulnerability first.** Replaying an identical, validly
  authenticated A2A request (same JSON-RPC `id`, same body, same token)
  against the raw backend succeeded twice with an identical `200 completed`
  response -- there is no nonce or timestamp check in either raw
  implementation, so a captured request is fully replayable within its
  auth token's lifetime.
- **Then closed it at the gateway.** The gateway requires a per-request
  `X-Request-Id` and rejects any ID it has already seen within a 60-second
  window (`409 replay_detected`). Re-sending request #1 verbatim through the
  gateway was rejected while the original succeeded -- confirmed in the same
  test run (`gateway/run_demo.py`, case `replay_attack`).
- **Credential isolation.** Callers authenticate to the gateway with a
  `GATEWAY_TOKEN` that is distinct from the backend token
  (`common.store.VALID_TOKEN`); the backend token never leaves the gateway
  process. A caller presenting a wrong or absent gateway token is rejected
  with 401 before either backend is ever contacted -- so a compromised
  caller credential can't be replayed directly against MCP or A2A.
- **Unified schema.** Both `unified_success_mcp` and `unified_success_a2a`
  cases return the identical response shape
  (`order_id/status/eta_days/carrier/source_protocol`) regardless of which
  backend actually served the request, collapsing MCP's `isError` tool
  result and A2A's `failed` task state into one error taxonomy
  (`{"error": bool, "code": ..., "message": ...}`).

This is a direct, testable instance of the earlier architecture diagram: the
gateway is not just a router, it demonstrably adds a security property
(replay resistance) that neither underlying protocol implementation
provides on its own -- which is the actual thesis of a "security-aware
interoperability framework" rather than just a compatibility shim.

## 5. What this demonstrates for the framework proposal

Tying back to the interoperability-gateway architecture from earlier: this
testbed is effectively the two "domain" boxes from that diagram, without the
gateway in between yet. The natural next step is a translation layer that:

- terminates auth once (real OAuth 2.1 token) and re-issues short-lived,
  scoped credentials to whichever protocol handles the downstream call
- normalizes the two failure shapes (`isError` tool result vs. `failed` task
  state) into one error taxonomy for unified logging
- decides, and documents, whether MCP tool manifests are exposed with the
  same "public by default" posture as A2A Agent Cards, per the asymmetry
  above

## 6. How to reproduce

```
cd mcp-a2a-demo
python3 run_comparison.py   # runs MCP demo, A2A demo, and the gateway demo
```

Outputs land in `results/`: `mcp_run.json`, `a2a_run.json`,
`gateway_run.json`, `comparison.json`.
