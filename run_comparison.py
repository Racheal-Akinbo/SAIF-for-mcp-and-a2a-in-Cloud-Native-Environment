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
