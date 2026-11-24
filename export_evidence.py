"""从正在运行的本机 Demo 导出模拟任务的实际验证材料。"""

import json
import os
from pathlib import Path

import httpx


def main() -> None:
    output = Path("artifacts")
    output.mkdir(exist_ok=True)
    with httpx.Client(base_url="http://127.0.0.1:8765", timeout=15) as client:
        response = client.post(
            "/api/login",
            json={"account": "chen", "password": os.getenv("DEMO_PASSWORD", "demo-2026")},
        )
        response.raise_for_status()
        response = client.get("/api/runs")
        response.raise_for_status()
        completed = next(
            run for run in response.json() if run["mode"] == "live" and run["status"] == "completed"
        )
        response = client.get(f"/api/runs/{completed['id']}")
        response.raise_for_status()
        run = response.json()
        assert run["order"]["total"] == run["proposal"]["total"]
        tool_names = [event["title"] for event in run["events"] if event["kind"] == "tool"]
        assert {"search_policy", "compare_catalog", "check_budget", "propose_purchase"}.issubset(
            tool_names
        )
        (output / "verified-live-run.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        response = client.get(f"/api/runs/{completed['id']}/report")
        response.raise_for_status()
        (output / "采购审计报告.md").write_text(response.text, encoding="utf-8")
        print(
            json.dumps(
                {
                    "status": run["status"],
                    "mode": run["mode"],
                    "tools": tool_names,
                    "checkpoints": run["checkpoints"],
                    "order_total": run["order"]["total"],
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
