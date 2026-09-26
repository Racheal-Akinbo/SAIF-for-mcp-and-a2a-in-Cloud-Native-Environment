# 5. Evaluation

## 5.1 Experimental setup

All results below were produced by the testbed described in Section 4
(`mcp_app/`, `a2a_app/`, `gateway/`): an MCP server exposing a single tool
(`get_order_status`) over streamable HTTP via the official `mcp` SDK
(v2.1.1), and a hand-built A2A agent exposing the equivalent capability as a
skill, discoverable via an Agent Card and invoked over JSON-RPC 2.0
`message/send`. Both backends share the same mock data source and the same
bearer-token auth gate, isolating the comparison to protocol-level behavior
rather than implementation-specific auth strength. All components ran
same-host (127.0.0.1) with no simulated network latency, so the reported
timings isolate *protocol overhead*, not network conditions — a threat to
validity discussed in Section 5.5.

Five independent runs of three scenarios were executed per protocol:
`happy_path` (valid token, valid order), `no_auth` (token omitted), and
`bad_order` (valid token, nonexistent order ID). A sixth condition —
replay of a captured valid request — was run once against the raw A2A
backend and once against the gateway, per Section 5.4.

## 5.2 Functional correctness

| Scenario | MCP | A2A |
|---|---|---|
| `happy_path` | `success`, correct order data returned | `success`, correct order data returned |
| `no_auth` | Rejected pre-tool-execution (401 at transport) | Rejected pre-dispatch (401, JSON-RPC error envelope) |
| `bad_order` | Tool result flagged `is_error`, no crash | Task state `failed`, no crash |

Both implementations were functionally correct and fail-closed across all
five runs — no run produced a false-accept on the `no_auth` case, and no run
crashed or hung on the `bad_order` case. This establishes a baseline: any
performance or security difference reported below is not attributable to
one implementation being simply broken relative to the other.

## 5.3 Performance

| Metric | MCP (n=5) | A2A (n=5) |
|---|---|---|
| `happy_path` total latency — min / median / max (ms) | 40.1 / 44.0 / 77.1 | 23.0 / 23.8 / 28.4 |
| `no_auth` total latency — min / median / max (ms) | 22.0 / 23.9 / 29.0 | 21.8 / 24.3 / 27.4 |
| `bad_order` total latency — min / median / max (ms) | 38.7 / 40.7 / 61.5 | 21.9 / 22.7 / 27.1 |
| `happy_path` response payload (bytes) | 172 | 290 |
| `bad_order` response payload (bytes) | 102 | 182 |

Two patterns hold across all five runs:

**MCP's authenticated calls are consistently and substantially slower than
A2A's for the same task** — roughly 1.6–2x on `happy_path` and `bad_order`
(both of which reach the tool/skill layer), while the two protocols are
statistically indistinguishable on `no_auth`, where neither reaches the
application layer at all. This localizes the overhead specifically to
**post-authentication protocol mechanics**, not to the auth check itself.

**The gap traces to session establishment, not to the tool call itself.**
Per-phase instrumentation in the MCP client shows the actual `call_tool`
invocation completes in single-digit milliseconds; the remainder is
consumed by MCP's streamable-HTTP session lifecycle — session
initialization, an SSE stream open, and (in this implementation) an
explicit termination — before and after that call. A2A's `message/send` is
a single POST once the Agent Card has been fetched. This is a legitimate
architectural tradeoff rather than an implementation defect: MCP's session
model exists to support interaction patterns A2A's single-shot task model
does not — server-initiated sampling, subscriptions, multi-turn
elicitation — and this evaluation's task (one stateless lookup) is the case
least likely to show that investment paying off. A fairer performance
comparison for a multi-turn workload is noted as future work in 5.5.

MCP's smaller payload (172 vs. 290 bytes) reflects A2A's more verbose
task/artifact wrapper (task ID, status object, artifact array) around the
same underlying data — a structural cost of A2A's richer task-lifecycle
model, mirroring the latency finding in the opposite direction.

## 5.4 Security evaluation

### 5.4.1 Attack tree

| # | Asset | Attack | Precondition | Target | Result (raw backend) | Result (via gateway) |
|---|---|---|---|---|---|---|
| A1 | Tool/skill invocation | Call without credential | Network access to backend | Both | **Blocked** — 401 before app logic runs (verified, 5 runs each) | N/A (gateway sits in front) |
| A2 | Tool/skill invocation | Replay of a captured, validly-signed request | Attacker has intercepted one valid request | A2A (tested) | **Not blocked** — identical request accepted twice with identical `200 completed` result (Section 4, confirmed by direct test) | **Blocked** — second use of the same `X-Request-Id` returns `409 replay_detected` (confirmed, `gateway/run_demo.py`) |
| A3 | Backend credential | Caller obtains/leaks their own auth token, tries to reuse it elsewhere | Caller has valid credential for *one* surface | Gateway | N/A | **Contained** — caller holds `GATEWAY_TOKEN`, structurally distinct from `BACKEND_TOKEN`; the backend credential never transits caller-facing traffic (verified: gateway process never returns `BACKEND_TOKEN` in any response) |
| A4 | Discovery surface | Enumerate available capabilities pre-auth | Network access | A2A (by design), MCP (as configured) | A2A Agent Card is intentionally public (no credential exposed in it); MCP tool list in this implementation requires an initialized session, so effectively gated | Not yet mediated — gateway does not currently normalize this asymmetry (see 5.5) |
| A5 | Malformed input | Send well-formed-but-invalid resource identifier | Valid credential | Both | **Contained** — structured error, no crash, no stack trace exposed (verified, 5 runs each) | Passed through as unified `{error: true, code: "not_found", ...}` |

Rows A1, A2, A3, and A5 were empirically exercised against the running
testbed, not inferred from the specifications; A4 is a design-level
observation confirmed by inspecting both implementations' discovery
endpoints.

### 5.4.2 Discussion

The single most load-bearing result in this evaluation is A2: **neither
protocol implementation provides replay resistance on its own**, and this
was demonstrated rather than assumed — the same captured request, replayed
byte-for-byte, was honored twice by the raw A2A backend. This matters
because both MCP's and A2A's authentication models (bearer tokens / OAuth
2.1 access tokens) are, by design, valid for their full lifetime once
issued; neither protocol's base message format mandates a nonce or
timestamp binding a message to a single use. A stolen or intercepted
request is therefore replayable for as long as its token remains valid, in
either protocol, unless something above the protocol layer adds that
control. Row A2's "result via gateway" column is this paper's proposed
mitigation, exercised against the same attack that broke the raw backend.

Row A4 is flagged rather than resolved: A2A's discovery-is-public design and
MCP's discovery-is-gated implementation are both individually defensible,
but a framework mediating both needs to pick one posture deliberately. This
evaluation's gateway does not yet make that choice — an acknowledged gap,
not an oversight, addressed in Section 6 (future work).

## 5.5 Threats to validity

- **Same-host deployment.** All latency figures exclude real network
  transit; the *relative* gap between MCP and A2A should persist under
  network latency (both pay it equally), but the *absolute* overhead
  percentage attributable to session establishment will shrink as
  round-trip network cost grows and comes to dominate.
- **Single-call task.** The chosen task (one stateless lookup) is close to
  a worst case for MCP's session-based overhead and a best case for A2A's
  single-shot model. A multi-turn task exercising MCP's subscription or
  sampling features, or an A2A task that pauses in `input-required` state,
  would likely narrow or reverse parts of this gap — this is noted as a
  concrete next experiment rather than claimed here.
- **Shared-secret auth instead of full OAuth 2.1.** Both backends use a
  static bearer token rather than the OAuth 2.1 flows both specifications
  formally support, in order to isolate protocol-level behavior from
  authorization-server configuration. Absolute latency figures would shift
  under full OAuth (token introspection round trips); the qualitative
  finding — MCP's extra session-lifecycle round trips vs. A2A's single
  request — is independent of the auth mechanism used.
- **A2A implemented against the wire spec, not the reference SDK.** The
  hand-built A2A server may not perfectly replicate every behavior of a
  production `a2a-sdk` deployment (e.g., its task-store/push-notification
  paths). The measured behavior reflects protocol-conformant, spec-level
  A2A, not `a2a-sdk`-specific implementation choices.
- **n=5 runs.** Sufficient to establish that the MCP/A2A latency gap is
  systematic rather than noise (the ranges do not overlap for `happy_path`
  and `bad_order`), but too small to support a formal statistical claim
  (e.g., a confidence interval on the exact multiplier). A larger-n run is
  planned for the camera-ready version.

## 5.6 Summary

The evaluation supports three claims made in Section 3 (framework design):
(1) both protocols correctly fail closed on missing credentials and handle
malformed input without crashing, so neither introduces a baseline
robustness gap; (2) MCP's richer session model carries a real, repeatable
latency cost relative to A2A's single-shot model for tasks that don't need
that richness — an architectural tradeoff, not a defect, that framework
designers should account for when choosing which protocol to expose for a
given interaction pattern; and (3) the proposed interoperability gateway
closes a genuine, empirically-confirmed security gap (replay) present in
both protocols' base implementations, which is the central justification
for a mediation layer beyond mere format translation.
