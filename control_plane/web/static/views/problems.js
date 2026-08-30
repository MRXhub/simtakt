/**
 * Problems List & Detail Views · Swiss Technical Typography
 * Stage 3 in the 5-stage composition pipeline.
 *
 * Vocabulary & Doctrine:
 * - Rule-delimited catalog sections and dense data tables
 * - Monospace ID and revision truncation (zero horizontal overflow)
 * - Clean metadata key-value strip in detail views without nested cards
 * - Upstream linking to Schemas and downstream handoffs to Studies & Candidate Designer
 */

import { t } from "../i18n.js";
import { state } from "../state.js";
import { el, txt, pageHead, emptyPanel, errorBlock, chip, statusPill, monoHash } from "../ui.js";
import { fetchJSON } from "../api.js";
import { navigate } from "../router.js";

export async function renderProblemsView({ id, isEmbedded = false } = {}) {
  const container = el("div", "problems-view");

  if (id) {
    return renderProblemDetail(id, container);
  }

  // Header only when rendered as standalone page
  if (!isEmbedded) {
    const head = pageHead(t("problemsTitle"), t("problemsDesc"), [
      el("button", "plain primary", {
        onclick: () => navigate("#/compose?step=3")
      }, "➕ " + t("submitCardProblem"))
    ]);
    container.appendChild(head);
  }

  const catalogSection = el("section", "rule-section problems-catalog-section");
  if (isEmbedded) {
    const catalogHead = el("div", "section-filter-bar");
    catalogHead.appendChild(el("h3", "section-title", txt(t("catalogProblemsTitle"))));
    catalogSection.appendChild(catalogHead);
  }

  // Fetch from GET /api/problems
  const r = await fetchJSON("/api/problems");
  let problems = (r && r.ok && r.data && (r.data.items || r.data.problems || (Array.isArray(r.data) ? r.data : []))) || [];

  if (problems.length === 0 && state.problemsList.length > 0) {
    problems = state.problemsList;
  }
  state.problemsList = problems;

  if (problems.length === 0) {
    catalogSection.appendChild(emptyPanel(t("noProblemsTitle"), t("noProblemsDesc")));
    container.appendChild(catalogSection);
    return container;
  }

  const tableWrap = el("div", "table-dense-wrap");
  const table = el("table", "data-table table-dense");
  const thead = el("thead");
  const trH = el("tr");
  trH.appendChild(el("th", "", txt(t("fieldProblemId"))));
  trH.appendChild(el("th", "", txt(t("fieldParamSchemaRev"))));
  trH.appendChild(el("th", "", txt(t("thRevision"))));
  trH.appendChild(el("th", "", txt(t("thCapabilities"))));
  trH.appendChild(el("th", "num", txt(t("thActions"))));
  thead.appendChild(trH);
  table.appendChild(thead);

  const tbody = el("tbody");
  problems.forEach(p => {
    const pid = p.problem_id || p.id || (typeof p === "string" ? p : "—");
    const sRev = p.parameter_schema_revision || p.schema_revision || "—";
    const pRev = p.problem_revision || p.revision || "—";
    const caps = (p.simulation_capabilities || ["tcad", "spis", "spice"]);

    const tr = el("tr");
    const tdId = el("td", "col-problem-id");
    const aId = el("a", "mono mono-problem-id", {
      href: `#/problem/${encodeURIComponent(pid)}`,
      title: pid,
      style: "font-weight: 600;"
    }, txt(pid));
    tdId.appendChild(aId);
    tr.appendChild(tdId);

    const tdSchema = el("td", "col-problem-id");
    if (sRev && sRev !== "—") {
      const aSchema = el("a", "mono", {
        href: `#/schema/${encodeURIComponent(sRev)}`,
        title: sRev
      }, monoHash(sRev, { len: 14 }));
      tdSchema.appendChild(aSchema);
    } else {
      tdSchema.appendChild(el("span", "dim", "—"));
    }
    tr.appendChild(tdSchema);

    tr.appendChild(el("td", "mono sub", monoHash(pRev, { len: 12 })));

    const tdCaps = el("td");
    const capsWrap = el("div", { style: "display: inline-flex; gap: 4px; flex-wrap: wrap;" });
    caps.forEach(c => capsWrap.appendChild(chip("dim", c)));
    tdCaps.appendChild(capsWrap);
    tr.appendChild(tdCaps);

    const tdAct = el("td", "num");
    const actGroup = el("div", { style: "display: inline-flex; gap: 5px; align-items: center;" });

    const viewBtn = el("button", "plain", {
      style: "padding: 2px 6px; font-size: 11px;",
      onclick: () => navigate(`#/problem/${encodeURIComponent(pid)}`)
    }, "🔍 " + t("btnViewDetail"));

    const studyBtn = el("button", "plain", {
      style: "padding: 2px 6px; font-size: 11px;",
      onclick: () => {
        state.candidateDesigner.problemId = pid;
        state.candidateDesigner.problemRev = pRev;
        navigate("#/compose?step=4");
      }
    }, "🔬 " + t("btnNewStudy"));

    const evalBtn = el("button", "plain primary", {
      style: "padding: 2px 6px; font-size: 11px;",
      onclick: () => {
        state.candidateDesigner.problemId = pid;
        state.candidateDesigner.problemRev = pRev;
        if (sRev && sRev !== "—") state.candidateDesigner.schemaRev = sRev;
        navigate("#/compose?step=5");
      }
    }, "⚡ " + t("btnSubmitEval"));

    actGroup.appendChild(viewBtn);
    actGroup.appendChild(studyBtn);
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
 * Problem Detail View (Flat rule-delimited layout without generic panel-card wrappers)
 */
async function renderProblemDetail(pid, container) {
  const head = pageHead(t("problemTitle", { id: pid }), t("problemDesc"), [
    el("button", "plain", { onclick: () => navigate("#/compose?step=3") }, "⬅️ " + t("btnBackToList")),
    el("button", "plain", {
      onclick: () => {
        state.candidateDesigner.problemId = pid;
        navigate("#/compose?step=4");
      }
    }, t("btnCreateStudyForProblem")),
    el("button", "plain primary", {
      onclick: () => {
        state.candidateDesigner.problemId = pid;
        navigate("#/compose?step=5");
      }
    }, t("btnSubmitCandidateForProblem"))
  ]);
  container.appendChild(head);

  const r = await fetchJSON(`/api/problems/${encodeURIComponent(pid)}`);
  if (!r || !r.ok || !r.data) {
    container.appendChild(errorBlock(t("statusError", { status: r ? r.status : 404 }) + ((r && r.data && r.data.error) || t("netError")), t("generalErrorHint")));
    return container;
  }

  const pData = r.data;
  state.problem = pData;
  state.problemId = pid;

  // Metadata Strip (Flat KV Rail)
  const metaStrip = el("div", "detail-meta-strip");
  const metaGrid = el("div", "detail-kv-grid");

  const rId = el("div", "detail-kv-item");
  rId.appendChild(el("span", "kv-key", txt(t("fieldProblemId"))));
  rId.appendChild(el("span", "kv-val mono", txt(pid)));
  metaGrid.appendChild(rId);

  const sRev = pData.parameter_schema_revision || (pData.problem && pData.problem.parameter_schema_revision);
  const rSchema = el("div", "detail-kv-item");
  rSchema.appendChild(el("span", "kv-key", txt(t("fieldParamSchemaRev"))));
  if (sRev) {
    const aRev = el("a", "mono", { href: `#/schema/${encodeURIComponent(sRev)}` }, monoHash(sRev, { len: 20 }));
    rSchema.appendChild(aRev);
  } else {
    rSchema.appendChild(el("span", "dim", "—"));
  }
  metaGrid.appendChild(rSchema);

  const rConst = el("div", "detail-kv-item");
  rConst.appendChild(el("span", "kv-key", txt(t("metaConstraint"))));
  rConst.appendChild(el("span", "kv-val mono", txt(pData.constraint_revision || "none")));
  metaGrid.appendChild(rConst);

  const rMetric = el("div", "detail-kv-item");
  rMetric.appendChild(el("span", "kv-key", txt(t("metaMetricSchema"))));
  rMetric.appendChild(el("span", "kv-val mono", txt(pData.metric_schema_revision || "default")));
  metaGrid.appendChild(rMetric);

  metaStrip.appendChild(metaGrid);
  container.appendChild(metaStrip);

  // Associated Studies Section
  const studies = pData.studies || [];
  const studiesSection = el("section", "rule-section");
  const sHead = el("div", "section-filter-bar");
  sHead.appendChild(el("h3", "section-title", txt(`${t("sectionStudies")} (${studies.length})`)));
  studiesSection.appendChild(sHead);

  if (studies.length === 0) {
    studiesSection.appendChild(emptyPanel(t("noProblemStudiesTitle"), t("noProblemStudiesDesc")));
  } else {
    const sWrap = el("div", "table-dense-wrap");
    const sTable = el("table", "data-table table-dense");
    const sThead = el("thead");
    const sTrH = el("tr");
    sTrH.appendChild(el("th", "", txt(t("thStudyId"))));
    sTrH.appendChild(el("th", "", txt(t("thRevision"))));
    sTrH.appendChild(el("th", "", txt(t("thAlgorithmRunId"))));
    sTrH.appendChild(el("th", "", txt(t("thAutomationProfile"))));
    sTrH.appendChild(el("th", "num", txt(t("thActions"))));
    sThead.appendChild(sTrH);
    sTable.appendChild(sThead);

    const sTbody = el("tbody");
    studies.forEach(s => {
      const tr = el("tr");
      const tdSid = el("td", "col-study-id");
      tdSid.appendChild(el("a", "mono mono-study-id", {
        href: `#/study/${encodeURIComponent(s.study_id)}`,
        style: "font-weight: 600;"
      }, txt(s.study_id)));
      tr.appendChild(tdSid);

      tr.appendChild(el("td", "mono sub", monoHash(s.problem_revision, { len: 12 })));

      const tdRun = el("td", "col-problem-id");
      if (s.algorithm_run_id) {
        tdRun.appendChild(el("a", "mono mono-run-id", {
          href: `#/algorithm/${encodeURIComponent(s.algorithm_run_id)}`,
          title: s.algorithm_run_id
        }, txt(s.algorithm_run_id)));
      } else {
        tdRun.appendChild(el("span", "dim", "—"));
      }
      tr.appendChild(tdRun);

      tr.appendChild(el("td", "", chip("info", s.automation_profile || "standard")));

      const tdAct = el("td", "num");
      const btn = el("button", "plain primary", {
        style: "padding: 2px 6px; font-size: 11px;",
        onclick: () => {
          state.candidateDesigner.studyId = s.study_id;
          state.candidateDesigner.problemId = pid;
          navigate("#/compose?step=5");
        }
      }, "⚡ " + t("btnSubmitEval"));
      tdAct.appendChild(btn);
      tr.appendChild(tdAct);

      sTbody.appendChild(tr);
    });
    sTable.appendChild(sTbody);
    sWrap.appendChild(sTable);
    studiesSection.appendChild(sWrap);
  }
  container.appendChild(studiesSection);

  // Associated Evaluations Section
  const evals = pData.evaluations || [];
  const evalsSection = el("section", "rule-section");
  const eHead = el("div", "section-filter-bar");
  eHead.appendChild(el("h3", "section-title", txt(`${t("sectionEvaluations")} (${evals.length})`)));
  evalsSection.appendChild(eHead);

  if (evals.length === 0) {
    evalsSection.appendChild(emptyPanel(t("noProblemEvalsTitle"), t("noProblemEvalsDesc")));
  } else {
    const eWrap = el("div", "table-dense-wrap");
    const eTable = el("table", "data-table table-dense");
    const eThead = el("thead");
    const eTrH = el("tr");
    eTrH.appendChild(el("th", "", txt(t("thEvalId"))));
    eTrH.appendChild(el("th", "", txt(t("thStatus"))));
    eTrH.appendChild(el("th", "num", txt(t("thFidelity"))));
    eTrH.appendChild(el("th", "", txt(t("thPriority"))));
    eThead.appendChild(eTrH);
    eTable.appendChild(eThead);

    const eTbody = el("tbody");
    evals.forEach(ev => {
      const tr = el("tr");
      const tdId = el("td");
      tdId.appendChild(el("div", "mono mono-eval-id", { style: "font-weight: 600;", title: ev.evaluation_id }, txt(ev.evaluation_id)));
      if (ev.candidate_id) {
        tdId.appendChild(el("div", "sub dim", monoHash(ev.candidate_id, { len: 16, prefix: "cand: " })));
      }
      tr.appendChild(tdId);

      tr.appendChild(el("td", "", statusPill(ev.status)));
      tr.appendChild(el("td", "num mono", txt(ev.fidelity !== undefined ? ev.fidelity : "—")));
      tr.appendChild(el("td", "mono", txt(ev.priority !== undefined ? ev.priority : "—")));
      eTbody.appendChild(tr);
    });
    eTable.appendChild(eTbody);
    eWrap.appendChild(eTable);
    evalsSection.appendChild(eWrap);
  }
  container.appendChild(evalsSection);

  return container;
}
