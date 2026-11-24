"""可复现的采购沙盒。每个 episode 隔离，唯一写操作是登记模拟草案。"""

from dataclasses import asdict, dataclass
from itertools import product
from typing import Literal

Category = Literal["laptop", "monitor", "dock"]
Family = Literal["stable", "timeout", "stock", "budget", "deadline", "impossible"]
Split = Literal["train", "dev", "test"]
CATEGORIES: tuple[Category, ...] = ("laptop", "monitor", "dock")
FAMILIES: tuple[Family, ...] = ("stable", "timeout", "stock", "budget", "deadline", "impossible")
TOOL_NAMES = ("read_policy", "read_budget", "read_catalog", "quote", "submit", "hold")


@dataclass(frozen=True)
class Product:
    sku: str
    category: Category
    price: int
    days: int
    stock: int


@dataclass(frozen=True)
class Case:
    id: str
    family: Family
    split: Split
    group: str
    quantities: dict[Category, int]
    budget: int
    deadline: int
    catalog: tuple[Product, ...]

    def request(self) -> dict:
        return {"quantities": self.quantities, "objective": "min_total_cost"}


def cases() -> list[Case]:
    """按采购配置组切分。同组变体及所有策略结果始终属于同一个分区。"""
    result: list[Case] = []
    profiles: tuple[tuple[Split, tuple[int, int, int]], ...] = (
        ("train", (2, 3, 2)),
        ("train", (4, 4, 3)),
        ("dev", (3, 5, 4)),
        ("test", (5, 6, 5)),
    )
    for split, (laptops, monitors, docks) in profiles:
        for variant in range(2):
            quantities = dict(zip(CATEGORIES, (laptops + variant, monitors, docks), strict=True))
            laptop_price = 6100 + laptops * 90 + variant * 25
            catalog = (
                Product("NB-E", "laptop", laptop_price, 5, 20),
                Product("NB-F", "laptop", laptop_price + 850, 2, 20),
                Product("NB-X", "laptop", 9650, 1, 20),
                Product("MN-E", "monitor", 1250, 5, 20),
                Product("MN-F", "monitor", 1730, 2, 20),
                Product("DK-E", "dock", 390, 5, 20),
                Product("DK-F", "dock", 570, 2, 20),
            )
            cheapest = laptop_price * quantities["laptop"] + 1250 * monitors + 390 * docks
            for family in FAMILIES:
                result.append(
                    Case(
                        f"{family}-{laptops}-{variant}",
                        family,
                        split,
                        f"profile-{laptops}",
                        quantities,
                        cheapest + 15000,
                        6,
                        catalog,
                    )
                )
    return result


def choose_bundle(request: dict, catalog: list[dict], budget: int, policy: dict) -> list[str]:
    """离线控制器的精确组合搜索。优先最低总价，相同总价按 SKU 排序。"""
    quantities = request["quantities"]
    options = [
        [
            item
            for item in catalog
            if item["category"] == category
            and item["days"] <= policy["deadline"]
            and item["stock"] >= quantity
            and (category != "laptop" or item["price"] <= policy["laptop_cap"])
        ]
        for category, quantity in quantities.items()
    ]
    feasible = []
    for combination in product(*options):
        total = sum(item["price"] * quantities[item["category"]] for item in combination)
        if total <= budget:
            feasible.append((total, sorted(item["sku"] for item in combination)))
    return min(feasible)[1] if feasible else []


class Sandbox:
    def __init__(self, case: Case):
        self.case = case
        self.catalog = {p.sku: asdict(p) for p in case.catalog}
        self.budget = case.budget
        self.deadline = case.deadline
        self.revision = 1
        self.injected = False
        self.timeout_sent = False
        self.quote: dict | None = None
        self.receipt: dict | None = None

    def policy(self) -> dict:
        return {"laptop_cap": 9000, "deadline": self.deadline, "approval": "draft_only"}

    def evaluate_bundle(self, skus: list[str]) -> tuple[int, str | None]:
        """服务端从当前状态获取金额、库存、交期和品类，校验完整采购组合。"""
        if len(skus) != len(CATEGORIES) or any(sku not in self.catalog for sku in skus):
            return 0, "invalid_bundle"
        items = [self.catalog[sku] for sku in skus]
        if {item["category"] for item in items} != set(CATEGORIES):
            return 0, "invalid_bundle"
        total = 0
        for item in items:
            quantity = self.case.quantities[item["category"]]
            if item["stock"] < quantity:
                return 0, "insufficient_stock"
            if item["days"] > self.deadline:
                return 0, "deadline_exceeded"
            if item["category"] == "laptop" and item["price"] > self.policy()["laptop_cap"]:
                return 0, "price_cap_exceeded"
            total += item["price"] * quantity
        return total, "budget_exceeded" if total > self.budget else None

    def inject_change(self) -> str | None:
        """在首次尝试提交已有报价时制造过期检测，不提前透露变更场景。"""
        if self.injected or self.case.family in ("stable", "timeout"):
            return None
        self.injected = True
        self.revision += 1
        if self.case.family == "stock":
            self.catalog["NB-E"]["stock"] = 0
            return "stock_changed"
        if self.case.family == "deadline":
            self.deadline = 2
            return "deadline_changed"
        if self.case.family == "budget":
            # 降预算的同时有新的目录折扣，刷新后仍能找出完整采购组合。
            self.catalog["NB-F"]["price"] = self.catalog["NB-E"]["price"] - 480
            cheapest, _ = self.evaluate_bundle(["NB-F", "MN-E", "DK-E"])
            self.budget = cheapest + 150
            return "budget_and_price_changed"
        self.budget = 1
        return "budget_changed"

    def call(self, tool: str, value: object = None) -> dict:
        """统一工具响应：版本号用于快照失效，拒绝原因用于规划器重试。"""
        details: dict = {}
        error = None
        if self.receipt is not None:
            return {"ok": False, "revision": self.revision, "code": "episode_closed"}
        if tool == "read_policy":
            details = self.policy()
        elif tool == "read_budget":
            details = {"available": self.budget}
        elif tool == "read_catalog" and value in CATEGORIES:
            if self.case.family == "timeout" and value == "monitor" and not self.timeout_sent:
                self.timeout_sent = True
                error = "transient_timeout"
            else:
                details = {
                    "category": value,
                    "items": [
                        item.copy() for item in self.catalog.values() if item["category"] == value
                    ],
                }
        elif tool == "quote" and isinstance(value, list) and all(isinstance(s, str) for s in value):
            total, error = self.evaluate_bundle(value)
            if not error:
                self.quote = {"skus": sorted(value), "total": total, "revision": self.revision}
                details = self.quote
        elif tool == "submit":
            if self.quote is None:
                error = "quote_required"
            elif change := self.inject_change():
                error = "stale_quote"
                details = {"change": change}
                self.quote = None
            else:
                _, error = self.evaluate_bundle(self.quote["skus"])
                if not error:
                    self.receipt = {"status": "draft", **self.quote}
                    details = self.receipt
        elif tool == "hold" and isinstance(value, str) and value.strip():
            self.receipt = {"status": "hold", "revision": self.revision, "reason": str(value)[:160]}
            details = self.receipt
        else:
            error = "invalid_action"
        return {
            "ok": error is None,
            "revision": self.revision,
            **details,
            **({"code": error} if error else {}),
        }

    def grade(self) -> dict:
        """由当前环境数据验收，使用穷举校验器计算独立于策略输出的可行最低价。"""
        by_category = [
            [sku for sku, item in self.catalog.items() if item["category"] == category]
            for category in CATEGORIES
        ]
        feasible_costs = [
            total
            for skus in product(*by_category)
            if (check := self.evaluate_bundle(list(skus)))[1] is None
            for total in [check[0]]
        ]
        optimum = min(feasible_costs, default=None)
        if self.receipt is None:
            return {"passed": False, "reason": "no_terminal_action", "optimum": optimum}
        if self.receipt["status"] == "hold":
            return {
                "passed": optimum is None,
                "reason": "valid_hold" if optimum is None else "premature_hold",
                "optimum": optimum,
            }
        cost, error = self.evaluate_bundle(self.receipt["skus"])
        return {
            "passed": error is None and cost == optimum,
            "optimum": optimum,
            "reason": error or ("optimal_draft" if cost == optimum else "suboptimal_draft"),
        }
