"""Run every provisioned dashboard panel query through Grafana's query API.

Template variables are expanded to match everything, and each query must
return data. Called by smoke.sh from the running test project:

    python3 tests/check_dashboards.py <compose-project> <grafana-user> <grafana-password>

Grafana publishes no port in the test stack, so requests go through
`docker compose exec grafana wget`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

DASHBOARDS = Path("config/grafana/provisioning/dashboards/json")
# Values that make each variable match every series
VARIABLES = {"${unit:regex}": ".*", "${unit}": "fridge-02"}


def query(project: str, auth: str, datasource: dict, flux: str) -> dict:
    body = {"from": "now-1h", "to": "now", "queries": [{"refId": "A", "datasource": datasource, "query": flux}]}
    result = subprocess.run(
        [
            "docker", "compose", "--project-name", project, "exec", "-T", "grafana",
            "wget", "-qO-", "--header", "Content-Type: application/json",
            "--post-data", json.dumps(body), f"http://{auth}@localhost:3000/api/ds/query",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {"error": result.stderr.strip() or "request failed"}
    return json.loads(result.stdout)["results"]["A"]


def main() -> int:
    project, user, password = sys.argv[1:4]
    failed = 0
    for path in sorted(DASHBOARDS.glob("*.json")):
        dashboard = json.loads(path.read_text())
        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                flux = target["query"]
                for name, value in VARIABLES.items():
                    flux = flux.replace(name, value)
                result = query(project, f"{user}:{password}", target["datasource"], flux)
                has_rows = any(f["data"]["values"] and f["data"]["values"][0] for f in result.get("frames", []))
                label = f'{dashboard["title"]} / {panel["title"]}'
                if "error" in result or not has_rows:
                    failed += 1
                    print(f"  FAIL panel {label}: {result.get('error', 'no data')}")
                else:
                    print(f"  ok   panel {label} returns data")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
