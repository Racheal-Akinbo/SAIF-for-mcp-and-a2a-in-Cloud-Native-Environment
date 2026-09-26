# MCP vs A2A comparison demo

Runnable testbed implementing the same task ("look up an order's status")
once over MCP and once over A2A, with matched auth and error scenarios.
See `report.md` for the full write-up and findings.

## Setup

```
python3 -m venv venv
source venv/bin/activate   # on Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run (without Docker)

```
python3 run_comparison.py
```

This runs both protocol demos plus the gateway demo, and writes:
- `results/mcp_run.json`
- `results/a2a_run.json`
- `results/gateway_run.json`
- `results/comparison.json`

## Run with Docker (containerized, real inter-container networking)

Everything above runs as plain local processes talking over `127.0.0.1`.
To test the system the way it would actually be deployed — as separate
containers on a real container network, not shared localhost — use Docker
Compose instead:

```
docker compose up --build
```

This builds one shared image (`Dockerfile`) and starts three containers
(`mcp`, `a2a`, `gateway`) on their own bridge network
(`docker-compose.yml`). The gateway reaches the other two by their
container DNS names (`http://mcp:8001`, `http://a2a:8002`), not
`127.0.0.1` — genuine inter-container networking, unlike every other test
in this project. Once it's up, the dashboard works exactly the same:

```
http://127.0.0.1:8000/
```

```
docker compose down
```
stops and removes the containers.

**Requires Docker Desktop** (or Docker Engine on Linux) — no Kubernetes
needed for this step. This proves containerization and real
inter-container networking; it does **not** prove orchestration or
infrastructure-level security policy enforcement — see `k8s/README.md`
for the deployment that does.

## Run on Kubernetes (orchestration + infrastructure-level security policy)

`k8s/` contains Deployment, Service, and NetworkPolicy manifests for a
real (if local) Kubernetes deployment — the piece that actually
distinguishes "containerized" from "cloud-native." The NetworkPolicies
enforce, at the network layer, that only the gateway may reach the MCP
and A2A backends directly — the same trust boundary the gateway already
enforces in application logic, now also enforced by the platform.

See **`k8s/README.md`** for the full step-by-step guide, including a test
that proves the NetworkPolicy is actually blocking traffic rather than
just being declared. This has not been executed in this project's own
sandbox (no cluster available here) — it's written and YAML-validated,
and needs to be run and verified on your own machine, same as the Docker
step above.

## Layout

- `common/store.py` — shared mock order data and the auth token, used
  identically by both protocol implementations
- `mcp_app/` — MCP server (official `mcp` SDK, streamable HTTP) + client
- `a2a_app/` — A2A server (hand-built against the wire spec: Agent Card +
  JSON-RPC 2.0) + client
- `gateway/` — interoperability gateway mediating both backends: unified
  auth, unified error schema, and replay protection neither backend has
  on its own
- `run_comparison.py` — orchestrates all three and prints/saves the comparison
- `Dockerfile`, `docker-compose.yml` — containerized deployment (see above)
- `k8s/` — Kubernetes manifests + NetworkPolicy enforcement + deployment
  guide (see above) — orchestration and infrastructure-layer security
- `report.md` — findings, methodology notes, threat-model checks, and the
  gateway's replay-attack demonstration
- `evaluation_section.md` — paper-ready Evaluation section built from
  repeated automated runs
- `live_validation/live_validation_report.md` — a second, independent
  validation pass run manually on Windows, including a cross-platform
  performance discrepancy this uncovered and the fix that followed from it
