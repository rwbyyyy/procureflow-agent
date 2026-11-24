"""采购演示领域模型：价格、预算和权限均由服务端校验。"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field


class Proposal(BaseModel):
    """模型只选择目录条目，不能自行填写价格或供应商账户。"""

    model_config = ConfigDict(extra="forbid")
    sku: str = Field(min_length=1, max_length=40)
    quantity: int = Field(ge=1, le=100)
    rationale: str = Field(min_length=8, max_length=2000)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "reject"]
    comment: str = Field(min_length=3, max_length=500)


POLICIES = [
    (
        "P-101",
        "授权采购制度",
        "采购 供应商 白名单 商品",
        "采购仅允许选择已准入供应商目录中的商品。模型不得新增供应商、付款或绕过审批。",
    ),
    (
        "P-102",
        "研发设备价格上限",
        "笔记本 电脑 价格 上限 开发 研发",
        "研发笔记本单价上限为 9,000 元。超过价格上限的商品不可自动提交申请。",
    ),
    (
        "P-103",
        "预算与审批",
        "预算 审批 采购 金额",
        "总额不得超过部门可用预算。任何采购申请均须由采购审批人确认后才可登记演示订单，申请人不能审批自己的申请。",
    ),
    (
        "P-104",
        "交付与审计",
        "交付 日期 审计 记录",
        "建议应包含选型理由、商品编号、单价、数量和制度依据。保留工具调用、审批和执行结果以支持审计。演示订单不产生外部交易。",
    ),
]

CATALOG = [
    ("NB-014", "ThinkBook 开发笔记本", "laptop", "星河设备", 7999, "32GB / 1TB / 14英寸", 24, 3),
    ("NB-021", "ProBook 工作笔记本", "laptop", "远山科技", 8999, "32GB / 1TB / 16英寸", 12, 5),
    ("NB-099", "Creator 高性能工作站", "laptop", "星河设备", 11999, "64GB / 2TB / 独显", 8, 7),
    ("MN-008", "27英寸 4K 显示器", "monitor", "远山科技", 2399, "4K / USB-C / 65W", 40, 2),
]


class Database:
    """短事务 SQLite 存储，订单和预算扣减使用同一事务。"""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, owner TEXT NOT NULL,
                    request TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
                    error TEXT, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    kind TEXT NOT NULL, title TEXT NOT NULL, payload TEXT NOT NULL,
                    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS budgets (
                    tenant TEXT PRIMARY KEY, available INTEGER NOT NULL CHECK(available >= 0));
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, tenant TEXT NOT NULL,
                    sku TEXT NOT NULL, quantity INTEGER NOT NULL, total INTEGER NOT NULL,
                    approved_by TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS decisions (
                    run_id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
                    comment TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS catalog (
                    sku TEXT PRIMARY KEY, name TEXT, category TEXT, vendor TEXT,
                    price INTEGER, spec TEXT, stock INTEGER, delivery_days INTEGER);
                CREATE TABLE IF NOT EXISTS policies (
                    id TEXT PRIMARY KEY, title TEXT, keywords TEXT, content TEXT);
                CREATE VIRTUAL TABLE IF NOT EXISTS policy_fts
                    USING fts5(id, title, keywords, content);
            """)
            db.executemany(
                "INSERT OR IGNORE INTO budgets VALUES (?,?)", [("atlas", 30000), ("nova", 18000)]
            )
            db.executemany("INSERT OR IGNORE INTO catalog VALUES (?,?,?,?,?,?,?,?)", CATALOG)
            db.executemany("INSERT OR IGNORE INTO policies VALUES (?,?,?,?)", POLICIES)
            if db.execute("SELECT COUNT(*) FROM policy_fts").fetchone()[0] == 0:
                db.execute("INSERT INTO policy_fts SELECT * FROM policies")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, run_id: str, kind: str, title: str, payload: object = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(run_id,kind,title,payload) VALUES (?,?,?,?)",
                (run_id, kind, title, json.dumps(payload, ensure_ascii=False)),
            )

    def get_run(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    def status(self, run_id: str, status: str, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE runs SET status=?,error=? WHERE id=?", (status, error, run_id))

    def budget(self, tenant: str) -> int:
        with self.connect() as db:
            return db.execute("SELECT available FROM budgets WHERE tenant=?", (tenant,)).fetchone()[
                0
            ]

    def search(self, query: str) -> list[dict]:
        # 针对小型中文演示知识库提取已知术语，避免拼接用户输入到 FTS 表达式。
        terms = [
            t
            for t in ["采购", "预算", "审批", "笔记本", "价格", "供应商", "交付", "审计"]
            if t in query
        ]
        with self.connect() as db:
            if not terms:
                rows = db.execute("SELECT id,title,content FROM policies LIMIT 4").fetchall()
            else:
                rows = db.execute(
                    "SELECT id,title,content FROM policy_fts "
                    "WHERE policy_fts MATCH ? ORDER BY rank LIMIT 4",
                    (" OR ".join(f'"{t}"' for t in terms),),
                ).fetchall()
        return [dict(r) for r in rows]

    def catalog(self, category: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM catalog WHERE category=? ORDER BY price", (category,)
                )
            ]

    def validate_proposal(self, tenant: str, proposal: Proposal) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM catalog WHERE sku=?", (proposal.sku,)).fetchone()
        if row is None:
            raise ValueError("该商品不在准入目录内")
        item = dict(row)
        total = item["price"] * proposal.quantity
        if item["category"] == "laptop" and item["price"] > 9000:
            raise ValueError("笔记本单价超过制度上限 9,000 元")
        if proposal.quantity > item["stock"]:
            raise ValueError("商品库存不足")
        if total > self.budget(tenant):
            raise ValueError("申请总额超过部门可用预算")
        return {
            **proposal.model_dump(),
            "item": item,
            "total": total,
            "citations": ["P-101", "P-102", "P-103", "P-104"],
        }

    def fulfill(self, run_id: str, approved_by: str, proposal: dict) -> dict:
        """幂等登记演示订单，并在事务中重新检查预算与库存，避免审批期间数据变化。"""
        run = self.get_run(run_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM orders WHERE run_id=?", (run_id,)).fetchone()
            if existing:
                return dict(existing)
            decision = db.execute(
                "SELECT actor,action FROM decisions WHERE run_id=?", (run_id,)
            ).fetchone()
            if (
                decision is None
                or decision["action"] != "approve"
                or decision["actor"] != approved_by
                or approved_by == run["owner"]
            ):
                raise ValueError("缺少有效的独立审批记录，禁止登记订单")
            item = db.execute("SELECT * FROM catalog WHERE sku=?", (proposal["sku"],)).fetchone()
            quantity = proposal["quantity"]
            if item is None or not isinstance(quantity, int) or quantity < 1:
                raise ValueError("商品或数量无效，请重新发起申请")
            total = item["price"] * quantity
            if total != proposal["total"] or item["price"] != proposal["item"]["price"]:
                raise ValueError("审批后商品价格发生变化，请重新发起申请")
            if item["category"] == "laptop" and item["price"] > 9000:
                raise ValueError("笔记本单价超过制度上限")
            if item["stock"] < quantity:
                raise ValueError("审批后库存发生变化，请重新发起申请")
            if not db.execute(
                "UPDATE budgets SET available=available-? WHERE tenant=? AND available>=?",
                (total, run["tenant"], total),
            ).rowcount:
                raise ValueError("审批后预算发生变化，请重新发起申请")
            db.execute("UPDATE catalog SET stock=stock-? WHERE sku=?", (quantity, item["sku"]))
            order = {
                "id": "PO-" + run_id[:8].upper(),
                "run_id": run_id,
                "tenant": run["tenant"],
                "sku": item["sku"],
                "quantity": quantity,
                "total": total,
                "approved_by": approved_by,
            }
            db.execute(
                "INSERT INTO orders(id,run_id,tenant,sku,quantity,total,approved_by) "
                "VALUES (:id,:run_id,:tenant,:sku,:quantity,:total,:approved_by)",
                order,
            )
            return order
