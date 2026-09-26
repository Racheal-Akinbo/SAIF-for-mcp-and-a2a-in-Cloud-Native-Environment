# Kubernetes deployment guide

This is the piece that actually earns the "cloud-native" claim in the
paper's title — Docker Compose (see top-level `README.md`) gave you
containerization and real inter-container networking, but not
orchestration or infrastructure-level security policy. This does.

**I have not run any of this myself** — this sandbox has no `kubectl`,
`docker`, `minikube`, or `pwsh`, and no cluster. Every command and the
automated script below have been written carefully and checked for
structural correctness (YAML parses, PowerShell braces/quotes balance),
but the actual apply-and-observe step is yours to run, the same way the
three-terminal Docker test was.

## Two ways to test this

**Option A — automated (`k8s/test-deployment.ps1`)**: one PowerShell
script that builds the image, applies every manifest, waits for
readiness, runs the same functional/security test suite used throughout
this project (happy path, bad order, wrong token, replay), and tests
NetworkPolicy enforcement — all in one run, with a pass/fail summary at
the end. Use this if you want a fast, repeatable smoke test.

**Option B — manual, step by step**: the walkthrough below. Use this if
the script fails and you want to isolate where, or if you'd rather see
and understand each step (the same way we went through the three-terminal
setup earlier).

They apply the exact same manifests — Option A is not a different or
lighter version of the test, just an automated run of the same steps.

## Prerequisites (both options)

- Docker Desktop with Kubernetes enabled (Settings → Kubernetes → Enable
  Kubernetes), **or** minikube
- `kubectl` (comes with Docker Desktop's Kubernetes; install separately
  for minikube)

Check you're pointed at the right cluster before doing anything:
```
kubectl config current-context
```
Should say `docker-desktop` or `minikube`.

## Option A — automated script

From the project root, in PowerShell:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\k8s\test-deployment.ps1
```

It will print a `[PASS]`/`[FAIL]` line for each test as it runs, then a
summary table, and save results to `k8s\test-results.json`. It leaves the
`kubectl port-forward` running in the background so you can also open
**http://127.0.0.1:8000/** in a browser afterward and click through the
dashboard manually — the script's own PID for that background process is
printed at the end so you can stop it with `Stop-Process -Id <pid>`.

**If it fails partway through**, the script prints diagnostics
(`kubectl describe`, `kubectl logs`) at the point of failure. Paste me
the exact output and we'll debug it the same way we fixed the earlier
`ModuleNotFoundError` issue — this script has not been run by anyone yet,
so a first-run bug is a real possibility, not a sign you did something
wrong.

**One known environment-dependent result to expect**: the NetworkPolicy
test may report `[FAIL]` on Docker Desktop even though nothing is
actually broken — see "Step 5" below for why, and what to do about it.

## Option B — manual, step by step

### Step 1 — build the image into the cluster's local image store

```
docker build -t mcp-a2a-demo:local .
```

**If using Docker Desktop's Kubernetes**: nothing further needed — it
shares Docker's image cache directly.

**If using minikube**: you must additionally load the image into
minikube's own image store:
```
minikube image load mcp-a2a-demo:local
```

## Step 2 — apply the manifests

```
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/01-mcp.yaml
kubectl apply -f k8s/02-a2a.yaml
kubectl apply -f k8s/03-gateway.yaml
kubectl apply -f k8s/04-network-policies.yaml
```

## Step 3 — check everything is running

```
kubectl get pods -n mcp-a2a-demo
```
Wait until all three show `Running` and `1/1` ready (may take a minute
for the readiness probes to pass). If a pod is stuck `ImagePullBackOff`,
the image didn't make it into the cluster — recheck Step 1.

```
kubectl logs -n mcp-a2a-demo deployment/mcp
kubectl logs -n mcp-a2a-demo deployment/a2a
kubectl logs -n mcp-a2a-demo deployment/gateway
```
You should see the same `[mcp-server] starting...` / `[a2a-server]
starting...` / `[gateway] starting...` lines you saw in the terminal and
Docker Compose runs — same code, third deployment substrate.

## Step 4 — reach the dashboard

```
kubectl port-forward -n mcp-a2a-demo svc/gateway 8000:8000
```
Leave this running, then open **http://127.0.0.1:8000/** — same
dashboard, same buttons, same replay-attack test as every prior test in
this project. This confirms the application logic works identically when
actually orchestrated by Kubernetes rather than run as bare processes or
plain containers.

## Step 5 — prove the NetworkPolicy is actually enforced

This is the step that distinguishes "we wrote a NetworkPolicy" from "the
NetworkPolicy actually does something." First, check whether your
cluster's CNI enforces NetworkPolicy at all:

- **Docker Desktop's Kubernetes**: does **not** enforce NetworkPolicy by
  default. If you're on Docker Desktop, the test below will likely show
  the request *succeeding* even though a policy exists — that's not a bug
  in the policy, it's a missing enforcement layer. Note this explicitly in
  the paper if so; it's a real, common limitation of local dev clusters.
- **minikube**: start it with a CNI that enforces policy:
  ```
  minikube start --cni=calico
  ```

Now run the actual test:
```
kubectl apply -f k8s/05-netpol-test-pod.yaml
kubectl exec -n mcp-a2a-demo -it netpol-test -- \
  curl -sm 5 -o /dev/null -w "HTTP %{http_code}\n" http://mcp.mcp-a2a-demo.svc.cluster.local:8001/mcp
```

**Interpreting the result:**
- **Times out / no response** → NetworkPolicy is enforced. The unlabeled
  pod's packets were dropped before ever reaching `mcp`. This is the
  infrastructure-layer equivalent of the replay-attack block from
  `live_validation_report.md` — a concrete, testable security control,
  not just a diagram.
- **Fast response with an HTTP status** → either NetworkPolicy isn't
  enforced by your CNI (see above), or the policy YAML has an error.
  Recheck `kubectl describe networkpolicy -n mcp-a2a-demo`.

For comparison, confirm the *gateway itself* still works from the same
test pod (it should — Policy 4 in `04-network-policies.yaml` allows this
deliberately):
```
kubectl exec -n mcp-a2a-demo -it netpol-test -- \
  curl -sm 5 -X POST http://gateway.mcp-a2a-demo.svc.cluster.local:8000/order-status \
  -H "Content-Type: application/json" -H "Authorization: Bearer gateway-issued-token" \
  -H "X-Request-Id: netpol-proof-1" -d '{"order_id": "ORD-1001", "backend": "mcp"}'
```
This should succeed — the gateway is meant to be reachable; only the
backends behind it are meant to be locked down.

## Step 6 — clean up

```
kubectl delete namespace mcp-a2a-demo
```
Deletes everything in one shot (all Deployments, Services, Policies, and
the test pod).

## Adding a service mesh for mTLS (further, optional step)

The Deployment YAMLs each have a commented-out annotation:
```yaml
# annotations:
#   linkerd.io/inject: enabled
```
Linkerd is recommended over Istio for this project specifically because
it gives automatic mTLS between meshed pods with no additional
`PeerAuthentication` CRDs or Istio-specific configuration required —
lower setup cost for a demo cluster. To use it:

1. Install the Linkerd CLI and control plane per Linkerd's own
   quickstart (this varies by version and is outside this project's
   scope to script reliably): https://linkerd.io/2/getting-started/
2. Uncomment the annotation in all three Deployment files
3. Re-apply: `kubectl apply -f k8s/01-mcp.yaml -f k8s/02-a2a.yaml -f k8s/03-gateway.yaml`
4. Verify mTLS is active: `linkerd viz edges -n mcp-a2a-demo deployment`
   should show `TLS: true` on the gateway→mcp and gateway→a2a edges.

This last step has not been attempted even by proxy — it's documented as
the honest remaining piece of "future work" for the full architecture
shown in the original diagram (gateway + K8s + service mesh mTLS).
