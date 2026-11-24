"""批量运行采购 Agent 实验、计算验收指标，并导出可回放轨迹与后训练候选。"""

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from time import perf_counter

import httpx

from lab_agent import STRATEGIES, LiveModel, run_case, serialize
from lab_environment import FAMILIES, cases

ROOT = Path(__file__).resolve().parent
SPLITS = ("train", "dev", "test")


def summarize(runs: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for run in runs:
        groups["all", run["strategy"]].append(run)
        groups[run["split"], run["strategy"]].append(run)
    metrics = []
    for (split, strategy), group in sorted(groups.items()):
        failure_cases = [run for run in group if run["scenario"] != "stable"]
        recovery_count = sum(
            run["verdict"]["passed"] and run["replans"] > 0 for run in failure_cases
        )
        metrics.append(
            {
                "split": split,
                "strategy": strategy,
                "episodes": len(group),
                "passed": sum(run["verdict"]["passed"] for run in group),
                "recovered": recovery_count,
                "failure_cases": len(failure_cases),
                "mean_actions": round(mean(len(run["steps"]) for run in group), 2),
                "context_bytes": sum(
                    step["context_bytes"] for run in group for step in run["steps"]
                ),
                "prompt_tokens": sum(
                    step["tokens"].get("prompt_tokens", 0) for run in group for step in run["steps"]
                ),
                "completion_tokens": sum(
                    step["tokens"].get("completion_tokens", 0)
                    for run in group
                    for step in run["steps"]
                ),
            }
        )
    return metrics


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def summarize_paired_tokens(runs: list[dict]) -> dict | None:
    """只用两种上下文都通过的真实模型案例比较 token，排除提前终止的影响。"""
    cases_by_model: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for run in runs:
        if run["engine"] != "offline-rule":
            cases_by_model[run["engine"], run["case_id"]][run["strategy"]] = run
    strategies = ("replan-full", "replan-compact")
    pairs = [group for group in cases_by_model.values() if all(s in group for s in strategies)]
    if not pairs:
        return None
    passed = [group for group in pairs if all(group[s]["verdict"]["passed"] for s in strategies)]
    tokens = {
        strategy: sum(
            step["tokens"].get("prompt_tokens", 0)
            for pair in passed
            for step in pair[strategy]["steps"]
        )
        for strategy in strategies
    }
    return {"pairs": len(pairs), "both_passed": len(passed), "prompt_tokens": tokens}


def export(runs: list[dict], destination: Path, *, write_candidates: bool) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    metrics = summarize(runs)
    modes = {run["engine"] for run in runs}
    groups: dict[str, str] = {}
    seen: set[str] = set()
    candidates: dict[str, list[dict]] = {split: [] for split in SPLITS}
    for run in runs:
        split = run["split"]
        if groups.setdefault(run["group"], split) != split:
            raise ValueError(f"采购配置组 {run['group']} 跨数据分区")
        if not write_candidates or not run["verdict"]["passed"]:
            continue
        # 验收通过才进入候选；相同任务的等价成功轨迹只保留一个版本。
        steps = [{"action": s["action"], "observation": s["observation"]} for s in run["steps"]]
        key = serialize([run["group"], run["request"], steps])
        if key in seen:
            continue
        seen.add(key)
        candidates[split].append(
            {
                "schema_version": 1,
                "case_id": run["case_id"],
                "group": run["group"],
                "split": split,
                "provenance": run["engine"],
                "request": run["request"],
                "steps": steps,
                "receipt": run["receipt"],
                "reward": 1,
            }
        )

    with (destination / "trajectories.jsonl").open("w", encoding="utf-8") as out:
        for run in runs:
            out.write(
                serialize({"schema_version": 1, **run, "reward": int(run["verdict"]["passed"])})
                + "\n"
            )
    if write_candidates:
        for split, rows in candidates.items():
            with (destination / f"candidates-{split}.jsonl").open("w", encoding="utf-8") as out:
                for row in rows:
                    out.write(serialize(row) + "\n")

    example_runs = {run["case_id"]: run["scenario"] for run in runs if run["split"] == "test"}
    sample_ids = {
        next((case_id for case_id, family in example_runs.items() if family == f), None)
        for f in FAMILIES
    }
    report = {
        "engines": sorted(modes),
        "metrics": metrics,
        "candidate_count": {split: len(rows) for split, rows in candidates.items()},
        "replays": [run for run in runs if run["case_id"] in sample_ids],
    }
    paired = summarize_paired_tokens(runs)
    if paired is not None:
        report["paired_tokens"] = paired
    write_json(destination / "summary.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="使用真实模型；必须显式给出案例 ID")
    parser.add_argument("--case", action="append", default=[], help="例如 stock-5-0，可重复")
    parser.add_argument("--strategy", choices=[s.id for s in STRATEGIES], action="append")
    parser.add_argument("--list", action="store_true", help="列出任务编号和数据分区")
    parser.add_argument("--out", type=Path, help="输出目录")
    args = parser.parse_args()
    task_set = cases()
    if args.list:
        for case in task_set:
            print(case.id, case.split, case.group)
        return
    if args.live and not args.case:
        parser.error("--live requires --case so the number of API calls is explicit")
    if set(args.case) - {case.id for case in task_set}:
        parser.error("unknown case ID; use --list")
    selected = [case for case in task_set if not args.case or case.id in args.case]
    strategies = [s for s in STRATEGIES if not args.strategy or s.id in args.strategy]
    complete_benchmark = not args.live and not args.case and not args.strategy
    default_dir = "lab-live" if args.live else "lab" if complete_benchmark else "lab-selection"
    destination = args.out or ROOT / "artifacts" / default_dir
    runs = []
    with httpx.Client(timeout=25) as client:
        model = LiveModel(client) if args.live else None
        for case in selected:
            for strategy in strategies:
                started = perf_counter()
                run = run_case(case, strategy, model)
                if args.live:
                    run["elapsed_seconds"] = round(perf_counter() - started, 3)
                    print(case.id, strategy.id, run["verdict"]["reason"], flush=True)
                runs.append(run)
    report = export(runs, destination, write_candidates=complete_benchmark)
    for row in report["metrics"]:
        if row["split"] == "all":
            print(
                f"{row['strategy']}: {row['passed']}/{row['episodes']}, "
                f"mean actions {row['mean_actions']}, input bytes {row['context_bytes']}"
            )
    print(f"Output: {destination}")


if __name__ == "__main__":
    main()
