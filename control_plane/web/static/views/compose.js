import { t, fmtClockTime } from "../i18n.js";
import { state } from "../state.js";
import { el, txt, pageHead, entityPicker, technicalDetails, showError } from "../ui.js";
import { postJSON, fetchJSON } from "../api.js";
import { entityName, problemValue, problemLabel } from "../display.js";
import { navigate } from "../router.js";

// Stage sub-view imports
import { renderPackagesView } from "./packages.js";
import { renderSchemasView } from "./schemas.js";
import { renderProblemsView } from "./problems.js";
import { renderStudiesView } from "./studies.js";
import { renderCandidateDesigner } from "./candidateDesigner.js";


export async function renderCompose({ initialStep = 1, detailId = null, detailType = null } = {}) {
  const container = el("div", "workbench-view");

  // Readonly note if server is read-only
  if (!state.writes) {
    container.appendChild(el("div", "readonly-note", txt(t("readonlyNote"))));
  }

  // Active stage state (1 to 5)
  let activeStage = (initialStep !== undefined && initialStep !== null) ? initialStep : (state.composeStep || 1);
  let activeDetailId = detailId;

  const isTemplate = activeStage <= 3;
  container.appendChild(pageHead(
    t(isTemplate ? "templatesTitle" : "researchTitle"),
    t(isTemplate ? "templatesDesc" : "researchDesc")
  ));

  const workspaceNav = el("nav", "workspace-tabs", {
    "aria-label": t(isTemplate ? "templatesTitle" : "researchTitle")
  });
  const sections = isTemplate
    ? [{ step: 1, key: "templateFiles" }, { step: 2, key: "templateParameters" }]
    : [{ step: 5, key: "researchRun" }, { step: 4, key: "researchRecords" }];
  sections.forEach(({ step, key }) => {
    const link = el("a", "workspace-tab", { href: `#/compose?step=${step}` }, txt(t(key)));
    if (step === activeStage || (step === 2 && activeStage === 3)) link.setAttribute("aria-current", "page");
    workspaceNav.appendChild(link);
  });
  container.appendChild(workspaceNav);

  const heroWorkspace = el("div", "workbench-hero-workspace");
  container.appendChild(heroWorkspace);

  async function renderWorkspace() {
    heroWorkspace.textContent = "";
    state.composeStep = activeStage;

    // Single Hero Surface Area
    const heroSurface = el("div", "workbench-hero-surface");
    heroWorkspace.appendChild(heroSurface);

    if (activeStage === 1) {
      // Stage 1: Package Workspace & Deck Parser
      heroSurface.appendChild(renderPackagesView());
    } else if (activeStage === 2) {
      // Stage 2: Persistent Schema Draft & Schema Registry
      const schemaView = await renderSchemasView({ revision: activeDetailId });
      heroSurface.appendChild(schemaView);
    } else if (activeStage === 3) {
      // Stage 3: Problems & Contracts
      heroSurface.appendChild(activeDetailId
        ? await renderProblemsView({ id: activeDetailId })
        : await renderSchemasView());
    } else if (activeStage === 4) {
      // Stage 4: Studies & Queues
      await renderStage4Studies(heroSurface);
    } else if (activeStage === 5) {
      // Stage 5: Candidate Designer & Manual Smoke-Test Evaluation
      renderStage5Candidate(heroSurface);
    }
  }

  // ================= Stage 4: Studies Workspace =================
  async function renderStage4Studies(surface) {
    if (activeDetailId) {
      const detailView = await renderStudiesView({ id: activeDetailId });
      surface.appendChild(detailView);
      return;
    }

    const pRes = await fetchJSON("/api/problems");
    if (pRes && pRes.ok && pRes.data) {
      state.problemsList = pRes.data.items || pRes.data.problems || (Array.isArray(pRes.data) ? pRes.data : []);
    }

    const wrap = el("div", "stage-content-wrap");

    // Collapsible Swiss Field Guide
    const guide = el("details", "guide-details");
    guide.appendChild(el("summary", "", el("span", "guide-icon", "ℹ "), txt(t("studiesGuideSummary"))));
    guide.appendChild(el("div", "details-content", txt(t("studiesGuide"))));
    wrap.appendChild(guide);

    // Study Creation Earned Action Surface
    const createCard = el("section", "action-surface study-workbench-section");
    createCard.appendChild(el("h3", "action-surface-heading", txt(t("composeStep3Title"))));
    createCard.appendChild(el("div", "action-surface-sub", txt(t("composeStep3Desc"))));

    const r1 = el("div", "form-row");
    const sField = el("div", "form-field");
    sField.appendChild(el("label", "", txt(t("fieldStudyId"))));
    const sIn = el("input", {
      type: "text",
      id: "s-study",
      className: "mono",
      value: "" , placeholder: t("studyNamePlaceholder")
    });
    sField.appendChild(sIn);
    r1.appendChild(sField);

    const activeProblem = state.problemsList.find(p => p.problem_id === state.candidateDesigner.problemId &&
      (p.problem_revision || p.revision) === state.candidateDesigner.problemRev) || state.problemsList.at(-1);
    const probPicker = entityPicker({
      label: t("fieldProblemId"), id: "s-problem", value: activeProblem ? problemValue(activeProblem) : "",
      items: state.problemsList,
      itemToOption: p => ({value: problemValue(p), label: problemLabel(p)}),
      onSelect: val => {
        const matched = state.problemsList.find(p => problemValue(p) === val);
        if (matched) {
          revIn.value = matched.problem_revision || matched.revision;
          state.candidateDesigner.problemId = matched.problem_id;
          state.candidateDesigner.problemRev = revIn.value;
        }
      }, allowManual: false
    });
    r1.appendChild(probPicker.node);
    createCard.appendChild(r1);
    const initialProbRev = activeProblem?.problem_revision || activeProblem?.revision || "";

    const r2 = el("div", "form-row");
    const revField = el("div", "form-field");
    revField.appendChild(el("label", "", txt(t("fieldProblemRevision"))));
    const revIn = el("input", { type: "text", id: "s-rev", className: "mono", value: initialProbRev });
    revField.appendChild(revIn);
    r2.appendChild(revField);

    const autoField = el("div", "form-field");
    autoField.appendChild(el("label", "", txt(t("fieldAutomationProfile"))));
    const autoIn = el("input", { type: "text", id: "s-auto", className: "mono", value: "assisted" });
    autoField.appendChild(autoIn);
    r2.appendChild(autoField);
    const studyAdvanced = technicalDetails(t("advancedSettings"), r2);
    createCard.appendChild(studyAdvanced);

    const r3 = el("div", "form-row");
    const byField = el("div", "form-field");
    byField.appendChild(el("label", "", txt(t("fieldSubmittedBy"))));
    const byIn = el("input", { type: "text", id: "s-by", className: "mono", value: "eval-researcher" });
    byField.appendChild(byIn);
    r3.appendChild(byField);

    const metaField = el("div", "form-field");
    metaField.appendChild(el("label", "", txt(t("fieldMetadataJson"))));
    const metaIn = el("textarea", {
      id: "s-meta",
      className: "mono",
      placeholder: '{"note": "baseline test"}'
    });
    metaField.appendChild(metaIn);
    r3.appendChild(metaField);
    studyAdvanced.lastElementChild.appendChild(r3);

    const btnRow = el("div", { style: "display: flex; gap: 8px; align-items: center; margin-top: 14px; flex-wrap: wrap;" });
    const prevBtn = el("button", "plain", txt(t("stepPrev")));
    prevBtn.type = "button";
    prevBtn.onclick = () => { activeStage = 3; navigate("#/compose?step=3"); };

    const createBtn = el("button", "plain primary", txt(t("btnCreateStudy")));
    createBtn.type = "button";
    createBtn.id = "btn-submit-study";
    if (!state.writes) {
      createBtn.disabled = true;
      createBtn.title = t("needAllowWrites");
    }

    const nextBtn = el("button", "plain", txt(t("toStepCandidate")));
    nextBtn.type = "button";
    nextBtn.onclick = () => { activeStage = 5; navigate("#/compose?step=5"); };

    btnRow.appendChild(prevBtn);
    btnRow.appendChild(createBtn);
    btnRow.appendChild(nextBtn);
    createCard.appendChild(btnRow);

    const msg = el("div", "submit-msg", { style: "margin-top: 8px;" });
    createCard.appendChild(msg);

    const generatedStudyId = "study:" + crypto.randomUUID();
    createBtn.onclick = async () => {
      if (!sIn.value.trim()) { msg.textContent = t("studyNameRequired"); return; }
      let meta = {};
      const metaRaw = metaIn.value.trim();
      if (metaRaw) {
        try {
          meta = JSON.parse(metaRaw);
          if (typeof meta !== "object" || Array.isArray(meta)) throw new Error(t("errMetaMustBeObject"));
        } catch (e) {
          msg.className = "submit-msg err";
          msg.textContent = e.message;
          return;
        }
      }
      if (byIn.value.trim()) {
        meta.submitted_by = byIn.value.trim();
      }

      createBtn.disabled = true;
      msg.className = "submit-msg";
      msg.textContent = t("connecting");

      const studyIdVal = generatedStudyId;
      meta.display_name = sIn.value.trim();
      const selectedProblem = state.problemsList.find(p => problemValue(p) === probPicker.getValue());
      if (!selectedProblem) { createBtn.disabled = false; msg.textContent = t("chooseTemplate"); return; }
      const probIdVal = selectedProblem.problem_id;

      const r = await postJSON("/api/studies", {
        study_id: studyIdVal,
        problem_id: probIdVal,
        problem_revision: revIn.value.trim(),
        automation_profile: autoIn.value.trim(),
        metadata: meta
      });
      createBtn.disabled = false;

      if (!r || !r.ok || !r.data) {
        msg.className = "submit-msg err";
        showError(msg, (r && r.data && r.data.error) || t("netError"));
        return;
      }

      state.candidateDesigner.studyId = studyIdVal;
      state.candidateDesigner.problemId = probIdVal;
      state.candidateDesigner.problemRev = revIn.value.trim();

      msg.className = "submit-msg ok";
      msg.textContent = t("msgStudyCreated");

      const advanceBtn = el("button", "plain primary", { style: "margin-top: 8px;" }, txt(t("readyAdvanceStep4")));
      advanceBtn.type = "button";
      advanceBtn.onclick = () => {
        activeStage = 5;
        navigate("#/compose?step=5");
      };
      msg.appendChild(el("div", { style: "margin-top: 6px;" }, advanceBtn));

      const updatedCatalog = await renderStudiesView({ isEmbedded: true });
      const oldCatalog = wrap.querySelector(".studies-catalog-section");
      if (oldCatalog && updatedCatalog.querySelector(".studies-catalog-section")) {
        oldCatalog.replaceWith(updatedCatalog.querySelector(".studies-catalog-section"));
      }
    };

    wrap.appendChild(createCard);

    // Embedded Studies Catalog Table View
    const catalogView = await renderStudiesView({ isEmbedded: true });
    wrap.appendChild(catalogView);
    surface.appendChild(wrap);
  }

  // ================= Stage 5: Candidate Designer & Manual Smoke-Test =================
  function renderStage5Candidate(surface) {
    const designerNode = renderCandidateDesigner({
      onSubmitted: () => {
        // Optional callback on candidate submitted
      }
    });
    surface.appendChild(designerNode);
  }

  await renderWorkspace();

  // Baseline Footer Termination
  const baselineFooter = el("footer", "overview-baseline-footer", { role: "contentinfo" });
  const footerRow = el("div", "baseline-meta-row");
  footerRow.appendChild(el("span", "baseline-meta meta mono", txt(t("workbenchBaselineMeta") || "Simtakt · 仿真研究工作空间")));
  footerRow.appendChild(el("span", "baseline-telemetry meta mono", txt(`${t("overviewLiveSync") || "实时同步: 正常"} · ${fmtClockTime(new Date())}`)));
  baselineFooter.appendChild(footerRow);
  container.appendChild(baselineFooter);

  return container;
}
