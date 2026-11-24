"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  user: null,
  runs: [],
  selected: null,
  run: null,
  view: "workspace",
  tab: "timeline",
  stream: null,
  refreshTimer: null,
};
const labels = {
  running: "执行中",
  awaiting_approval: "待审批",
  completed: "已完成",
  rejected: "已拒绝",
  failed: "执行失败",
  paused: "已暂停",
  needs_revision: "需调整需求",
};
const money = (value) => "¥" + Number(value).toLocaleString("zh-CN");
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
function toast(message) {
  $("toast").textContent = message;
  $("toast").style.display = "block";
  setTimeout(() => {
    $("toast").style.display = "none";
  }, 4500);
}
async function api(path, body) {
  const response = await fetch("/api" + path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let value;
  try {
    value = await response.json();
  } catch {
    throw Error("服务暂时无法响应");
  }
  if (!response.ok) {
    if (response.status === 401) $("login-dialog").hidden = false;
    throw Error(
      typeof value.detail === "string" ? value.detail : "请求格式不正确",
    );
  }
  return value;
}
async function login(account, password) {
  if (state.stream) state.stream.close();
  state.user = await api("/login", { account, password });
  $("login-dialog").hidden = true;
  $("account").value = account;
  state.selected = null;
  state.run = null;
  state.tab = "timeline";
  await refresh();
}
function renderList() {
  const filtered =
    state.view === "approvals"
      ? state.runs.filter((r) => r.status === "awaiting_approval")
      : state.runs;
  $("list-count").textContent = filtered.length + " 个任务";
  $("runs").innerHTML = filtered.length
    ? filtered
        .map(
          (run) =>
            `<button class="run-card ${state.selected === run.id ? "active" : ""}" data-id="${run.id}"><span class="run-title">${escape(run.request)}</span><span class="run-meta"><span>${run.mode === "live" ? "真实模型" : "离线演示"}</span><span class="badge ${run.status}">${labels[run.status] || run.status}</span></span></button>`,
        )
        .join("")
    : '<div class="no-runs">这里还没有任务</div>';
  document
    .querySelectorAll(".run-card")
    .forEach((button) => (button.onclick = () => selectRun(button.dataset.id)));
}
async function refresh() {
  if (!state.user) return;
  const [user, runs] = await Promise.all([api("/me"), api("/runs")]);
  state.user = user;
  state.runs = runs;
  $("budget").textContent = money(user.budget);
  $("run-count").textContent = runs.length;
  const pending = runs.filter((r) => r.status === "awaiting_approval").length;
  $("approval-count").textContent = pending;
  $("pending-count").textContent = pending;
  $("complete-count").textContent = runs.filter(
    (r) => r.status === "completed",
  ).length;
  $("tenant-label").textContent =
    user.tenant === "atlas" ? "Atlas Lab" : "Nova Lab";
  $("avatar").textContent = user.name[0];
  $("composer").hidden = user.role !== "employee" || state.view !== "workspace";
  renderList();
  if (state.selected && !runs.some((r) => r.id === state.selected))
    state.selected = null;
  if (!state.selected && runs.length) {
    const list =
      state.view === "approvals"
        ? runs.filter((r) => r.status === "awaiting_approval")
        : runs;
    if (list.length) await selectRun(list[0].id);
  } else if (state.selected) await loadDetail();
  else {
    $("empty").hidden = false;
    $("detail").hidden = true;
  }
}
async function selectRun(id) {
  state.selected = id;
  state.tab =
    state.view === "audit"
      ? "timeline"
      : state.view === "approvals"
        ? "proposal"
        : "timeline";
  renderList();
  state.run = null;
  await loadDetail();
  if (state.stream) state.stream.close();
  if (state.run?.status === "running") {
    state.stream = new EventSource(`/api/runs/${id}/events`);
    state.stream.onmessage = () => {
      clearTimeout(state.refreshTimer);
      state.refreshTimer = setTimeout(() => loadDetail().catch(() => {}), 180);
    };
    state.stream.onerror = () => {
      state.stream.close();
    };
  }
}
function renderDetail(run) {
  $("empty").hidden = true;
  $("detail").hidden = false;
  $("detail-title").textContent = run.request;
  $("run-mode").textContent =
    run.mode === "live"
      ? "LIVE MODEL · 真实工具调用"
      : "OFFLINE DEMO · 规则驱动，未调用大模型";
  $("run-id").textContent = "任务 " + run.id.slice(0, 12);
  $("status").textContent = labels[run.status] || run.status;
  $("status").className = "badge " + run.status;
  $("error").hidden = !run.error;
  $("error").textContent = run.error || "";
  $("checkpoint-line").textContent =
    `${run.checkpoints} 个持久化检查点${run.checkpoint ? " · 当前 " + run.checkpoint.slice(0, 18) : ""}${run.next.length ? " · 下一节点 " + run.next.join(", ") : " · 执行已结束"}`;
  $("timeline").innerHTML = run.events
    .map(
      (event) =>
        `<div class="event"><div class="event-head"><span>${escape(event.title)}</span><time>${escape(event.created.slice(11))}</time></div><small>${escape(event.kind)}</small>${event.payload ? `<details><summary>查看工具参数与结果</summary><pre>${escape(JSON.stringify(event.payload, null, 2))}</pre></details>` : ""}</div>`,
    )
    .join("");
  $("evidence").innerHTML = run.policies
    .map(
      (policy) =>
        `<div class="evidence-item"><strong>[${policy.id}] ${escape(policy.title)}</strong><p>${escape(policy.content)}</p></div>`,
    )
    .join("");
  const p = run.proposal;
  $("proposal").innerHTML = p
    ? `<div class="proposal-title">${escape(p.item.name)}</div><div class="proposal-vendor">${escape(p.sku)} · ${escape(p.item.vendor)} · ${escape(p.item.spec)}</div><div class="proposal-grid"><div><span>采购数量</span><strong>${p.quantity} 台</strong></div><div><span>目录单价</span><strong>${money(p.item.price)}</strong></div><div><span>申请总额</span><strong>${money(p.total)}</strong></div></div><p class="proposal-text">${escape(p.rationale)}</p><p class="proposal-text">预计交期 ${p.item.delivery_days} 天 · 当前选型来自准入供应商目录</p><div>${p.citations.map((c) => `<span class="citation">${escape(c)}</span>`).join("")}</div>${run.order ? `<div class="order"><strong>演示订单已登记</strong><br>${escape(run.order.id)} · ${money(run.order.total)}<br>仅本地模拟登记，未向供应商发送或产生付款。</div>` : ""}${run.decision ? `<p class="proposal-text">审批意见：${escape(run.decision.comment)}</p>` : ""}`
    : `<p class="proposal-text">${escape(run.answer || "Agent 完成查询并通过服务端校验后，建议将显示在这里。")}</p>`;
  const awaiting = run.status === "awaiting_approval";
  $("approval-box").hidden = !awaiting || state.user.role !== "approver";
  $("waiting").hidden = !awaiting || state.user.role === "approver";
  $("resume").hidden = !(
    run.owner === state.user.id && ["failed", "paused"].includes(run.status)
  );
  $("export").href = `/api/runs/${run.id}/report`;
  renderTabs();
}
function renderTabs() {
  document
    .querySelectorAll("[data-tab]")
    .forEach((button) =>
      button.classList.toggle("active", button.dataset.tab === state.tab),
    );
  ["timeline", "evidence", "proposal"].forEach(
    (id) => ($(id).hidden = state.tab !== id),
  );
}
async function loadDetail() {
  if (!state.selected) return;
  const id = state.selected;
  const run = await api("/runs/" + id);
  if (state.selected !== id) return;
  if (JSON.stringify(state.run) === JSON.stringify(run)) return;
  state.run = run;
  renderDetail(run);
}
async function action(button, operation) {
  button.disabled = true;
  try {
    await operation();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}
$("login-form").onsubmit = (event) => {
  event.preventDefault();
  action(event.submitter, () =>
    login($("login-account").value, $("password").value),
  );
};
$("account").onchange = () => {
  $("login-account").value = $("account").value;
  $("login-dialog").hidden = false;
};
$("refresh").onclick = () => action($("refresh"), refresh);
document.querySelectorAll("[data-prompt]").forEach(
  (button) =>
    (button.onclick = () => {
      $("request").value = button.dataset.prompt;
      $("request").focus();
    }),
);
document.querySelectorAll("[data-view]").forEach(
  (button) =>
    (button.onclick = async () => {
      state.view = button.dataset.view;
      document
        .querySelectorAll("[data-view]")
        .forEach((b) => b.classList.toggle("active", b === button));
      const name = {
        workspace: "执行工作台",
        approvals: "审批队列",
        audit: "审计记录",
      }[state.view];
      $("view-title").textContent = name;
      $("list-heading").textContent =
        state.view === "approvals" ? "待审批申请" : "任务列表";
      state.selected = null;
      await refresh().catch((e) => toast(e.message));
    }),
);
document.querySelectorAll("[data-tab]").forEach(
  (button) =>
    (button.onclick = () => {
      state.tab = button.dataset.tab;
      renderTabs();
    }),
);
$("mode").onchange = () => {
  $("mode-help").textContent =
    $("mode").value === "live"
      ? "发送模拟需求至 DeepSeek，按实际用量计费"
      : "共用真实工具、权限与持久化链路";
};
$("start").onclick = () =>
  action($("start"), async () => {
    const request = $("request").value.trim();
    if (request.length < 8) throw Error("请至少输入 8 个字符的采购需求");
    const result = await api("/runs", { request, mode: $("mode").value });
    await refresh();
    await selectRun(result.id);
    toast("Agent 已启动，可在执行轨迹中查看工具结果");
  });
for (const [id, decision] of [
  ["approve", "approve"],
  ["reject", "reject"],
])
  $(id).onclick = () =>
    action($(id), async () => {
      const comment = $("comment").value.trim();
      if (comment.length < 3) throw Error("请填写至少 3 个字符的审批意见");
      await api(`/runs/${state.selected}/decision`, {
        action: decision,
        comment,
      });
      $("comment").value = "";
      await refresh();
      toast(
        decision === "approve" ? "审批已记录，正在恢复执行" : "拒绝决定已记录",
      );
    });
$("resume").onclick = () =>
  action($("resume"), async () => {
    await api(`/runs/${state.selected}/resume`, {});
    await refresh();
    toast("正在从检查点恢复");
  });
async function boot() {
  try {
    const config = await api("/config");
    $("mode").querySelector('[value="live"]').disabled = !config.live_available;
    state.user = await api("/me");
    $("account").value = state.user.id;
    await refresh();
  } catch {
    $("login-dialog").hidden = false;
  }
}
boot();
setInterval(() => {
  if (state.user && document.visibilityState === "visible")
    refresh().catch(() => {});
}, 2500);
