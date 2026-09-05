/**
 * Algorithms List & Detail Views · Swiss Technical Typography
 *
 * Doctrine 6: Primary = Optimization run lifecycle & state (ID, status, algorithm configuration & linked studies); Sectioned = Historical event sequence timeline; Deferred = Final result artifacts table.
 *
 * Vocabulary & Doctrine:
 * - Asymmetrical rule-delimited metric rail with primary run counters
 * - Rule-delimited catalog sections and dense data tables
 * - Monospace run ID and hash truncation with zero horizontal overflow
 * - Flat metadata summary rail without nested card wrappers
 * - Full dual-locale support (zh / en)
 */

import { t, fmtRelativeTime, fmtDate, fmtClockTime } from "../i18n.js";
import { entityName } from "../display.js";
import { state } from "../state.js";
import { el, txt, pageHead, emptyPanel, errorBlock, metricRail, chip, statusPill, tip, monoHash, technicalDetails } from "../ui.js";
import { fetchJSON } from "../api.js";
import { navigate } from "../router.js";

let textFilter = "";
let activeOnlyFilter = false;

export async function renderAlgorithmsView({ id } = {}) {
  const container = el("div", "algorithms-view");

  if (id) {
    return renderAlgorithmDetail(id, container);
  }

  // Fetch algorithm runs
  const r = await fetchJSON("/api/algorithms");
  const data = (r && r.ok && r.data) ? r.data : (state.algorithms || {});
  state.algorithms = data;

  const runs = data.algorithms || data.runs || data.algorithm_runs || (Array.isArray(data) ? data : []);

  // Page Header
  const head = pageHead(t("algorithmsTitle") || "算法", t("algorithmsDesc") || "算法运行与优化流水线列表");
  container.appendChild(head);

  // Section 1: Asymmetrical Rule-Delimited Metric Rail (Doctrine 2 / Doctrine 6)
  const totalCount = data.algorithm_count !== undefined ? data.algorithm_count : runs.length;
  const activeCount = runs.filter(x => (x.status || "").toLowerCase() === "active" || (x.status || "").toLowerCase() === "running").length;
  const completedCount = runs.filter(x => (x.status || "").toLowerCase() === "completed" || (x.status || "").toLowerCase() === "qualified").length;
  const blockedCount = runs.filter(x => ["blocked", "failed", "unresolved"].includes((x.status || "").toLowerCase())).length;

  const runsSub = totalCount === 1
    ? (t("metricActiveAlgoSingle") || "1 项活跃运行")
    : totalCount > 0
      ? (t("metricActiveAlgoPlural", { n: totalCount }) || `${totalCount} 项运行`)
      : (t("metricZeroRegistered") || "0 项已登记");

  const metricItems = [
    {
      primary: true,
      label: t("statAlgoRuns") || "运行总数",
      val: totalCount,
      tipKey: "tipRunId",
      sub: runsSub
    },
    {
      label: t("statAlgoActive") || "活跃运行",
      val: activeCount,
      dotTone: activeCount > 0 ? "ok" : null,
      tone: activeCount > 0 ? "ok" : "neutral",
      sub: activeCount > 0 ? (t("metricActiveAlgoPlural", { n: activeCount }) || `${activeCount} 项活跃`) : (t("metricZeroRegistered") || "无活跃任务")
    },
    {
      label: t("statAlgoCompleted") || "已完成",
      val: completedCount,
      dotTone: completedCount > 0 ? "info" : null,
      tone: "neutral",
      sub: completedCount > 0 ? (t("metricCompletedSub", { n: completedCount }) || `${completedCount} 项已达标`) : (t("metricZeroCompleted") || "0 项完成")
    },
    {
      label: t("statAlgoBlocked") || "受阻/失败",
      val: blockedCount,
      dotTone: blockedCount > 0 ? "bad" : null,
      tone: blockedCount > 0 ? "bad" : "neutral",
      sub: blockedCount > 0 ? (t("metricAuditRequired") || "需要审计") : (t("metricZeroBlocked") || "无阻塞异常")
    }
  ];

  const rail = metricRail(metricItems);
  container.appendChild(rail);

  // Section 2: Rule-Delimited Catalog Section
  const catalogSection = el("section", "rule-section algorithms-catalog-section");

  // Filter Bar
  const filterRow = el("div", "section-filter-bar");
  const sectionTitleWrap = el("div", "section-title-wrap");
  sectionTitleWrap.appendChild(el("h2", "section-title", txt(t("algorithmsCatalogTitle") || "算法运行目录")));
  filterRow.appendChild(sectionTitleWrap);

  const filterControls = el("div", "filter-controls-wrap");

  const filterIn = el("input", {
    type: "text",
    placeholder: t("filterAlgoPlaceholder") || "过滤 run_id / algorithm_id / problem_id …",
    value: textFilter,
    "aria-label": t("filterAlgoAria") || "Filter algorithm runs"
  });
  filterIn.oninput = () => {
    textFilter = filterIn.value.trim().toLowerCase();
    renderRunsTable();
  };
  filterControls.appendChild(filterIn);

  const activeLabel = el("label", "active-filter-label");
  const activeChk = el("input", { type: "checkbox", checked: activeOnlyFilter });
  activeChk.onchange = () => {
    activeOnlyFilter = activeChk.checked;
    renderRunsTable();
  };
  activeLabel.appendChild(activeChk);
  activeLabel.appendChild(txt(t("activeOnly") || "仅活跃"));
  filterControls.appendChild(activeLabel);

  const countSummary = el("span", "count-summary meta mono");
  filterControls.appendChild(countSummary);

  filterRow.appendChild(filterControls);
  catalogSection.appendChild(filterRow);

  // Table Container
  const tableContainer = el("div", "algorithms-table-container");
  catalogSection.appendChild(tableContainer);
  container.appendChild(catalogSection);

  // Baseline Footer Termination
  const baselineFooter = el("footer", "overview-baseline-footer", { role: "contentinfo" });
  const footerRow = el("div", "baseline-meta-row");
  footerRow.appendChild(el("span", "baseline-meta meta mono", txt(t("algorithmsBaselineMeta") || "科研评测控制台 · 算法流水线与执行时序")));
  footerRow.appendChild(el("span", "baseline-telemetry meta mono", txt(`${t("overviewLiveSync") || "实时同步: 正常"} · ${fmtClockTime(new Date())}`)));
  baselineFooter.appendChild(footerRow);
  container.appendChild(baselineFooter);

  function renderRunsTable() {
    tableContainer.textContent = "";

    const filtered = runs.filter(run => {
      const isAct = ["active", "running", "qualifying"].includes(String(run.status || "").toLowerCase());
      if (activeOnlyFilter && !isAct) return false;
      if (!textFilter) return true;
      const rid = String(run.algorithm_run_id || "").toLowerCase();
      const aid = String(run.algorithm_id || "").toLowerCase();
      const pid = String(run.problem_id || "").toLowerCase();
      return rid.includes(textFilter) || aid.includes(textFilter) || pid.includes(textFilter);
    });

    countSummary.textContent = t("algoCountSummary", { shown: filtered.length, total: runs.length });

    if (runs.length === 0) {
      tableContainer.appendChild(emptyPanel(t("noAlgorithmsTitle") || "暂无算法运行", t("noAlgorithmsDesc") || "控制数据库中尚未登记任何算法运行。"));
      return;
    }

    if (filtered.length === 0) {
      tableContainer.appendChild(emptyPanel(t("noMatchingAlgorithmsTitle") || "没有匹配的算法运行", t("noMatchingAlgorithmsDesc", { filter: textFilter || (t("activeOnly") || "仅活跃") })));
      return;
    }

    const tableWrap = el("div", "table-dense-wrap");
    const table = el("table", "data-table table-dense");
    const thead = el("thead");
    const trH = el("tr");
    trH.appendChild(el("th", "", txt(t("thRunId") || "运行标识"), tip(null, "tipRunId")));
    trH.appendChild(el("th", "", txt(t("thAlgorithm") || "算法"), tip(null, "tipAlgorithm")));
    trH.appendChild(el("th", "", txt(t("thStatus") || "状态")));
    trH.appendChild(el("th", "num", txt(t("thStudyCount") || "关联研究"), tip(null, "tipStudyCount")));
    trH.appendChild(el("th", "num", txt(t("thEventsResults") || "事件 / 结果"), tip(null, "tipEventsResults")));
    trH.appendChild(el("th", "", txt(t("thLatestEvent") || "最新事件"), tip(null, "tipLatestEvent")));
    trH.appendChild(el("th", "num", txt(t("thActions") || "操作")));
    thead.appendChild(trH);
    table.appendChild(thead);

    const tbody = el("tbody");
    filtered.forEach(run => {
      const tr = el("tr");

      // Run ID (Monospace link, safe width)
      const tdId = el("td", "col-run-id");
      const rid = run.algorithm_run_id || "—";
      const aLink = el("a", "mono mono-run-id", {
        href: `#/algorithm/${encodeURIComponent(rid)}`,
        title: rid
      }, txt(entityName(rid)));
      tdId.appendChild(aLink);
      tr.appendChild(tdId);

      // Algorithm ID + Revision
      const tdAlgo = el("td", "col-algo-id");
      if (run.algorithm_id) {
        const algoWrap = el("span", "algo-id-wrap");
        const algoSpan = el("span", "mono mono-algo-id", {
          title: run.algorithm_id
        }, txt(entityName(run.algorithm_id)));
        algoWrap.appendChild(algoSpan);
        if (run.algorithm_revision) {
          algoWrap.appendChild(monoHash(run.algorithm_revision, { len: 10, prefix: " @ " }));
        }
        tdAlgo.appendChild(algoWrap);
      } else {
        tdAlgo.appendChild(el("span", "dim", "—"));
      }
      tr.appendChild(tdAlgo);

      // Status Pill
      tr.appendChild(el("td", "", statusPill(run.status || "active")));

      // Studies Count (Right-aligned)
      const studyCount = run.study_count !== undefined ? run.study_count : ((run.study_ids || []).length || 0);
      tr.appendChild(el("td", "num mono", txt(String(studyCount))));

      // Events / Results count
      const eCount = run.event_count || (Array.isArray(run.events) ? run.events.length : 0);
      const rCount = run.result_count || (Array.isArray(run.results) ? run.results.length : 0);
      tr.appendChild(el("td", "num mono", txt(`${eCount} / ${rCount}`)));

      // Latest Event Time
      const timeVal = run.latest_event_at || run.created_at;
      tr.appendChild(el("td", "sub", txt(timeVal ? fmtRelativeTime(timeVal) : "—")));

      // Actions
      const tdAct = el("td", "num");
      const viewBtn = el("button", "plain", {
        style: "padding: 2px 6px; font-size: 11px;",
        onclick: () => navigate(`#/algorithm/${encodeURIComponent(rid)}`)
      }, "🔍 " + (t("btnViewDetail") || "详情"));
      tdAct.appendChild(viewBtn);
      tr.appendChild(tdAct);

      tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    tableWrap.appendChild(table);
    tableContainer.appendChild(tableWrap);
  }

  renderRunsTable();
  return container;
}

/**
 * Algorithm Detail View
 * Primary: Optimization Run Lifecycle & State (Run ID, status, algorithm configuration & linked studies)
 * Sectioned: Historical Event Sequence Timeline
 * Deferred: Final Result Artifacts Table (rendered when results exist)
 */
async function renderAlgorithmDetail(aid, container) {
  const head = pageHead(t("algorithmTitle", { id: aid }) || `算法运行 ${aid}`, t("algorithmDesc") || "算法运行详情与事件/结果时序", [
    el("button", "plain", { onclick: () => navigate("#/algorithms") }, "⬅️ " + (t("btnBackToList") || "返回列表"))
  ]);
  container.appendChild(head);

  const r = await fetchJSON(`/api/algorithms/${encodeURIComponent(aid)}`);
  if (!r || !r.ok || !r.data) {
    container.appendChild(errorBlock((t("statusError", { status: r ? r.status : 404 }) || "加载失败: ") + ((r && r.data && r.data.error) || t("netError") || "网络错误"), t("generalErrorHint") || "请检查网络或刷新页面"));
    return container;
  }

  const data = r.data;
  const run = data.algorithm || data.run || data;
  const events = data.events || data.algorithm_events || (Array.isArray(run.events) ? run.events : []);
  const results = data.results || data.algorithm_results || (Array.isArray(run.results) ? run.results : []);

  state.algorithm = data;
  state.algorithmId = aid;

  // Metadata Strip (Flat Rule-Delimited KV Rail)
  const metaStrip = el("div", "detail-meta-strip");
  const metaGrid = el("div", "detail-kv-grid");

  // Run ID
  const rId = el("div", "detail-kv-item");
  rId.appendChild(el("span", "kv-key", txt(t("metaRunId") || "运行标识: ")));
  rId.appendChild(el("span", "kv-val mono", { style: "font-weight: 600;" }, txt(aid)));
  metaGrid.appendChild(rId);

  // Algorithm
  const rAlgo = el("div", "detail-kv-item");
  rAlgo.appendChild(el("span", "kv-key", txt(t("metaAlgorithm") || "算法标识: ")));
  if (run.algorithm_id) {
    const algoWrap = el("span", "kv-val mono", txt(entityName(run.algorithm_id)));
    if (run.algorithm_revision) {
      algoWrap.appendChild(monoHash(run.algorithm_revision, { len: 12, prefix: " @ " }));
    }
    rAlgo.appendChild(algoWrap);
  } else {
    rAlgo.appendChild(el("span", "kv-val dim", "—"));
  }
  metaGrid.appendChild(rAlgo);

  // Status
  const rStatus = el("div", "detail-kv-item");
  rStatus.appendChild(el("span", "kv-key", txt(t("thStatus") || "状态: ")));
  rStatus.appendChild(el("span", "kv-val", statusPill(run.status || "active")));
  metaGrid.appendChild(rStatus);

  // Problem ID
  const rProb = el("div", "detail-kv-item");
  rProb.appendChild(el("span", "kv-key", txt(t("metaProblem") || "关联问题: ")));
  if (run.problem_id) {
    rProb.appendChild(el("span", "kv-val", el("a", "mono", { href: `#/problem/${encodeURIComponent(run.problem_id)}` }, txt(run.problem_id))));
  } else {
    rProb.appendChild(el("span", "kv-val dim", "—"));
  }
  metaGrid.appendChild(rProb);

  // Retention Class
  const rRet = el("div", "detail-kv-item");
  rRet.appendChild(el("span", "kv-key", txt(t("metaRetention") || "保留策略: ")));
  rRet.appendChild(el("span", "kv-val", chip("info", run.retention_class || "standard")));
  metaGrid.appendChild(rRet);

  // Created At
  if (run.created_at) {
    const rCreated = el("div", "detail-kv-item");
    rCreated.appendChild(el("span", "kv-key", txt(t("metaCreated") || "创建时间: ")));
    rCreated.appendChild(el("span", "kv-val sub", txt(fmtDate(new Date(run.created_at)))));
    metaGrid.appendChild(rCreated);
  }

  metaStrip.appendChild(metaGrid);
  container.appendChild(metaStrip);

  // Linked Studies Section
  const studies = run.study_ids || [];
  const studiesSection = el("section", "rule-section linked-studies-section");
  const sHead = el("div", "section-filter-bar");
  sHead.appendChild(el("h3", "section-title", txt(`${t("sectionLinkedStudies") || "关联研究"} (${studies.length})`)));
  studiesSection.appendChild(sHead);

  if (studies.length === 0) {
    studiesSection.appendChild(el("div", "dim sub", { style: "padding: 8px 0;" }, txt(t("noLinkedStudies") || "暂无关联的 Study 实验")));
  } else {
    const sWrap = el("div", { style: "display: flex; gap: 8px; flex-wrap: wrap; padding: 6px 0;" });
    studies.forEach(sid => {
      const chipLink = el("a", "chip tone-neutral mono", {
        href: `#/study/${encodeURIComponent(sid)}`,
        style: "text-decoration: none; cursor: pointer;"
      }, "🔬 " + sid);
      sWrap.appendChild(chipLink);
    });
    studiesSection.appendChild(sWrap);
  }
  container.appendChild(studiesSection);

  // Configuration Section (Only rendered if configuration is present)
  if (run.configuration && Object.keys(run.configuration).length > 0) {
    const cfgSection = el("section", "rule-section algo-config-section");
    const cfgHead = el("div", "section-filter-bar");
    cfgHead.appendChild(el("h3", "section-title", txt(t("sectionConfiguration") || "运行配置")));
    cfgSection.appendChild(cfgHead);

    const preWell = el("pre", "log-well mono", txt(JSON.stringify(run.configuration, null, 2)));
    cfgSection.appendChild(technicalDetails(t("technicalDetails"), preWell));
    container.appendChild(cfgSection);
  }

  // Section 3: Event Sequence Timeline (Sectioned Dataset under Doctrine 6)
  const evSection = el("section", "rule-section algo-events-section");
  const evHead = el("div", "section-filter-bar");
  evHead.appendChild(el("h3", "section-title", txt(`${t("sectionEvents") || "算法事件时序"} (${events.length})`)));
  evSection.appendChild(evHead);

  if (events.length === 0) {
    evSection.appendChild(emptyPanel(t("noEventsTitle") || "暂无事件记录", t("noEventsDesc") || "该算法运行尚未记录任何事件。"));
  } else {
    const evWrap = el("div", "table-dense-wrap");
    const evTable = el("table", "data-table table-dense");
    const evThead = el("thead");
    const evTrH = el("tr");
    evTrH.appendChild(el("th", "", txt(t("thSeq") || "#")));
    evTrH.appendChild(el("th", "", txt(t("thEventKey") || "事件标识")));
    evTrH.appendChild(el("th", "", txt(t("thEventType") || "事件类型")));
    evTrH.appendChild(el("th", "", txt(t("thRunStatus") || "运行状态")));
    evTrH.appendChild(el("th", "", txt(t("thPayload") || "负载数据")));
    evTrH.appendChild(el("th", "", txt(t("thTime") || "记录时间")));
    evThead.appendChild(evTrH);
    evTable.appendChild(evThead);

    const evTbody = el("tbody");
    events.forEach(e => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono sub num", txt(e.sequence !== undefined ? String(e.sequence) : "#")));
      tr.appendChild(el("td", "mono", { style: "font-weight: 600;" }, monoHash(e.event_key, { len: 20 })));
      tr.appendChild(el("td", "", chip("info", e.event_type || "event")));
      tr.appendChild(el("td", "", statusPill(e.run_status || run.status)));
      const payloadStr = JSON.stringify(e.payload || {});
      tr.appendChild(el("td", "", technicalDetails(t("technicalDetails"), el("pre", "technical-code", payloadStr))));
      tr.appendChild(el("td", "sub", txt(e.created_at ? fmtRelativeTime(e.created_at) : "—")));
      evTbody.appendChild(tr);
    });
    evTable.appendChild(evTbody);
    evWrap.appendChild(evTable);
    evSection.appendChild(evWrap);
  }
  container.appendChild(evSection);

  // Section 4: Results Section (Deferred / Scoped under Doctrine 6)
  if (results && results.length > 0) {
    const resSection = el("section", "rule-section algo-results-section");
    const resHead = el("div", "section-filter-bar");
    resHead.appendChild(el("h3", "section-title", txt(`${t("sectionResults") || "产出结果"} (${results.length})`)));
    resSection.appendChild(resHead);

    const resWrap = el("div", "table-dense-wrap");
    const resTable = el("table", "data-table table-dense");
    const rThead = el("thead");
    const rTrH = el("tr");
    rTrH.appendChild(el("th", "", txt(t("thResultId") || "结果标识")));
    rTrH.appendChild(el("th", "", txt(t("thResultType") || "结果类型")));
    rTrH.appendChild(el("th", "", txt(t("thPayload") || "负载数据")));
    rThead.appendChild(rTrH);
    resTable.appendChild(rThead);

    const rTbody = el("tbody");
    results.forEach(res => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", { style: "font-weight: 600;" }, monoHash(res.algorithm_result_id, { len: 24 })));
      tr.appendChild(el("td", "", chip("ok", res.result_type || "optimal-parameter-set")));
      const resPayloadStr = JSON.stringify(res.payload || {});
      tr.appendChild(el("td", "", technicalDetails(t("technicalDetails"), el("pre", "technical-code", resPayloadStr))));
      rTbody.appendChild(tr);
    });
    resTable.appendChild(rTbody);
    resWrap.appendChild(resTable);
    resSection.appendChild(resWrap);
    container.appendChild(resSection);
  }

  // Baseline Footer Termination
  const baselineFooter = el("footer", "overview-baseline-footer", { role: "contentinfo" });
  const footerRow = el("div", "baseline-meta-row");
  footerRow.appendChild(el("span", "baseline-meta meta mono", txt(t("algorithmsBaselineMeta") || "科研评测控制台 · 算法流水线与执行时序")));
  footerRow.appendChild(el("span", "baseline-telemetry meta mono", txt(`${t("overviewLiveSync") || "实时同步: 正常"} · ${fmtClockTime(new Date())}`)));
  baselineFooter.appendChild(footerRow);
  container.appendChild(baselineFooter);

  return container;
}
