"""覆盖失效快照、真实工具参数兼容与训练数据分区。"""

import json

from lab_agent import STRATEGIES, normalize_action, run_case
from lab_environment import cases
from run_lab import export


def test_replanning_replaces_stale_quote_and_keeps_current_evidence():
    case = next(case for case in cases() if case.id == "stock-5-0")
    baseline, full, compact = [run_case(case, strategy) for strategy in STRATEGIES]
    assert baseline["stop"] == "stale_quote"
    assert baseline["receipt"] is None
    assert compact["verdict"]["passed"] and full["verdict"]["passed"]
    assert compact["receipt"] == full["receipt"]
    assert compact["receipt"]["revision"] == 2
    assert "NB-F" in compact["receipt"]["skus"]
    fail_step = next(step for step in compact["steps"] if step["replanned"])
    assert fail_step["observation"]["code"] == "stale_quote"
    assert "read:laptop" in fail_step["plan_after"]
    assert sum(s["context_bytes"] for s in compact["steps"]) < sum(
        s["context_bytes"] for s in full["steps"]
    )


def test_valid_hold_and_curated_export(tmp_path):
    selected = [case for case in cases() if case.family == "impossible"]
    runs = [run_case(case, strategy) for case in selected for strategy in STRATEGIES]
    report = export(runs, tmp_path, write_candidates=True)
    assert sum(report["candidate_count"].values()) == len(selected)
    assert sum(not run["verdict"]["passed"] for run in runs) == len(selected)
    groups = []
    for split in ("train", "dev", "test"):
        rows = [
            json.loads(line)
            for line in (tmp_path / f"candidates-{split}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert all(row["receipt"]["status"] == "hold" and row["split"] == split for row in rows)
        groups.append({row["group"] for row in rows})
    assert not (groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2])


def test_real_model_catalog_argument_envelope():
    assert normalize_action({"tool": "read_catalog", "argument": {"category": "laptop"}}) == {
        "tool": "read_catalog",
        "argument": "laptop",
    }
    invalid = {"category": "laptop", "other": True}
    assert normalize_action({"tool": "read_catalog", "argument": invalid})["argument"] == invalid
