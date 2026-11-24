"""走同一工具循环的三种 Agent 配置，用于重规划与上下文压缩消融。"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

from lab_environment import CATEGORIES, TOOL_NAMES, Case, Sandbox, choose_bundle

MAX_ACTIONS = 20
MAX_REPLANS = 3
RECENT_STEPS = 3
READ_KEYS = ("policy", "budget", *CATEGORIES)


@dataclass(frozen=True)
class Strategy:
    id: str
    replanning: bool
    compact: bool


STRATEGIES = (
    Strategy("baseline", replanning=False, compact=False),
    Strategy("replan-full", replanning=True, compact=False),
    Strategy("replan-compact", replanning=True, compact=True),
)


def serialize(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class State(TypedDict):
    request: dict
    revision: int
    snapshots: dict[str, dict]
    steps: list[dict]
    quote: dict | None
    replans: int
    next_action: dict
    context_bytes: int
    token_usage: dict[str, int]
    stopped: str | None


def pending(state: State) -> list[str]:
    reads = [f"read:{key}" for key in READ_KEYS if key not in state["snapshots"]]
    return [*reads, *([] if state["quote"] else ["quote"]), "submit_or_hold"]


def context(state: State, compact: bool) -> dict:
    """快照保留当前版本证据和进度。压缩模式只带最近操作，完整轨迹留在回放。"""
    history = state["steps"]
    if compact:
        history = [
            {
                "action": step["action"],
                "code": step["observation"].get("code"),
                "revision": step["observation"]["revision"],
            }
            for step in history[-RECENT_STEPS:]
        ]
    return {
        "request": state["request"],
        "revision": state["revision"],
        "snapshots": state["snapshots"],
        "pending": pending(state),
        "quote": state["quote"],
        "replans": state["replans"],
        "history": history,
    }


def rule_decision(view: dict) -> dict:
    """离线模式只读取策略上下文；没有访问隐藏的环境变更或测试标签。"""
    snapshots = view["snapshots"]
    for key in READ_KEYS:
        if key not in snapshots:
            if key in CATEGORIES:
                return {"tool": "read_catalog", "argument": key}
            return {"tool": f"read_{key}"}
    if view["quote"]:
        return {"tool": "submit"}
    catalog = [item for category in CATEGORIES for item in snapshots[category]["items"]]
    bundle = choose_bundle(
        view["request"], catalog, snapshots["budget"]["available"], snapshots["policy"]
    )
    return (
        {"tool": "quote", "argument": bundle}
        if bundle
        else {"tool": "hold", "argument": "no_feasible_bundle"}
    )


PROMPT = """You are a policy in a simulated procurement evaluation, selecting one action per turn.
Return a JSON object with tool and, only if needed, argument. Available tools:
read_policy, read_budget, read_catalog (argument: laptop, monitor, or dock),
quote (argument: array of distinct SKUs, one SKU per requested category), submit,
hold (argument: short reason, only when the complete request cannot be met).
Follow pending reads before quoting or holding. Fetch each missing category separately.
For example, use {"tool":"read_catalog","argument":"laptop"}. Use the same format after a retry.
From current snapshots, calculate the lowest total cost bundle covering all quantities.
Check stock, deadline, laptop_cap, and total budget. Do not change requested quantities.
After a successful quote, submit that quote. After a timeout retry the missing read;
after changes trust only the current revision and refresh missing evidence. Do not add prose."""


class LiveModel:
    """复用 Demo 的供应商配置；记录供应商返回的实际 token 用量。"""

    def __init__(self, client: httpx.Client):
        self.client = client
        self.key = os.getenv("DEEPSEEK_API_KEY")
        if not self.key:
            raise ValueError("真实模型模式需要设置 DEEPSEEK_API_KEY")
        self.model = os.getenv("AGENT_MODEL", "deepseek-chat")
        self.endpoint = os.getenv("AGENT_API_BASE", "https://api.deepseek.com").rstrip("/")

    def __call__(self, view: dict) -> tuple[dict, dict[str, int]]:
        response = self.client.post(
            f"{self.endpoint}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}"},
            json={
                "model": self.model,
                "temperature": 0,
                "max_tokens": 220,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": serialize(view)},
                ],
            },
        )
        response.raise_for_status()
        result = response.json()
        message = result["choices"][0]["message"]["content"]
        usage = {
            name: int(result.get("usage", {}).get(name, 0))
            for name in ("prompt_tokens", "completion_tokens")
        }
        try:
            action = json.loads(message or "")
        except json.JSONDecodeError:
            action = {}
        return action if isinstance(action, dict) else {}, usage


def normalize_action(action: dict) -> dict:
    """接受已知的工具参数包装格式，未知字段仍由沙盒拒绝。"""
    tool, argument = action.get("tool"), action.get("argument")
    argument_key = (
        {"read_catalog": "category", "quote": "skus", "hold": "reason"}.get(tool)
        if (isinstance(tool, str))
        else None
    )
    if argument_key and isinstance(argument, dict) and set(argument) == {argument_key}:
        argument = argument[argument_key]
    return {"tool": tool, "argument": argument}


def run_case(
    case: Case,
    strategy: Strategy,
    model: LiveModel | None = None,
    *,
    refresh_policy: Callable[[str], tuple[str, ...]] | None = None,
) -> dict:
    """运行独立 episode。所有策略共用环境、行动预算、模型提示和验收规则。"""
    sandbox = Sandbox(case)

    def decide(state: State) -> dict:
        view = context(state, strategy.compact)
        if model is None:
            action, usage = rule_decision(view), {}
        else:
            action, usage = model(view)
        action = normalize_action(action)
        return {
            "next_action": action,
            "context_bytes": len(serialize(view).encode("utf-8")),
            "token_usage": usage,
        }

    def act(state: State) -> dict:
        action = state["next_action"]
        tool = action["tool"]
        arg = action["argument"]
        before = pending(state)
        snapshots = state["snapshots"].copy()
        quote = state["quote"]
        replans = state["replans"]
        if tool not in TOOL_NAMES:
            obs = sandbox.call("invalid")
        elif tool in ("quote", "submit", "hold") and not all(key in snapshots for key in READ_KEYS):
            obs = {"ok": False, "revision": state["revision"], "code": "missing_evidence"}
        else:
            obs = sandbox.call(tool, arg)
        stopped = None
        refresh_keys: tuple[str, ...] = ()
        if obs["ok"]:
            if tool in ("read_policy", "read_budget"):
                snapshots[tool.removeprefix("read_")] = obs
            elif tool == "read_catalog":
                snapshots[arg] = obs
            elif tool == "quote":
                quote = obs
            elif tool in ("submit", "hold"):
                stopped = "terminal_action"
        elif not strategy.replanning or replans >= MAX_REPLANS:
            stopped = obs["code"]
        else:
            replans += 1
            if obs["revision"] != state["revision"]:
                # 通用策略刷新全部快照。策略学习实验可以选择刷新范围，最终仍实时验收。
                refresh_keys = (
                    refresh_policy(obs.get("change", "unknown")) if refresh_policy else READ_KEYS
                )
                for key in refresh_keys:
                    snapshots.pop(key, None)
                quote = None
        if stopped is None and len(state["steps"]) + 1 >= MAX_ACTIONS:
            stopped = "action_limit"
        updated = {
            **state,
            "snapshots": snapshots,
            "quote": quote,
            "replans": replans,
            "revision": obs["revision"],
        }
        event = {
            "step": len(state["steps"]) + 1,
            "action": action,
            "observation": obs,
            "plan_before": before,
            "plan_after": pending(updated),
            "replanned": replans != state["replans"],
            "refreshed_keys": refresh_keys,
            "context_bytes": state["context_bytes"],
            "tokens": state["token_usage"],
        }
        return {
            "steps": [*state["steps"], event],
            "snapshots": snapshots,
            "quote": quote,
            "replans": replans,
            "revision": obs["revision"],
            "stopped": stopped,
        }

    graph = StateGraph(State)
    graph.add_node("decide", decide)
    graph.add_node("act", act)
    graph.add_edge(START, "decide")
    graph.add_edge("decide", "act")
    graph.add_conditional_edges("act", lambda state: END if state["stopped"] else "decide")
    state = graph.compile().invoke(
        {
            "request": case.request(),
            "revision": 1,
            "snapshots": {},
            "steps": [],
            "quote": None,
            "replans": 0,
            "stopped": None,
        },
        {"recursion_limit": MAX_ACTIONS * 2 + 4},
    )
    return {
        "case_id": case.id,
        "scenario": case.family,
        "group": case.group,
        "split": case.split,
        "strategy": strategy.id,
        "engine": model.model if model else "offline-rule",
        "request": case.request(),
        "steps": state["steps"],
        "stop": state["stopped"],
        "replans": state["replans"],
        "receipt": sandbox.receipt,
        "verdict": sandbox.grade(),
    }
