# Live System Validation: Interactive Windows Deployment Testing

## 1. Purpose and scope

The evaluation reported in `report.md` and `evaluation_section.md` was produced
by an automated batch harness (`run_comparison.py`) executing on a Linux
sandbox, with all three services (MCP, A2A, gateway) running inside a single
process for tight control. This document reports a **second, independent
validation pass**: a manual, interactive test session conducted by the
end user on a separate Windows 10 machine, driving the system through the
gateway's browser dashboard while three standalone terminal processes
(MCP server, A2A server, gateway) ran concurrently.

This is methodologically valuable in its own right — it validates the system
under a genuinely different OS, network stack, and process topology
(three independent OS processes rather than one asyncio event loop), and
against a human operator clicking a UI rather than a scripted client. Running
the same claims through a second, structurally different environment is a
basic form of replication, and — as shown below — it surfaced a real,
reportable discrepancy that the single-environment automated run could not
have exposed.

## 2. Environment

| | Automated benchmark (prior) | Live validation (this report) |
|---|---|---|
| OS | Linux (sandboxed container) | Windows 10 (build 10.0.26200.8655) |
| Process topology | All 3 services in one Python process (asyncio tasks) | 3 independent OS processes, 3 terminal windows |
| Driver | Scripted client (`run_comparison.py`) | Human operator via browser dashboard (`http://127.0.0.1:8000/`) |
| Sample size | n = 5 automated runs × 3 scenarios | n = 18 manually-issued requests across one continuous session |
| Date | — | 05 September 2026, 10:16–10:34 local time |

## 3. Full transaction log

All 18 gateway-mediated transactions from the session, reconstructed
chronologically from the dashboard's running log and cross-checked against
the corresponding lines in the MCP-server, A2A-server, and gateway terminal
outputs. "Round-trip" is the browser's measured time; "gateway-internal" is
the `gateway_latency_ms` value the server itself reported for the same call
(the small gap between the two is browser/network overhead on top of the
server's own processing).

| # | Time | Backend | Order | Outcome | Round-trip (ms) | Gateway-internal (ms) |
|---|---|---|---|---|---|---|
| 1 | 10:16:10 | MCP | ORD-1001 | 200 success | 2966.9 | 2952.86 |
| 2 | 10:17:00 | MCP | ORD-1002 | 200 success | 93.3 | 80.56 |
| 3 | 10:20:48 | MCP | ORD-1003 | 200 success | 118.0 | 83.99 |
| 4 | 10:21:34 | MCP | ORD-9999 | 404 not found | 125.7 | 103.88 |
| 5 | 10:29:07 | A2A | ORD-1001 | 200 success | 1362.2 | 1340.68 |
| 6 | 10:29:55 | A2A | ORD-1002 | 200 success | 1446.9 | 1433.65 |
| 7 | 10:30:44 | A2A | ORD-1003 | 200 success | 1217.8 | 1205.75 |
| 8 | 10:31:19 | A2A | ORD-9999 | 404 not found | 1177.0 | 1162.81 |
| 9 | 10:31:49 | — | (replay click) | — | — | — |
| 10 | 10:31:50 | A2A | ORD-9999 (replayed) | **409 blocked** | 34.7 | — |
| 11 | 10:32:06 | A2A | ORD-1002 | 200 success | 1237.7 | 1215.2 |
| 12 | 10:32:08 | — | (replay click) | — | — | — |
| 13 | 10:32:08 | A2A | ORD-1002 (replayed) | **409 blocked** | 19.8 | — |
| 14 | 10:32:45 | MCP | ORD-1002 | 200 success | 98.9 | 74.15 |
| 15 | 10:32:48 | — | (replay click) | — | — | — |
| 16 | 10:32:48 | MCP | ORD-1002 (replayed) | **409 blocked** | 20.4 | — |
| 17 | 10:33:23 | MCP | ORD-1002 (wrong token) | **401 rejected** | 33.9 | — |
| 18 | 10:34:24 | A2A | ORD-1002 (wrong token) | **401 rejected** | 33.3 | — |

## 4. Functional correctness

Every one of the 18 transactions produced the expected outcome with no
exceptions escaping to the client and no crashes in any of the three
processes:

- 8/8 legitimate happy-path and bad-order requests returned the correct
  data or the correct `404 not_found` shape.
- 3/3 replay attempts were blocked with `409 replay_detected`.
- 2/2 wrong-token attempts were blocked with `401 unauthorized`.

Terminal-log cross-checks confirm the gateway's access-control logic
executed exactly as designed, not just that the HTTP status codes looked
right:

- For both wrong-token attempts, the gateway terminal logged
  `[gateway] !! rejected: invalid gateway token (backend never contacted)`,
  and neither the MCP-server nor the A2A-server terminal produced a
  corresponding `>> tool called` / `>> skill invoked` line at that
  timestamp — i.e. the backend genuinely was never reached, not merely
  that the error was cosmetic.
- For all three replay attempts, the gateway terminal logged
  `[gateway] !! REPLAY DETECTED: request_id=... was already processed --
  blocking`, again with no matching backend log line, and the response
  times (19.8–34.7 ms) are an order of magnitude faster than any real
  backend round trip in this session — strong indirect evidence the
  request was rejected before any network call to the backend, not after.

This corroborates the automated benchmark's finding that both the
credential check and the replay check are enforced at the gateway, prior to
dispatch, using a second, independent implementation topology (separate
processes, not shared event loop).

## 5. Performance analysis — an unexpected, reportable reversal

The automated Linux benchmark's headline performance finding was that MCP
was consistently *slower* than A2A (roughly 1.6–2x) because of MCP's
multi-exchange session handshake versus A2A's single POST. The live
Windows session shows the **opposite pattern**, and it is worth reporting
precisely rather than silently reconciling it with the earlier result.

**Observation 1 — MCP pays a one-time cost, then stays fast.** The very
first MCP call of the session (#1) took 2966.9 ms. Every subsequent MCP
call in the same gateway process lifetime — including one issued 16
minutes later (#14) — completed in 74–126 ms, a pattern consistent with a
one-time warm-up cost (e.g. first-socket connection setup, module
JIT/import effects, or OS-level route caching) that is paid once and then
amortized away.

**Observation 2 — A2A does not show the same amortization.** Every A2A
call in this session, from the first (#5, 1362.2 ms) to the last real one
three minutes later (#11, 1237.7 ms), took over 1.1 seconds. There is no
downward trend across the five real A2A calls — the last is not
meaningfully faster than the first. This rules out a simple one-time
process-startup explanation and points instead to a **per-call cost specific
to the A2A code path** in this environment.

**Most likely cause.** In `gateway/app.py`, `call_mcp()` and `call_a2a()`
each open a *fresh* HTTP client on every invocation (`async with
httpx2.AsyncClient(...)` and `async with httpx.AsyncClient(...)`
respectively) rather than reusing a persistent client across requests.
On Windows, socket setup/teardown for a new client — particularly the
IPv4/IPv6 dual-stack resolution behavior for `127.0.0.1` under the
`asyncio` proactor event loop — is known to add latency the Linux sandbox's
event loop did not exhibit. That both libraries create a fresh client per
call but only one (`httpx`, used for A2A) shows the sustained ~1.2 s cost
suggests the two HTTP client stacks (`httpx` vs. the `mcp` SDK's bundled
`httpx2`) resolve or pool loopback connections differently under Windows'
proactor loop — a plausible but not yet isolated explanation.

**This is flagged as an open finding, not a settled conclusion.** The
correct way to confirm it would be to instrument socket-level connect
timing directly, which this test session did not do. What can be stated
with confidence is the *empirical pattern* (Observations 1 and 2 above),
independently of its ultimate cause.

**Practical implication for the framework.** Regardless of root cause,
this is a legitimate engineering finding: **the gateway should hold a
single persistent HTTP client per backend, constructed once at startup,
rather than opening a new client per request.** This is a common
production pattern (connection pooling) that this prototype did not
implement, precisely because the earlier Linux benchmark's fast, in-process
event loop masked the cost that Windows' networking stack exposed. This is
recommended as a concrete next implementation step (Section 8).

## 6. Security validation — consistent with automated findings, now cross-platform

The two security properties claimed for the gateway were re-confirmed here
under a genuinely different OS and process model:

- **Credential isolation / fail-closed auth**: both a wrong-token MCP
  attempt and a wrong-token A2A attempt were rejected in 33–34 ms with no
  backend contact.
- **Replay resistance**: three independent replay attempts — two against
  A2A-originated request IDs and one against an MCP-originated request ID —
  were all correctly blocked, showing the replay cache is not backend-specific
  (the same `X-Request-Id` dedup logic protects both protocols uniformly, as
  designed).

No security property observed on Linux failed to reproduce on Windows. This
strengthens confidence that the security behavior is a property of the
gateway's logic, not an artifact of the original test environment.

## 7. A secondary finding: error-message verbosity asymmetry

Comparing the two backends' `404 not_found` responses for the identical
bad-order case reveals a difference not previously surfaced in the
automated report, because the earlier client-side parsing did not print the
raw message string for manual inspection:

- **A2A**: `"message": "'No such order: ORD-9999'"`
- **MCP**: `"message": "Error executing tool get_order_status"`

A2A surfaces the underlying `KeyError`'s text directly to the caller,
while MCP's SDK wraps any tool-level exception in a generic
`UnexpectedToolError` before it crosses the wire — the specific reason
(`No such order: ...`) is logged only locally, visible in the MCP-server
terminal's own traceback, and never reaches the client.

This is a genuine, if minor, security/usability trade-off between the two
protocols' default error-handling philosophies: MCP's default is more
conservative (less information disclosure, less debuggability for a
legitimate caller), while A2A's default is more transparent (easier to
debug, marginally more information available to a potential attacker
probing for valid order-ID formats). A framework unifying both should
decide, deliberately, which posture to standardize on — this was not
previously called out in the architecture's error-taxonomy unification
(Section 4 of `report.md`) and is added here as a refinement.

A related, smaller implementation detail: A2A's message string contains a
redundant pair of single quotes (`'No such order: ORD-9999'` rather than
`No such order: ORD-9999`). This is caused by `str(KeyError("..."))` in
Python returning the `repr()` of the exception's argument rather than the
bare string — a one-line fix (`str(exc.args[0])`) for a future revision,
noted here for completeness rather than as a functional problem.

## 8. Recommendations arising from this validation pass

1. ~~**Reuse HTTP clients in the gateway**~~ **Implemented.** `call_a2a()`
   now uses a single `httpx.AsyncClient` constructed once at gateway
   startup (FastAPI startup/shutdown events) instead of a fresh client per
   request. Re-verified functionally after the change (happy path, replay
   detection, and the quote fix below all re-tested and passing); a
   repeat of this same Windows session would be needed to confirm the
   ~1.2s-per-call A2A pattern is actually resolved, since the anomaly was
   Windows-specific and this fix was implemented and only re-tested on
   Linux.
2. **Normalize error-message verbosity** between the two backends'
   generated responses — not yet implemented, still open.
3. ~~**Fix the redundant-quote formatting**~~ **Implemented.** A2A's
   error handler now returns `str(exc.args[0])` instead of `str(exc)`;
   confirmed the message field no longer contains the stray
   `'...'` wrapping (`"No such order: ORD-9999"` rather than
   `"'No such order: ORD-9999'"`).
4. **Instrument socket-level connection timing** — still open, would
   require re-running this same session on the Windows machine with the
   client-pooling fix in place to see whether the fix actually closes the
   gap or whether the cause lies elsewhere.

## 8a. Note on scope: cloud-native coverage, updated

Both the automated benchmark and this live validation pass — on Linux and
on Windows respectively — ran the three services as plain local processes
communicating over `127.0.0.1`. Neither used containers, Kubernetes, or a
service mesh, despite the paper's title naming cloud-native environments
as part of its scope.

That gap is now partially closed, in two stages, by work described in the
top-level `README.md`:

1. **Containerization** (`Dockerfile` + `docker-compose.yml`): the three
   services run as separate containers communicating over real
   container-network DNS rather than shared localhost.
2. **Orchestration + infrastructure-layer security policy** (`k8s/`):
   Kubernetes Deployments and Services, plus NetworkPolicies that enforce
   — at the network layer, independent of the gateway's own application
   logic — that only the gateway pod may reach the `mcp` and `a2a` pods
   directly. `k8s/05-netpol-test-pod.yaml` provides a concrete test proving
   this is actually enforced, not just declared, analogous in spirit to
   the replay-attack proof in Section 6 of this report but at the
   infrastructure layer instead of the application layer.

**Neither stage has been executed and observed the way the Linux and
Windows sessions above were** — both are written, syntax-validated, and
ready to run, but running them and capturing results (pod status, the
NetworkPolicy enforcement test's actual output, dashboard behavior
through `kubectl port-forward`) is the natural next validation pass,
structurally identical to how the Windows session in this report validated
what the automated Linux benchmark could not reach.

**Still genuinely open**: service-mesh mTLS (Linkerd/Istio). The
Deployment manifests carry a commented annotation and `k8s/README.md`
documents the steps, but installing and verifying a mesh control plane
has not been attempted even in documentation form beyond pointing to
Linkerd's own quickstart. This remains the one piece of the original
architecture diagram not yet built in any form.

## 9. Threats to validity (this pass)

- **n = 1 session, single machine.** Unlike the automated benchmark's five
  repeated runs, this was one continuous manual session on one Windows
  machine. The consistent ~1.2 s A2A pattern across five separate calls
  within that session is suggestive but not statistically robust across
  machines or repeated cold starts.
- **Manual timing, not controlled load.** A human clicking a button
  introduces variable delay between requests (visible in the multi-minute
  gaps in the timestamp column) that a scripted harness would not have;
  this does not affect the *server-reported* latencies but is worth noting
  for reproducibility.
- **No independent confirmation of the dual-stack hypothesis.** As stated
  in Section 5, the proposed explanation for the MCP/A2A performance
  reversal is plausible given the evidence but not directly measured.

## 10. Conclusion

This live, cross-platform validation pass reproduced every security
finding from the automated benchmark (fail-closed auth, replay resistance,
graceful bad-input handling) using an independent process topology and
operator, which strengthens confidence in those results. It also
surfaced a genuine, previously-undetected performance discrepancy — MCP
and A2A traded places in relative speed between the Linux and Windows
environments — along with a smaller error-message verbosity asymmetry
between the two protocols' default behavior. Both are reported here as
concrete findings with recommended follow-up work, consistent with the
paper's broader argument that protocol-level claims about MCP and A2A
should be validated empirically and across environments rather than
assumed from specification alone.
