/**
 * Overview View · Swiss Technical Typography & Flat Asymmetric Dual-Column Layout
 * Rule-delimited Asymmetric Metric Rail + Asymmetric Dual-Column Workspace + Graphic Baseline Termination
 */

import { t, fmtRelativeTime, fmtDate, fmtClockTime } from "../i18n.js";
import { state } from "../state.js";
import { el, txt, pageHead, emptyPanel, metricRail, chip, tip, monoHash, renderModeBadge, renderHealthIndicator } from "../ui.js";
import { fetchJSON } from "../api.js";
import { navigate } from "../router.js";

let activeOnlyFilter = false;
let textFilter = "";

export async function renderOverviewView() {
  const container = el("div", "overview-view");

  const r = await fetchJSON("/api/overview");
  const data = (r && r.ok && r.data) ? r.data : (state.overview || {});
  state.overview = data;

  const g = data.global || {};
  const studies = data.studies || [];
  const allocations = data.allocations || [];

  // Page Header (Technical Display Heading + Live Timestamp + Primary Action)
  const genTime = data.generated_at ? fmtDate(new Date(data.generated_at)) : fmtDate(new Date());
  const head = pageHead(t("overviewTitle") || "总览", t("overviewDesc", { time: genTime }), [
    el("button", "plain primary", {
      onclick: () => navigate("#/compose")
    }, "⚡ " + (t("navCompose") || "评测工作台"))
  ]);
  container.appendChild(head);

  // Section 1: Asymmetrical Rule-Delimited Swiss Metric Rail (No outer container border, dominant primary measure)
  const totalStudiesCount = data.study_count !== undefined ? data.study_count : studies.length;
  const queuedCount = g.queued || 0;
  const recoveringCount = g.recovering || 0;
  const reconcilingCount = g.reconciling || 0;
  const staleReconcilingCount = g.stale_reconciling;

  const studiesSub = totalStudiesCount === 1
    ? (t("metricActiveStudySingle") || "1 项活跃研究")
    : totalStudiesCount > 0
      ? (t("metricActiveStudyPlural", { n: totalStudiesCount }) || `${totalStudiesCount} 项活跃研究`)
      : (t("metricZeroRegistered") || "0 项已登记");

  const queuedSub = queuedCount > 0
    ? (queuedCount === 1 ? (t("metricAwaitingSlotSingle") || "等待调度槽位") : (t("metricAwaitingSlotsPlural", { n: queuedCount }) || `${queuedCount} 项等待槽位`))
    : (t("metricQueueClear") || "队列无积压");

  const recoveringSub = recoveringCount > 0
    ? (recoveringCount === 1 ? (t("metricSolverRetrySingle") || "1 项重试中") : (t("metricSolverRetriesPlural", { n: recoveringCount }) || `${recoveringCount} 项重试中`))
    : (t("metricZeroRecovering") || "0 项重试中");

  const reconcilingSub = reconcilingCount > 0
    ? (reconcilingCount === 1 ? (t("metricSyncingNodeSingle") || "1 项同步中") : (t("metricSyncingNodesPlural", { n: reconcilingCount }) || `${reconcilingCount} 项同步中`))
    : (t("metricZeroReconciling") || "0 项同步中");

  const metricItems = [
    {
      primary: true,
      label: t("statStudies") || "研究总数",
      val: totalStudiesCount,
      tipKey: "tipStudyId",
      sub: studiesSub
    },
    {
      label: t("statQueued") || "排队中",
      val: queuedCount,
      dotTone: queuedCount > 0 ? "warn" : null,
      tone: queuedCount > 0 ? "warn" : "neutral",
      tipKey: "tipWaiting",
      sub: queuedSub
    },
    {
      label: t("statRecovering") || "重试中",
      val: recoveringCount,
      dotTone: recoveringCount > 0 ? "bad" : null,
      tone: recoveringCount > 0 ? "bad" : "neutral",
      tipKey: "tipStaleReconciling",
      sub: recoveringSub
    },
    {
      label: t("statReconciling") || "节点同步中",
      val: reconcilingCount,
      dotTone: reconcilingCount > 0 ? "warn" : null,
      tone: reconcilingCount > 0 ? "warn" : "neutral",
      tipKey: "tipStaleReconciling",
      sub: reconcilingSub
    }
  ];

  if (staleReconcilingCount !== undefined) {
    metricItems.push({
      label: t("statStaleReconciling") || "同步超时",
      val: staleReconcilingCount || 0,
      dotTone: (staleReconcilingCount || 0) > 0 ? "bad" : null,
      tone: (staleReconcilingCount || 0) > 0 ? "bad" : "neutral",
      tipKey: "tipStaleReconciling",
      sub: (staleReconcilingCount || 0) > 0 ? (t("metricAuditRequired") || "需要审计") : (t("metricNoStale") || "无超时")
    });
  }

  const rail = metricRail(metricItems);
  container.appendChild(rail);

  // Section 2: Flat Asymmetric Dual-Column Composition (Studies Catalog + Flat Operational Rail)
  const layoutGrid = el("div", "overview-layout-grid");

  // Left Column: Studies Catalog
  const mainCol = el("div", "overview-main-col");
  const studiesSection = el("section", "rule-section overview-studies-section");

  // Filter Bar & Technical Control Strip
  const filterRow = el("div", "section-filter-bar");
  const sectionTitleWrap = el("div", "section-title-wrap");
  sectionTitleWrap.appendChild(el("h2", "section-title", txt(t("overviewStudiesTitle") || "研究列表")));
  filterRow.appendChild(sectionTitleWrap);

  // Filter Controls
  const filterControls = el("div", "filter-controls-wrap");

  const filterIn = el("input", {
    type: "text",
    placeholder: t("filterPlaceholder") || "过滤 study_id / problem_id …",
    value: textFilter,
    "aria-label": t("filterAria") || "Filter studies"
  });
  filterIn.oninput = () => {
    textFilter = filterIn.value.trim().toLowerCase();
    renderStudiesTable();
  };
  filterControls.appendChild(filterIn);

  const activeLabel = el("label", "active-filter-label");
  const activeChk = el("input", { type: "checkbox", checked: activeOnlyFilter });
  activeChk.onchange = () => {
    activeOnlyFilter = activeChk.checked;
    renderStudiesTable();
  };
  activeLabel.appendChild(activeChk);
  activeLabel.appendChild(txt(t("activeOnly") || "仅活跃"));
  filterControls.appendChild(activeLabel);

  const countSummary = el("span", "count-summary meta mono");
  filterControls.appendChild(countSummary);

  filterRow.appendChild(filterControls);
  studiesSection.appendChild(filterRow);

  // Table Container
  const tableContainer = el("div", "overview-table-container");
  studiesSection.appendChild(tableContainer);
  mainCol.appendChild(studiesSection);
  layoutGrid.appendChild(mainCol);

  // Right Column: Flat Shared-Spine Operational Rail (No stacked card boxes)
  const sideCol = el("aside", "overview-side-col", { "aria-label": t("overviewClusterState") || "Operational Context" });
  const sideRail = el("div", "overview-side-rail");

  // Section 1: Service State
  const secHealth = el("section", "side-rail-section");
  secHealth.appendChild(el("h3", "side-rail-heading", txt(t("overviewClusterState") || "服务状态")));

  const healthRow = el("div", "side-rail-status-row");
  const healthStatus = renderHealthIndicator(state.healthOk);
  healthRow.appendChild(healthStatus);

  const modeBadge = renderModeBadge(state.writes);
  healthRow.appendChild(modeBadge);
  secHealth.appendChild(healthRow);

  secHealth.appendChild(el("div", "side-rail-sub meta", txt(t("overviewTelemetryHealthy") || "集群状态正常 · 实时同步中")));
  sideRail.appendChild(secHealth);

  // Section 2: Queue & Capacity
  const secCapacity = el("section", "side-rail-section");
  secCapacity.appendChild(el("h3", "side-rail-heading", txt(t("overviewCapacityMetrics") || "队列与容量")));

  const capList = el("div", "side-rail-kv-list");

  const r1 = el("div", "side-rail-kv-item");
  r1.appendChild(el("span", "kv-key", txt(t("overviewActiveAllocations") || "活跃任务分配")));
  r1.appendChild(el("span", "kv-val mono", txt(String(allocations.length))));
  capList.appendChild(r1);

  const r2 = el("div", "side-rail-kv-item");
  r2.appendChild(el("span", "kv-key", txt(t("statQueued") || "排队中")));
  r2.appendChild(el("span", `kv-val mono ${queuedCount > 0 ? "tone-warn-text" : ""}`, txt(String(queuedCount))));
  capList.appendChild(r2);

  const r3 = el("div", "side-rail-kv-item");
  r3.appendChild(el("span", "kv-key", txt(t("overviewSolverSlots") || "求解器调度槽位")));
  r3.appendChild(el("span", "kv-val mono", txt(t("overviewMaxSlotsVal") || "上限 4")));
  capList.appendChild(r3);

  secCapacity.appendChild(capList);
  sideRail.appendChild(secCapacity);

  sideCol.appendChild(sideRail);
  layoutGrid.appendChild(sideCol);
  container.appendChild(layoutGrid);

  // Section 3: Graphic Baseline Termination Footer (Anchors page rhythm)
  const baselineFooter = el("footer", "overview-baseline-footer", { role: "contentinfo" });
  const footerRow = el("div", "baseline-meta-row");
  footerRow.appendChild(el("span", "baseline-meta meta mono", txt(t("overviewBaselineMeta") || "科研评测控制台 · SQLite 控制数据库")));
  footerRow.appendChild(el("span", "baseline-telemetry meta mono", txt(`${t("overviewLiveSync") || "实时同步: 正常"} · ${fmtClockTime(new Date())}`)));
  baselineFooter.appendChild(footerRow);
  container.appendChild(baselineFooter);

  function renderStudiesTable() {
    tableContainer.textContent = "";

    const filtered = studies.filter(s => {
      if (activeOnlyFilter && !s.active_count && !s.waiting_count) return false;
      if (!textFilter) return true;
      const sid = (s.study_id || "").toLowerCase();
      const pid = (s.problem_id || "").toLowerCase();
      return sid.includes(textFilter) || pid.includes(textFilter);
    });

    countSummary.textContent = t("studyCountSummary", { shown: filtered.length, total: studies.length });

    if (studies.length === 0) {
      tableContainer.appendChild(emptyPanel(t("noStudiesTitle") || "暂无研究", t("noStudiesDesc") || "控制数据库中还没有任何 study。"));
      return;
    }
    if (filtered.length === 0) {
      tableContainer.appendChild(emptyPanel(t("noMatchingStudiesTitle") || "没有匹配的研究", t("noMatchingStudiesDesc", { filter: textFilter || (t("activeOnly") || "仅活跃") })));
      return;
    }

    const tableWrap = el("div", "table-dense-wrap");
    const table = el("table", "data-table table-dense");
    const thead = el("thead");
    const trH = el("tr");
    trH.appendChild(el("th", "", txt(t("thStudyId") || "study_id"), tip(null, "tipStudyId")));
    trH.appendChild(el("th", "", txt(t("thProblem") || "problem"), tip(null, "tipProblem")));
    trH.appendChild(el("th", "num", txt(t("thEvalCount") || "评测数"), tip(null, "tipEvalCount")));
    trH.appendChild(el("th", "", txt(t("thStatusDist") || "状态分布"), tip(null, "tipStatusDist")));
    trH.appendChild(el("th", "", txt(t("thWaiting") || "等待状态"), tip(null, "tipWaiting")));
    trH.appendChild(el("th", "", txt(t("thLastActivity") || "最近活动"), tip(null, "tipLastActivity")));
    trH.appendChild(el("th", "", txt(t("thCreatedAt") || "创建时间"), tip(null, "tipCreatedAt")));
    thead.appendChild(trH);
    table.appendChild(thead);

    const tbody = el("tbody");
    filtered.forEach(s => {
      const tr = el("tr");

      // Study ID (Monospace bold, clickable with tooltip)
      const tdSid = el("td", "col-study-id");
      const sidLink = el("a", "mono mono-study-id", {
        href: `#/study/${encodeURIComponent(s.study_id)}`,
        title: s.study_id
      }, txt(s.study_id));
      tdSid.appendChild(sidLink);
      tr.appendChild(tdSid);

      // Problem ID + safe revision (Monospace truncated, zero horizontal overflow)
      const tdPid = el("td", "col-problem-id");
      if (s.problem_id) {
        tdPid.appendChild(el("a", "mono mono-problem-id", {
          href: `#/problem/${encodeURIComponent(s.problem_id)}`,
          title: s.problem_id
        }, txt(s.problem_id)));
        if (s.problem_revision !== undefined && s.problem_revision !== null && s.problem_revision !== "") {
          const revSpan = monoHash(s.problem_revision, { len: 12, prefix: " @ " });
          tdPid.appendChild(revSpan);
        }
      } else {
        tdPid.appendChild(el("span", "dim", "—"));
      }
      tr.appendChild(tdPid);

      // Evaluation Count (Right-aligned tabular numeral)
      tr.appendChild(el("td", "num mono", txt(s.evaluation_count !== undefined ? s.evaluation_count : "—")));

      // Status Distribution Chips
      const tdDist = el("td", "col-status-dist");
      const counts = s.status_counts || {};
      const chipsWrap = el("div", "status-dist-wrap");
      Object.keys(counts).forEach(st => {
        if (counts[st] > 0) {
          let tone = "neutral";
          if (st === "qualified" || st === "completed") tone = "ok";
          else if (st === "running" || st === "qualifying") tone = "info";
          else if (st === "queued" || st === "recovering" || st === "reconciling") tone = "warn";
          else if (st === "failed" || st === "unresolved") tone = "bad";
          else if (st === "cancelled") tone = "dim";
          chipsWrap.appendChild(chip(tone, `${st}: ${counts[st]}`));
        }
      });
      if (chipsWrap.children.length === 0) chipsWrap.appendChild(el("span", "dim sub", txt(t("noEvals") || "无评测")));
      tdDist.appendChild(chipsWrap);
      tr.appendChild(tdDist);

      // Waiting status column
      const tdWait = el("td", "col-waiting");
      if (s.waiting_count > 0) {
        tdWait.appendChild(el("span", "waiting-badge tone-warn", txt(t("waitingCount", { count: s.waiting_count }) || `${s.waiting_count} waiting`)));
        if (s.oldest_wait && s.oldest_wait.wait_reason) {
          tdWait.appendChild(el("div", "sub dim truncate-text", { title: s.oldest_wait.wait_reason }, txt(s.oldest_wait.wait_reason)));
        }
      } else {
        tdWait.appendChild(el("span", "dim", txt(t("noWaiting") || "无等待")));
      }
      tr.appendChild(tdWait);

      // Last activity column
      tr.appendChild(el("td", "sub col-last-activity", txt(s.last_activity_at ? fmtRelativeTime(s.last_activity_at) : "—")));

      // Created at column
      tr.appendChild(el("td", "sub col-created-at", txt(s.created_at ? fmtDate(new Date(s.created_at)) : "—")));

      tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    tableWrap.appendChild(table);
    tableContainer.appendChild(tableWrap);
  }

  renderStudiesTable();
  return container;
}
