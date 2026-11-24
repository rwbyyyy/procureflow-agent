"""用沙盒验收奖励学习重新规划时的刷新策略，并在冻结策略后复验。"""

import random
from collections import defaultdict
from pathlib import Path
from statistics import mean

from lab_agent import READ_KEYS, STRATEGIES, run_case
from lab_environment import Case, cases
from run_lab import write_json

ARMS = {
    "all": READ_KEYS,
    "laptop": ("laptop",),
    "policy": ("policy",),
    "budget": ("budget",),
    "budget_and_laptop": ("budget", "laptop"),
}
SEED = 2026
EPOCHS = 5
STRATEGY = STRATEGIES[-1]
CHANGE_FAMILIES = {"stock", "deadline", "budget", "impossible"}


def reward(run: dict) -> float:
    """优先保证最优草案或正确暂缓，成功率相同才偏好较短轨迹。"""
    return (1.0 if run["verdict"]["passed"] else -1.0) - 0.03 * len(run["steps"])


def train(training: list[Case]) -> tuple[dict[str, str], list[dict]]:
    """对每个观测到的变更类别用 ε-greedy 探索，使用实际回报更新样本均值。"""
    rng = random.Random(SEED)
    samples: dict[str, dict[str, list[float]]] = defaultdict(lambda: {arm: [] for arm in ARMS})
    log = []

    def greedy(change: str) -> str:
        return max(
            (arm for arm in ARMS if samples[change][arm]),
            key=lambda arm: mean(samples[change][arm]),
            default="all",
        )

    for epoch in range(EPOCHS):
        order = training.copy()
        rng.shuffle(order)
        for case in order:
            choices: list[tuple[str, str]] = []

            def explore(change: str) -> tuple[str, ...]:
                untried = [arm for arm, values in samples[change].items() if not values]
                if untried:
                    arm = rng.choice(untried)
                else:
                    arm = rng.choice(list(ARMS)) if rng.random() < 0.2 else greedy(change)
                choices.append((change, arm))
                return ARMS[arm]

            run = run_case(case, STRATEGY, refresh_policy=explore)
            for change, arm in choices:
                value = reward(run)
                samples[change][arm].append(value)
                log.append(
                    {
                        "epoch": epoch + 1,
                        "case": case.id,
                        "context": change,
                        "action": arm,
                        "reward": round(value, 3),
                        "estimate": round(mean(samples[change][arm]), 3),
                    }
                )
    return {change: greedy(change) for change in sorted(samples)}, log


def evaluate(holdout: list[Case], policy: dict[str, str]) -> dict:
    def measured(selected: dict[str, str]) -> dict:
        runs = [
            run_case(
                case, STRATEGY, refresh_policy=lambda change: ARMS[selected.get(change, "all")]
            )
            for case in holdout
        ]
        return {
            "passed": sum(run["verdict"]["passed"] for run in runs),
            "episodes": len(runs),
            "mean_actions": round(mean(len(run["steps"]) for run in runs), 2),
            "mean_reward": round(mean(map(reward, runs)), 3),
        }

    return {"full_refresh": measured({}), "learned_refresh": measured(policy)}


def main() -> None:
    episodes = [case for case in cases() if case.family in CHANGE_FAMILIES]
    training = [case for case in episodes if case.split == "train"]
    holdout = [case for case in episodes if case.split == "test"]
    policy, log = train(training)
    measures = evaluate(holdout, policy)
    report = {
        "seed": SEED,
        "epochs": EPOCHS,
        "training_groups": sorted({c.group for c in training}),
        "holdout_groups": sorted({c.group for c in holdout}),
        "policy": policy,
        "holdout": measures,
        "updates": log,
    }
    output = Path(__file__).resolve().parent / "artifacts" / "lab"
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "policy-learning.json", report)
    for name, row in measures.items():
        print(name, row)


if __name__ == "__main__":
    main()
