const state = {
  selectedTaskId: null,
  selectedArtifact: "stdout",
};

function badgeClass(status) {
  if (["failed", "timed_out", "stale"].includes(status)) return "badge danger";
  if (["blocked", "ready_for_review", "ready_for_synthesis"].includes(status)) return "badge warn";
  if (["running", "active", "waiting"].includes(status)) return "badge";
  return "badge neutral";
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderStats(payload) {
  const stats = document.getElementById("stats");
  stats.innerHTML = "";
  const cards = [
    ["Running Jobs", payload.overview.running_jobs],
    ["Runnable", payload.overview.runnable_tasks],
    ["Blocked", payload.overview.blocked_tasks],
    ["Stale Graphs", payload.overview.stale_graphs],
    ["Ready Groups", payload.overview.ready_for_synthesis_groups],
    ["Completed Jobs", payload.queue_counts.completed],
  ];
  for (const [label, value] of cards) {
    const card = el("article", "stat");
    card.append(el("p", "stat-label", label));
    card.append(el("p", "stat-value", String(value)));
    stats.append(card);
  }
}

function renderMeta(targetId, pairs) {
  const root = document.getElementById(targetId);
  root.innerHTML = "";
  for (const [label, value] of pairs) {
    root.append(el("dt", "", label));
    root.append(el("dd", "", value ?? "—"));
  }
}

function renderList(containerId, items, renderer, emptyText) {
  const root = document.getElementById(containerId);
  root.innerHTML = "";
  if (!items || items.length === 0) {
    root.append(el("p", "empty", emptyText));
    return;
  }
  for (const item of items) {
    root.append(renderer(item));
  }
}

function renderGraphItem(graph) {
  const card = el("article", "item-card");
  const header = el("header");
  const titleWrap = el("div");
  titleWrap.append(el("h3", "", graph.graph_id));
  titleWrap.append(el("p", "", `groups: ${graph.task_group_ids.join(", ") || "none"}`));
  header.append(titleWrap);
  const badge = el("span", badgeClass(graph.stale ? "stale" : graph.status), graph.stale ? `${graph.status} stale` : graph.status);
  header.append(badge);
  card.append(header);
  card.append(el("p", "", `completed ${graph.completed.length} | running ${graph.running.length} | runnable ${graph.runnable_pending.length} | blocked ${graph.blocked_pending.length} | failed ${graph.failed.length}`));
  card.append(el("p", "", `last updated: ${graph.last_updated_utc || "unknown"} | age ${graph.age_minutes ?? "unknown"}m`));
  if (graph.blocked_pending.length) {
    const pills = el("div", "pills");
    graph.blocked_pending.forEach((item) => pills.append(el("span", "pill", `${item.task_id}: ${item.reason}`)));
    card.append(pills);
  }
  return card;
}

function renderTaskGroupItem(group) {
  const card = el("article", "item-card");
  const header = el("header");
  const titleWrap = el("div");
  titleWrap.append(el("h3", "", group.task_group_title));
  titleWrap.append(el("p", "", `${group.task_group_id} | graphs: ${group.graph_ids.join(", ") || "none"}`));
  header.append(titleWrap);
  header.append(el("span", badgeClass(group.status), group.status));
  card.append(header);
  card.append(el("p", "", `completed ${group.completed.length} | running ${group.running.length} | runnable ${group.runnable_pending.length} | blocked ${group.blocked_pending.length} | failed ${group.failed.length}`));
  return card;
}

function renderPendingItem(item, reason) {
  const card = el("article", "item-card");
  card.append(el("h3", "", item.task_id));
  card.append(el("p", "", item.objective));
  card.append(el("p", "", `graph ${item.graph_id} | group ${item.task_group_id}${reason ? ` | ${reason}` : ""}`));
  return card;
}

function renderJobItem(job) {
  const card = el("article", "item-card");
  const header = el("header");
  const titleWrap = el("div");
  titleWrap.append(el("h3", "", job.task_id));
  titleWrap.append(el("p", "", `${job.task_group_title} | graph ${job.graph_id}`));
  header.append(titleWrap);
  header.append(el("span", badgeClass(job.status), job.status));
  card.append(header);
  card.append(el("p", "", job.summary || job.objective));
  card.append(el("p", "", `reasoning ${job.reasoning_effort} | timeout ${job.timeout_minutes}m | priority ${job.priority}`));
  card.append(el("p", "", `started ${job.started_at_utc || "unknown"} | finished ${job.finished_at_utc || "—"} | elapsed ${job.elapsed_minutes ?? "—"}m`));
  if (job.decision) {
    card.append(el("p", "", `decision ${job.decision}${job.delta !== null && job.delta !== undefined ? ` | delta ${job.delta}` : ""}`));
  }
  const pills = el("div", "pills");
  (job.conflict_keys || []).forEach((key) => pills.append(el("span", "pill", key)));
  (job.depends_on || []).forEach((dep) => pills.append(el("span", "pill", `dep:${dep}`)));
  if (pills.children.length) card.append(pills);

  const actions = el("div", "job-actions");
  ["stdout", "stderr", "analysis", "task", "status", "heartbeat"].forEach((kind) => {
    const button = el("button", "button muted", kind);
    button.disabled = (kind === "stdout" && !job.has_stdout)
      || (kind === "stderr" && !job.has_stderr)
      || (kind === "analysis" && !job.has_analysis)
      || (kind === "heartbeat" && !job.has_heartbeat);
    button.addEventListener("click", () => showArtifact(job.task_id, kind));
    actions.append(button);
  });
  card.append(actions);
  return card;
}

async function showArtifact(taskId, kind) {
  state.selectedTaskId = taskId;
  state.selectedArtifact = kind;
  document.getElementById("artifactLabel").textContent = `${taskId} · ${kind}`;
  const viewer = document.getElementById("artifactViewer");
  viewer.textContent = "Loading artifact…";
  try {
    const response = await fetch(`/api/artifact?task_id=${encodeURIComponent(taskId)}&kind=${encodeURIComponent(kind)}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const text = await response.text();
    viewer.textContent = text || "(empty)";
  } catch (error) {
    viewer.textContent = `Unable to load artifact: ${error}`;
  }
}

async function refresh() {
  const response = await fetch("/api/dashboard");
  const payload = await response.json();

  document.getElementById("goal").textContent = payload.goal || "No goal configured.";
  document.getElementById("generatedAt").textContent = `Updated ${payload.generated_at_utc}`;
  document.getElementById("loopStatus").className = badgeClass(payload.loop.status);
  document.getElementById("loopStatus").textContent = payload.loop.status;
  document.getElementById("sessionMode").className = badgeClass(payload.session.mode || "none");
  document.getElementById("sessionMode").textContent = payload.session.mode || "none";
  document.getElementById("latestCompletion").textContent = payload.latest_completion_message || "No completion message yet.";

  renderStats(payload);
  renderMeta("loopMeta", [
    ["Target", payload.target_value === null ? "—" : `${payload.headline_metric}: ${payload.target_value}`],
    ["Stop reason", payload.loop.stop_reason || "active"],
    ["Last updated", payload.loop.last_updated_utc || "—"],
    ["Latest cycle", payload.loop.latest_cycle ? `cycle ${payload.loop.latest_cycle.cycle}` : "—"],
  ]);
  renderMeta("sessionMeta", [
    ["Session ID", payload.session.session_id || "—"],
    ["Mode", payload.session.mode || "—"],
    ["Status", payload.session.status || "—"],
    ["Current cycle", payload.session.current_cycle ?? "—"],
    ["Dispatched", payload.session.dispatched_at_utc || "—"],
  ]);

  document.getElementById("graphCount").textContent = String(payload.graphs.length);
  document.getElementById("groupCount").textContent = String(payload.task_groups.length);
  document.getElementById("runnableCount").textContent = String(payload.runnable_pending.length);
  document.getElementById("blockedCount").textContent = String(payload.blocked_pending.length);
  document.getElementById("jobCount").textContent = String(payload.jobs.length);

  renderList("graphs", payload.graphs, renderGraphItem, "No graphs yet.");
  renderList("taskGroups", payload.task_groups, renderTaskGroupItem, "No task groups yet.");
  renderList("runnablePending", payload.runnable_pending, (item) => renderPendingItem(item), "No runnable pending tasks.");
  renderList("blockedPending", payload.blocked_pending, (item) => renderPendingItem(item, item.reason), "No blocked pending tasks.");
  renderList("jobs", payload.jobs, renderJobItem, "No jobs yet.");

  if (state.selectedTaskId) {
    showArtifact(state.selectedTaskId, state.selectedArtifact);
  }
}

document.getElementById("refreshButton").addEventListener("click", refresh);
document.querySelectorAll(".artifact-actions .button").forEach((button) => {
  button.addEventListener("click", () => {
    if (!state.selectedTaskId) return;
    showArtifact(state.selectedTaskId, button.dataset.kind);
  });
});

refresh().catch((error) => {
  document.getElementById("artifactViewer").textContent = `Dashboard failed to load: ${error}`;
});
setInterval(() => refresh().catch(() => {}), 10000);
