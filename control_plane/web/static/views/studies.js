/**
 * Studies List & Detail Views · Swiss Technical Typography
 * Stage 4 in the 5-stage composition pipeline.
 *
 * Vocabulary & Doctrine:
 * - Rule-delimited catalog sections and dense data tables
 * - Monospace study/candidate ID truncation with zero horizontal overflow
 * - Flat metadata summary rail without nested card wrappers
 * - Interactive status filtering and attempt inspection in flat inset wells
 */

import { t, fmtRelativeTime, fmtDate } from "../i18n.js";
import { state } from "../state.js";
import { el, txt, pageHead, emptyPanel, errorBlock, chip, statusPill, tip, monoHash } from "../ui.js";
import { fetchJSON } from "../api.js";
import { navigate } from "../router.js";

export async function renderStudiesView({ id, isEmbedded = false } = {}) {
  const container = el("div", "studies-view");

  if (id) {
    return renderStudyDetail(id, container);
  }

  // Header only when rendered as standalone page
  if (!isEmbedded) {
    const head = pageHead(t("studiesTitle"), t("studiesDesc"), [
      el("button", "plain primary", {
        onclick: () => navigate("#/compose?step=4")
      }, "➕ " + t("submitCardStudy"))
    ]);
    container.appendChild(head);
  }

  const catalogSection = el("section", "rule-section studies-catalog-section");
  if (isEmbedded) {
    const catalogHead = el("div", "section-filter-bar");
    catalogHead.appendChild(el("h3", "section-title", txt(t("catalogStudiesTitle"))));
    catalogSection.appendChild(catalogHead);
  }

  const r = await fetchJSON("/api/studies");
  let studies = (r && r.ok && r.data && (r.data.items || r.data.studies || (Array.isArray(r.data) ? r.data : []))) || [];

  if (studies.length === 0 && state.studiesList.length > 0) {
    studies = state.studiesList;
  }
  state.studiesList = studies;

  if (studies.length === 0) {
    catalogSection.appendChild(emptyPanel(t("noStudiesTitle"), t("noStudiesDesc")));
    container.appendChild(catalogSection);
    return container;
  }

  const tableWrap = el("div", "table-dense-wrap");
  const table = el("table", "data-table table-dense");
  const thead = el("thead");
  const trH = el("tr");
  trH.appendChild(el("th", "", txt(t("thStudyId"))));
  trH.appendChild(el("th", "", txt(t("thProblem"))));
  trH.appendChild(el("th", "", txt(t("thRevision"))));
  trH.appendChild(el("th", "", txt(t("thAutomationProfile"))));
  trH.appendChild(el("th", "num", txt(t("thActions"))));
  thead.appendChild(trH);
  table.appendChild(thead);

  const tbody = el("tbody");
  studies.forEach(s => {
    const sid = s.study_id || s.id || (typeof s === "string" ? s : "—");
    const pid = s.problem_id || "—";
    const rev = s.problem_revision !== undefined ? s.problem_revision : "—";
    const auto = s.automation_profile || "standard";

    const tr = el("tr");
    const tdId = el("td", "col-study-id");
    const aId = el("a", "mono mono-study-id", {
      href: `#/study/${encodeURIComponent(sid)}`,
      style: "font-weight: 600;"
    }, txt(sid));
    tdId.appendChild(aId);
    tr.appendChild(tdId);

    const tdProb = el("td", "col-problem-id");
    if (pid !== "—") {
      tdProb.appendChild(el("a", "mono mono-problem-id", {
        href: `#/problem/${encodeURIComponent(pid)}`
      }, txt(pid)));
    } else {
      tdProb.appendChild(el("span", "dim", "—"));
    }
    tr.appendChild(tdProb);

    tr.appendChild(el("td", "mono sub", monoHash(rev, { len: 12 })));
    tr.appendChild(el("td", "", chip("info", auto)));

    const tdAct = el("td", "num");
    const actGroup = el("div", { style: "display: inline-flex; gap: 5px; align-items: center;" });

    const viewBtn = el("button", "plain", {
      style: "padding: 2px 6px; font-size: 11px;",
      onclick: () => navigate(`#/study/${encodeURIComponent(sid)}`)
    }, "🔍 " + t("btnViewQueueDetail"));

    const evalBtn = el("button", "plain primary", {
      style: "padding: 2px 6px; font-size: 11px;",
      onclick: () => {
        state.candidateDesigner.studyId = sid;
        state.candidateDesigner.problemId = pid;
        navigate("#/compose?step=5");
      }
    }, "⚡ " + t("btnSubmitEval"));

    actGroup.appendChild(viewBtn);
    actGroup.appendChild(evalBtn);
    tdAct.appendChild(actGroup);
    tr.appendChild(tdAct);

    tbody.appendChild(tr);
  });

  table.appendChild(tbody);
  tableWrap.appendChild(table);
  catalogSection.appendChild(tableWrap);
  container.appendChild(catalogSection);

  return container;
}

/**
 * Study Detail View (Flat rule-delimited layout with evaluation queue & status filter)
 */
async function renderStudyDetail(sid, container) {
  const head = pageHead(t("studyTitle", { id: sid }), t("studyDesc"), [
    el("button", "plain", { onclick: () => navigate("#/compose?step=4") }, "⬅️ " + t("btnBackToList")),
    el("button", "plain primary", {
      onclick: () => {
        state.candidateDesigner.studyId = sid;
        if (state.study && state.study.study && state.study.study.problem_id) {
          state.candidateDesigner.problemId = state.study.study.problem_id;
        }
        navigate("#/compose?step=5");
      }
    }, t("btnSubmitEvalToStudy"))
  ]);
  container.appendChild(head);

  const r = await fetchJSON(`/api/studies/${encodeURIComponent(sid)}`);
  if (!r || !r.ok || !r.data) {
    container.appendChild(errorBlock(t("statusError", { status: r ? r.status : 404 }) + ((r && r.data && r.data.error) || t("netError")), t("generalErrorHint")));
    return container;
  }

  const data = r.data;
  const s = data.study || {};
  const evals = data.evaluations || [];
  state.study = data;
  state.studyId = sid;

  // Metadata Strip (Flat KV Rail)
  const metaStrip = el("div", "detail-meta-strip");
  const metaGrid = el("div", "detail-kv-grid");

  const rId = el("div", "detail-kv-item");
  rId.appendChild(el("span", "kv-key", txt(t("fieldStudyId"))));
  rId.appendChild(el("span", "kv-val mono", txt(sid)));
  metaGrid.appendChild(rId);

  const rProb = el("div", "detail-kv-item");
  rProb.appendChild(el("span", "kv-key", txt(t("metaProblem"))));
  if (s.problem_id) {
    const probWrap = el("span", "kv-val");
    probWrap.appendChild(el("a", "mono", { href: `#/problem/${encodeURIComponent(s.problem_id)}` }, txt(s.problem_id)));
    if (s.problem_revision !== undefined) {
      probWrap.appendChild(el("span", "dim sub", txt(` @ `)));
      probWrap.appendChild(monoHash(s.problem_revision, { len: 12 }));
    }
    rProb.appendChild(probWrap);
  } else {
    rProb.appendChild(el("span", "kv-val dim", "—"));
  }
  metaGrid.appendChild(rProb);

  const rProf = el("div", "detail-kv-item");
  rProf.appendChild(el("span", "kv-key", txt(t("metaProfile"))));
  rProf.appendChild(el("span", "kv-val", chip("info", s.automation_profile || "standard")));
  metaGrid.appendChild(rProf);

  if (s.algorithm_run_id) {
    const rRun = el("div", "detail-kv-item");
    rRun.appendChild(el("span", "kv-key", txt(t("metaRun"))));
    rRun.appendChild(el("span", "kv-val", el("a", "mono mono-run-id", { href: `#/algorithm/${encodeURIComponent(s.algorithm_run_id)}`, title: s.algorithm_run_id }, txt(s.algorithm_run_id))));
    metaGrid.appendChild(rRun);
  }

  if (s.created_at) {
    const rCreated = el("div", "detail-kv-item");
    rCreated.appendChild(el("span", "kv-key", txt(t("metaCreated"))));
    rCreated.appendChild(el("span", "kv-val sub", txt(fmtDate(new Date(s.created_at)))));
    metaGrid.appendChild(rCreated);
  }

  metaStrip.appendChild(metaGrid);
  container.appendChild(metaStrip);

  // Status Filter Bar
  const filterBar = el("div", "study-filter-bar");
  filterBar.appendChild(el("span", "study-filter-title", txt(t("filterStatus"))));

  const statusList = ["all", "queued", "running", "qualifying", "recovering", "qualified", "failed", "ambiguous", "cancelled"];
  let currentFilter = state.studyStatusFilter || "all";

  const chipsWrap = el("div", { style: "display: inline-flex; gap: 5px; flex-wrap: wrap;" });
  statusList.forEach(st => {
    const isCur = currentFilter === st;
    const btn = el("button", `plain ${isCur ? "primary" : ""}`, {
      style: "padding: 2px 7px; font-size: 11px;",
      onclick: () => {
        currentFilter = st;
        state.studyStatusFilter = st;
        renderEvaluationsTable();
        // Update active class on filter buttons
        chipsWrap.querySelectorAll("button").forEach(b => b.classList.remove("primary"));
        btn.classList.add("primary");
      }
    }, st === "all" ? t("filterAll") : st);
    chipsWrap.appendChild(btn);
  });
  filterBar.appendChild(chipsWrap);
  container.appendChild(filterBar);

  // Evaluations Table Section
  const tableSection = el("section", "rule-section evaluations-table-section");
  container.appendChild(tableSection);

  function renderEvaluationsTable() {
    tableSection.textContent = "";
    const filteredEvals = evals.filter(ev => {
      if (currentFilter === "all") return true;
      return String(ev.status || "").toLowerCase() === currentFilter.toLowerCase();
    });

    const sHead = el("div", "section-filter-bar");
    sHead.appendChild(el("h3", "section-title", txt(`${t("sectionEvaluations")} (${filteredEvals.length}/${evals.length})`)));
    tableSection.appendChild(sHead);

    if (filteredEvals.length === 0) {
      tableSection.appendChild(emptyPanel(t("noFilteredEvalsTitle"), t("noFilteredEvalsDesc")));
      return;
    }

    const tableWrap = el("div", "table-dense-wrap");
    const table = el("table", "data-table table-dense");
    const thead = el("thead");
    const trH = el("tr");
    trH.appendChild(el("th", "", txt(t("thEvalId"))));
    trH.appendChild(el("th", "", txt(t("thStatus"))));
    trH.appendChild(el("th", "num", txt(t("thFidelity")), tip(null, "tipFidelity")));
    trH.appendChild(el("th", "", txt(t("thPriority")), tip(null, "tipPriority")));
    trH.appendChild(el("th", "", txt(t("thWaitReason")), tip(null, "tipWaitReason")));
    trH.appendChild(el("th", "", txt(t("thWaitDuration")), tip(null, "tipWaitSince")));
    trH.appendChild(el("th", "num", txt(t("thAttempts"))));
    thead.appendChild(trH);
    table.appendChild(thead);

    const tbody = el("tbody");
    filteredEvals.forEach(ev => {
      const eid = ev.evaluation_id || "—";
      const cand = ev.candidate_id || "—";
      const atts = ev.attempts || [];
      const isExpanded = !!state.expandedAttempts[eid];

      const tr = el("tr");
      const tdId = el("td");
      tdId.appendChild(el("div", "mono mono-eval-id", { style: "font-weight: 600;", title: eid }, txt(eid)));
      if (cand !== "—") {
        tdId.appendChild(el("div", "sub dim", monoHash(cand, { len: 16, prefix: "cand: " })));
      }
      tr.appendChild(tdId);

      tr.appendChild(el("td", "", statusPill(ev.status)));
      tr.appendChild(el("td", "num mono", txt(ev.fidelity !== undefined ? ev.fidelity : "—")));
      tr.appendChild(el("td", "mono", txt(ev.priority !== undefined ? ev.priority : "—")));
      tr.appendChild(el("td", "dim", txt(ev.wait_reason || "—")));
      tr.appendChild(el("td", "sub", txt(ev.wait_since ? fmtRelativeTime(ev.wait_since) : "—")));

      const tdAtt = el("td", "num");
      if (atts.length > 0) {
        const toggleBtn = el("button", "plain", {
          style: "padding: 1px 6px; font-size: 11px;",
          onclick: () => {
            state.expandedAttempts[eid] = !state.expandedAttempts[eid];
            renderEvaluationsTable();
          }
        }, `${t("attemptsLabel", { count: atts.length })} ${isExpanded ? "▲" : "▼"}`);
        tdAtt.appendChild(toggleBtn);
      } else {
        tdAtt.appendChild(el("span", "dim sub", "0"));
      }
      tr.appendChild(tdAtt);
      tbody.appendChild(tr);

      // Expanded attempts sub-row in inset well
      if (isExpanded && atts.length > 0) {
        const trExp = el("tr", { style: "background: var(--paper-100);" });
        const tdExp = el("td", { colspan: "7", style: "padding: 8px 16px;" });

        const attWrap = el("div", "attempt-row-wrap");
        atts.forEach(a => {
          const aRow = el("div", "attempt-row-item");
          aRow.appendChild(el("span", "mono sub", txt(`#${a.attempt_number || 1}`)));
          aRow.appendChild(el("span", "mono", monoHash(a.attempt_id, { len: 16 })));
          aRow.appendChild(statusPill(a.status));
          if (a.failure_class) aRow.appendChild(chip("bad", a.failure_class));
          if (a.created_at) aRow.appendChild(el("span", "sub", txt(fmtRelativeTime(a.created_at))));
          attWrap.appendChild(aRow);
        });

        tdExp.appendChild(attWrap);
        trExp.appendChild(tdExp);
        tbody.appendChild(trExp);
      }
    });

    table.appendChild(tbody);
    tableWrap.appendChild(table);
    tableSection.appendChild(tableWrap);
  }

  renderEvaluationsTable();
  return container;
}
