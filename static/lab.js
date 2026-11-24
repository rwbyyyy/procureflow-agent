"use strict";
const byId = (id) => document.getElementById(id);
const labels = {
  baseline: "无重规划 · 全量上下文",
  "replan-full": "重规划 · 全量上下文",
  "replan-compact": "重规划 · 压缩上下文",
  stable: "稳定采购", timeout: "目录超时", stock: "初选商品缺货",
  budget: "预算与价格变化", deadline: "交期缩短", impossible: "采购不可行",
  read_policy: "读取制度", read_budget: "查询预算", read_catalog: "查询目录",
  quote: "生成报价", submit: "提交草案", hold: "暂缓采购",
  optimal_draft: "最优草案通过", valid_hold: "正确暂缓", no_terminal_action: "未到达有效结果",
  stale_quote: "原报价失效", transient_timeout: "暂时超时", suboptimal_draft: "草案非最低价",
};
const nameOf = (key) => labels[key] || key;
let reports = {};

function setOptions(select, entries, preferred) {
  select.replaceChildren(...entries.map(([value, label]) => new Option(label, value)));
  if (entries.some(([value]) => value === preferred)) select.value = preferred;
}

function renderReplay() {
  const report = reports[byId("mode").value];
  const candidates = report.replays.filter((run) => run.case_id === byId("case").value);
  const strategy = byId("strategy");
  setOptions(strategy, candidates.map((run) => [run.strategy, nameOf(run.strategy)]),
    strategy.value || "replan-compact");
  const run = candidates.find((record) => record.strategy === strategy.value);
  byId("steps").replaceChildren();
  if (!run) {
    byId("result").textContent = "当前批次没有可回放任务。";
    return;
  }
  const receipt = run.receipt?.total != null ? ` · 草案 ¥${run.receipt.total.toLocaleString("zh-CN")}` : "";
  const stopped = !run.verdict.passed && run.stop !== "terminal_action" ? ` · 停止原因：${nameOf(run.stop)}` : "";
  byId("result").textContent = `${nameOf(run.verdict.reason)} · ${run.steps.length} 步 · ${run.replans} 次重规划${receipt}${stopped}`;
  for (const step of run.steps) {
    const item = document.createElement("li");
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    const payload = document.createElement("pre");
    const error = step.observation.code ? ` · ${nameOf(step.observation.code)}` : "";
    const replan = step.replanned ? " · 已重规划" : "";
    summary.classList.toggle("replanned", step.replanned);
    summary.textContent = `${nameOf(step.action.tool)} · 状态版本 ${step.observation.revision}${error}${replan}`;
    payload.textContent = JSON.stringify(step, null, 2);
    details.append(summary, payload);
    item.append(details);
    byId("steps").append(item);
  }
}

function renderReport() {
  const mode = byId("mode").value;
  const report = reports[mode];
  const live = mode === "live";
  byId("usage-heading").textContent = live ? "实际输入 token" : "累计输入字节";
  byId("mode-note").textContent = live
    ? `真实模型 ${report.engines.join(", ")} 的线路抽查，仅代表所列案例。`
    : "离线规则控制器用于可复现实验。单位为 UTF-8 字节，模型能力以真实调用记录为准。";
  const paired = report.paired_tokens;
  if (live && paired) {
    const full = paired.prompt_tokens["replan-full"].toLocaleString("zh-CN");
    const compact = paired.prompt_tokens["replan-compact"].toLocaleString("zh-CN");
    byId("mode-note").textContent += paired.both_passed
      ? ` ${paired.both_passed}/${paired.pairs} 个案例两组都通过，按这些案例比较，输入 token 为 ${full} → ${compact}。`
      : " 当前没有两组都通过的案例。";
  }
  const rows = report.metrics.filter((row) => row.split === "all").map((row) => {
    const tr = document.createElement("tr");
    const values = [nameOf(row.strategy), `${row.passed}/${row.episodes}`,
      row.failure_cases ? `${row.recovered}/${row.failure_cases}` : "—", row.mean_actions,
      (live ? row.prompt_tokens : row.context_bytes).toLocaleString("zh-CN")];
    for (const value of values) tr.insertCell().textContent = value;
    return tr;
  });
  byId("metrics").replaceChildren(...rows);
  const policy = report.policy_learning;
  byId("policy-learning").hidden = !policy;
  if (policy) {
    const full = policy.full_refresh;
    const learned = policy.learned_refresh;
    byId("policy-result").textContent =
      `表格策略在训练组用环境奖励学习，测试时冻结。全量刷新通过 ${full.passed}/${full.episodes}，` +
      `平均 ${full.mean_actions} 步；学习策略通过 ${learned.passed}/${learned.episodes}，` +
      `平均 ${learned.mean_actions} 步。该实验仅更新快照刷新策略。`;
  }
  const cases = [...new Map(report.replays.map((run) => [run.case_id, nameOf(run.scenario)]))];
  const stock = cases.find(([id]) => id.startsWith("stock-"));
  setOptions(byId("case"), cases, stock?.[0]);
  renderReplay();
}

async function start() {
  const status = byId("loading");
  try {
    const response = await fetch("/api/lab/reports");
    if (!response.ok) throw new Error("无法加载评测报告。");
    reports = await response.json();
    const modes = Object.keys(reports).map((mode) => [mode, mode === "live" ? "真实模型抽查" : "离线完整评测"]);
    if (!modes.length) throw new Error("暂无评测结果。运行 uv run python run_lab.py 后刷新。");
    setOptions(byId("mode"), modes, "offline");
    byId("mode").addEventListener("change", renderReport);
    byId("case").addEventListener("change", renderReplay);
    byId("strategy").addEventListener("change", renderReplay);
    renderReport();
    status.hidden = true;
    byId("report").hidden = false;
  } catch (error) {
    status.textContent = error.message;
  }
}

start();
