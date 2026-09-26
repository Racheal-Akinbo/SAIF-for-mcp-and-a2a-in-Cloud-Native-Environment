<#
.SYNOPSIS
  One-shot automated smoke test for the Kubernetes deployment of the
  MCP/A2A security gateway project.

.DESCRIPTION
  This has NOT been executed by Claude -- there is no Windows machine,
  PowerShell, kubectl, or Kubernetes cluster available in the sandbox this
  project was built in. It has been written carefully, defensively, and to
  match kubectl/PowerShell syntax exactly, but you are the first one to
  actually run it. If something breaks, paste the exact error and we'll
  debug it together the same way we fixed the earlier "No module named
  'mcp'" issue.

.NOTES
  Run from the project root (the folder containing Dockerfile and k8s\).
  If PowerShell refuses to run this script at all, first run:
      Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
  in the same window, then re-run this script.
#>

$ErrorActionPreference = "Continue"
$results = @()
$portForwardProcess = $null

function Write-Section($title) {
    Write-Host ""
    Write-Host "=== $title ===" -ForegroundColor Cyan
}

function Write-Result($name, $pass, $detail) {
    $script:results += [PSCustomObject]@{ Test = $name; Pass = $pass; Detail = $detail }
    if ($pass) {
        Write-Host "  [PASS] $name -- $detail" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] $name -- $detail" -ForegroundColor Red
    }
}

function Invoke-GatewayRequest($OrderId, $Backend, $Token, $RequestId) {
    # Wraps Invoke-WebRequest so both success and error responses (4xx/5xx)
    # come back as a plain object instead of a thrown exception -- needed
    # because Windows PowerShell 5.1 throws on non-2xx by default.
    $uri = "http://127.0.0.1:8000/order-status"
    $headers = @{ "Authorization" = "Bearer $Token"; "X-Request-Id" = $RequestId }
    $bodyJson = (@{ order_id = $OrderId; backend = $Backend } | ConvertTo-Json -Compress)
    try {
        $resp = Invoke-WebRequest -Uri $uri -Method Post -Headers $headers `
            -ContentType "application/json" -Body $bodyJson -UseBasicParsing -TimeoutSec 10
        return [PSCustomObject]@{ StatusCode = [int]$resp.StatusCode; Body = $resp.Content }
    } catch {
        $errResp = $_.Exception.Response
        if ($errResp) {
            $statusCode = [int]$errResp.StatusCode
            try {
                $stream = $errResp.GetResponseStream()
                $reader = New-Object System.IO.StreamReader($stream)
                $bodyText = $reader.ReadToEnd()
            } catch { $bodyText = "(could not read error body)" }
            return [PSCustomObject]@{ StatusCode = $statusCode; Body = $bodyText }
        } else {
            return [PSCustomObject]@{ StatusCode = -1; Body = $_.Exception.Message }
        }
    }
}

# --- 0. Prerequisite checks -------------------------------------------------
Write-Section "Prerequisite checks"

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    Write-Host "kubectl not found on PATH. Install it or enable Kubernetes in Docker Desktop." -ForegroundColor Red
    exit 1
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "docker not found on PATH. Install Docker Desktop first." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path ".\Dockerfile") -or -not (Test-Path ".\k8s")) {
    Write-Host "Run this script from the project root (folder containing Dockerfile and k8s\)." -ForegroundColor Red
    exit 1
}

$context = (kubectl config current-context) 2>&1
Write-Host "Current kubectl context: $context"
if ($context -notmatch "docker-desktop" -and $context -notmatch "minikube") {
    Write-Host "WARNING: context is neither docker-desktop nor minikube. Proceeding anyway, but double-check you're not pointed at a real/shared cluster." -ForegroundColor Yellow
}

# --- 1. Build and load the image --------------------------------------------
Write-Section "Building image"
docker build -t mcp-a2a-demo:local .
if ($LASTEXITCODE -ne 0) {
    Write-Host "docker build failed -- see output above." -ForegroundColor Red
    exit 1
}

if ($context -match "minikube") {
    Write-Section "Loading image into minikube"
    minikube image load mcp-a2a-demo:local
    if ($LASTEXITCODE -ne 0) {
        Write-Host "minikube image load failed -- see output above." -ForegroundColor Red
        exit 1
    }
}

# --- 2. Apply manifests ------------------------------------------------------
Write-Section "Applying manifests"
$manifests = @(
    "k8s\00-namespace.yaml",
    "k8s\01-mcp.yaml",
    "k8s\02-a2a.yaml",
    "k8s\03-gateway.yaml",
    "k8s\04-network-policies.yaml"
)
foreach ($m in $manifests) {
    kubectl apply -f $m
    if ($LASTEXITCODE -ne 0) {
        Write-Host "kubectl apply -f $m failed -- see output above." -ForegroundColor Red
        exit 1
    }
}

# --- 3. Wait for readiness ---------------------------------------------------
Write-Section "Waiting for pods to become ready (up to 120s each)"
foreach ($app in @("mcp", "a2a", "gateway")) {
    kubectl wait --for=condition=Ready pod -l "app=$app" -n mcp-a2a-demo --timeout=120s
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Pod for '$app' did not become ready in time. Diagnostics:" -ForegroundColor Red
        kubectl get pods -n mcp-a2a-demo
        kubectl describe pod -l "app=$app" -n mcp-a2a-demo
        kubectl logs -l "app=$app" -n mcp-a2a-demo --tail=50
        exit 1
    }
}
Write-Host "All three pods are Ready." -ForegroundColor Green
kubectl get pods -n mcp-a2a-demo

# --- 4. Port-forward the gateway ---------------------------------------------
Write-Section "Starting port-forward to the gateway (background process)"
$portForwardProcess = Start-Process -FilePath "kubectl" `
    -ArgumentList "port-forward -n mcp-a2a-demo svc/gateway 8000:8000" `
    -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 3
Write-Host "Port-forward PID: $($portForwardProcess.Id)"

# --- 5. Functional + security test suite (mirrors the rest of this project) -
Write-Section "Running test suite against http://127.0.0.1:8000/"

$r = Invoke-GatewayRequest -OrderId "ORD-1001" -Backend "mcp" -Token "gateway-issued-token" -RequestId ([guid]::NewGuid().ToString())
Write-Result "MCP happy path" ($r.StatusCode -eq 200) "HTTP $($r.StatusCode)"

$r = Invoke-GatewayRequest -OrderId "ORD-1002" -Backend "a2a" -Token "gateway-issued-token" -RequestId ([guid]::NewGuid().ToString())
Write-Result "A2A happy path" ($r.StatusCode -eq 200) "HTTP $($r.StatusCode)"

$r = Invoke-GatewayRequest -OrderId "ORD-9999" -Backend "mcp" -Token "gateway-issued-token" -RequestId ([guid]::NewGuid().ToString())
Write-Result "MCP bad order -> 404" ($r.StatusCode -eq 404) "HTTP $($r.StatusCode)"

$r = Invoke-GatewayRequest -OrderId "ORD-1001" -Backend "mcp" -Token "wrong-token" -RequestId ([guid]::NewGuid().ToString())
Write-Result "Wrong token -> 401" ($r.StatusCode -eq 401) "HTTP $($r.StatusCode)"

$replayId = [guid]::NewGuid().ToString()
$r1 = Invoke-GatewayRequest -OrderId "ORD-1003" -Backend "a2a" -Token "gateway-issued-token" -RequestId $replayId
$r2 = Invoke-GatewayRequest -OrderId "ORD-1003" -Backend "a2a" -Token "gateway-issued-token" -RequestId $replayId
Write-Result "Replay: first attempt succeeds" ($r1.StatusCode -eq 200) "HTTP $($r1.StatusCode)"
Write-Result "Replay: second attempt blocked -> 409" ($r2.StatusCode -eq 409) "HTTP $($r2.StatusCode)"

# --- 6. NetworkPolicy enforcement test ---------------------------------------
Write-Section "Testing NetworkPolicy enforcement (infrastructure-layer security)"
kubectl apply -f k8s\05-netpol-test-pod.yaml
kubectl wait --for=condition=Ready pod/netpol-test -n mcp-a2a-demo --timeout=60s
if ($LASTEXITCODE -ne 0) {
    Write-Result "NetworkPolicy test pod ready" $false "pod did not become ready -- see kubectl describe pod/netpol-test -n mcp-a2a-demo"
} else {
    $directCall = kubectl exec -n mcp-a2a-demo netpol-test -- curl -sm 5 -o /dev/null -w "%{http_code}" http://mcp.mcp-a2a-demo.svc.cluster.local:8001/mcp 2>&1
    $curlExit = $LASTEXITCODE
    if ($curlExit -ne 0) {
        Write-Result "Direct pod-to-mcp access blocked" $true "curl timed out/failed (exit $curlExit) -- NetworkPolicy is enforced"
    } else {
        Write-Result "Direct pod-to-mcp access blocked" $false "curl got a response ($directCall) -- NetworkPolicy is NOT being enforced by this cluster's CNI. See k8s/README.md Step 5 (Docker Desktop does not enforce NetworkPolicy by default)."
    }

    # Deliberately a plain GET to the dashboard route (no body, no extra
    # headers) rather than a POST with a JSON body -- a POST here would
    # require quoting that survives three layers (PowerShell -> kubectl
    # exec -> the container's shell), which is fragile and unnecessary
    # just to prove the gateway is *reachable*.
    $gwCall = kubectl exec -n mcp-a2a-demo netpol-test -- curl -sm 5 -o /dev/null -w "%{http_code}" http://gateway.mcp-a2a-demo.svc.cluster.local:8000/ 2>&1
    Write-Result "Gateway still reachable from other pods" ($gwCall -eq "200") "HTTP $gwCall"
}

# --- 7. Summary ---------------------------------------------------------------
Write-Section "Summary"
$results | Format-Table -AutoSize
$failCount = ($results | Where-Object { -not $_.Pass }).Count
$results | ConvertTo-Json | Out-File -FilePath "k8s\test-results.json" -Encoding utf8
Write-Host ""
Write-Host "Results saved to k8s\test-results.json"
if ($failCount -eq 0) {
    Write-Host "ALL TESTS PASSED ($($results.Count)/$($results.Count))" -ForegroundColor Green
} else {
    Write-Host "$failCount of $($results.Count) TEST(S) FAILED -- see detail above" -ForegroundColor Red
}

# --- 8. Cleanup notice ---------------------------------------------------------
Write-Section "Cleanup"
Write-Host "Port-forward is still running in the background (PID $($portForwardProcess.Id))."
Write-Host "Stop it with:  Stop-Process -Id $($portForwardProcess.Id)"
Write-Host "Tear down everything with:  kubectl delete namespace mcp-a2a-demo"
