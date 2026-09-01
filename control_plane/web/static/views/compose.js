/**
 * Unified 5-Stage Composition Workbench View · Swiss Technical Typography
 * Manages the sequential 5-stage research composition pipeline:
 *   Stage 1: Packages (Deck parser & runtime artifact landing)
 *   Stage 2: Schemas (ParameterSchema draft & registration)
 *   Stage 3: Problems (Problem contracts & capability definition)
 *   Stage 4: Studies (Study container & queue management)
 *   Stage 5: Candidate (Candidate designer & smoke-test evaluation)
 */

import { t, fmtClockTime } from "../i18n.js";
import { state } from "../state.js";
import { el, txt, pageHead, entityPicker } from "../ui.js";
import { postJSON } from "../api.js";
import { navigate } from "../router.js";

// Stage sub-view imports
import { renderPackagesView } from "./packages.js";
import { renderSchemasView } from "./schemas.js";
import { renderProblemsView } from "./problems.js";
import { renderStudiesView } from "./studies.js";
import { renderCandidateDesigner } from "./candidateDesigner.js";

const DEFAULT_SHA256_0 = "sha256:" + "0".repeat(64);
const DEFAULT_SHA256_1 = "sha256:" + "1".repeat(64);
const DEFAULT_SHA256_2 = "sha256:" + "2".repeat(64);

export async function renderCompose({ initialStep = 1, detailId = null, detailType = null } = {}) {
  const container = el("div", "workbench-view");

  // Readonly note if server is read-only
  if (!state.writes) {
    container.appendChild(el("div", "readonly-note", txt(t("readonlyNote"))));
  }

  // Active stage state (1 to 5)
  let activeStage = (initialStep !== undefined && initialStep !== null) ? initialStep : (state.composeStep || 1);
  let activeDetailId = detailId;
  let activeDetailType = detailType;

  // Header
  const head = pageHead(t("workbenchTitle"), t("workbenchDesc"));
  container.appendChild(head);

  // Master-Detail Layout Container
  const masterDetailWrap = el("div", "workbench-master-detail");
  container.appendChild(masterDetailWrap);

  // Left: Stage Outline Navigator Rail (Master)
  const outlineRail = el("aside", "workbench-outline-rail");
  masterDetailWrap.appendChild(outlineRail);

  // Right: Hero Detail Workspace (Hero surface)
  const heroWorkspace = el("main", "workbench-hero-workspace");
  masterDetailWrap.appendChild(heroWorkspace);

  const STAGES = [
    { step: 1, titleKey: "stage1Title", subKey: "stage1Sub", tag: "Deck" },
    { step: 2, titleKey: "stage2Title", subKey: "stage2Sub", tag: "Schema" },
    { step: 3, titleKey: "stage3Title", subKey: "stage3Sub", tag: "Problem" },
    { step: 4, titleKey: "stage4Title", subKey: "stage4Sub", tag: "Study" },
    { step: 5, titleKey: "stage5Title", subKey: "stage5Sub", tag: "Candidate" }
  ];

  let isMobileStageListOpen = false;

  function renderOutline() {
    outlineRail.textContent = "";

    const outlineCard = el("nav", `workbench-outline-card ${isMobileStageListOpen ? "mobile-open" : ""}`, {
      "aria-label": t("stagesNavTitle"),
      id: "workbench-stages-nav"
    });

    // Mobile Compact Progress Bar
    const curStage = STAGES.find(s => s.step === activeStage) || STAGES[0];
    const mobileBar = el("div", "workbench-stage-mobile-bar");

    const mobileInfo = el("div", "mobile-stage-info");
    const badge = el("span", "mobile-stage-badge", `${curStage.step} / ${STAGES.length}`);
    mobileInfo.appendChild(badge);

    const titleWrap = el("div", "mobile-stage-title-wrap");
    titleWrap.appendChild(el("span", "mobile-stage-title", txt(t(curStage.titleKey))));
    titleWrap.appendChild(el("span", "mobile-stage-tag", txt(curStage.tag)));
    mobileInfo.appendChild(titleWrap);
    mobileBar.appendChild(mobileInfo);

    const toggleBtn = el("button", "mobile-stage-toggle-btn", {
      type: "button",
      id: "mobile-stage-toggle",
      "aria-expanded": isMobileStageListOpen ? "true" : "false",
      "aria-label": t("toggleStagesList") || "Toggle stage list",
      onclick: (e) => {
        e.stopPropagation();
        isMobileStageListOpen = !isMobileStageListOpen;
        outlineCard.classList.toggle("mobile-open", isMobileStageListOpen);
        toggleBtn.setAttribute("aria-expanded", isMobileStageListOpen ? "true" : "false");
        const lblNode = toggleBtn.querySelector(".toggle-label");
        if (lblNode) lblNode.textContent = isMobileStageListOpen ? (t("mobileStageClose") || "Close") : (t("mobileStageToggle") || "All Stages");
        const arrNode = toggleBtn.querySelector(".toggle-arrow");
        if (arrNode) arrNode.textContent = isMobileStageListOpen ? "▴" : "▾";
      }
    });

    const toggleLabel = el("span", "toggle-label", txt(isMobileStageListOpen ? (t("mobileStageClose") || "Close") : (t("mobileStageToggle") || "All Stages")));
    const toggleArrow = el("span", "toggle-arrow", isMobileStageListOpen ? "▴" : "▾");
    toggleBtn.appendChild(toggleLabel);
    toggleBtn.appendChild(toggleArrow);
    mobileBar.appendChild(toggleBtn);
    outlineCard.appendChild(mobileBar);

    // Desktop Header
    const cardHead = el("div", "outline-group-head");
    cardHead.appendChild(el("span", "outline-group-title", txt(t("stagesNavTitle"))));
    outlineCard.appendChild(cardHead);

    // Stage Tree
    const stageList = el("div", "outline-tree");

    STAGES.forEach((s) => {
      const isActive = activeStage === s.step;
      const stageBtn = el("button", `outline-node ${isActive ? "active" : ""}`, {
        type: "button",
        role: "tab",
        "aria-selected": isActive ? "true" : "false",
        onclick: () => {
          activeStage = s.step;
          activeDetailId = null;
          activeDetailType = null;
          isMobileStageListOpen = false;
          navigate(`#/compose?step=${s.step}`);
        }
      });

      const mainLine = el("div", "node-main-line");
      mainLine.appendChild(el("span", "node-title", txt(t(s.titleKey))));
      mainLine.appendChild(el("span", "node-role-muted", txt(s.tag)));
      stageBtn.appendChild(mainLine);

      const subLine = el("div", "node-desc-sub", txt(t(s.subKey)));
      stageBtn.appendChild(subLine);

      stageList.appendChild(stageBtn);
    });

    outlineCard.appendChild(stageList);
    outlineRail.appendChild(outlineCard);
  }

  async function renderWorkspace() {
    heroWorkspace.textContent = "";
    state.composeStep = activeStage;

    renderOutline();

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
      await renderStage3Problems(heroSurface);
    } else if (activeStage === 4) {
      // Stage 4: Studies & Queues
      await renderStage4Studies(heroSurface);
    } else if (activeStage === 5) {
      // Stage 5: Candidate Designer & Manual Smoke-Test Evaluation
      renderStage5Candidate(heroSurface);
    }
  }

  // ================= Stage 3: Problems Workspace =================
  async function renderStage3Problems(surface) {
    if (activeDetailId) {
      const detailView = await renderProblemsView({ id: activeDetailId });
      surface.appendChild(detailView);
      return;
    }

    const wrap = el("div", "stage-content-wrap");

    // Collapsible Swiss Field Guide
    const guide = el("details", "guide-details");
    guide.appendChild(el("summary", "", el("span", "guide-icon", "ℹ "), txt(t("problemsGuideSummary"))));
    guide.appendChild(el("div", "details-content", txt(t("problemsGuide"))));
    wrap.appendChild(guide);

    // Contract Creation Earned Action Surface
    const createCard = el("section", "action-surface problem-workbench-section");
    createCard.appendChild(el("h3", "action-surface-heading", txt(t("composeStep2Title"))));
    createCard.appendChild(el("div", "action-surface-sub", txt(t("composeStep2Desc"))));

    const r1 = el("div", "form-row");
    const pidField = el("div", "form-field");
    pidField.appendChild(el("label", "", txt(t("fieldProblemId"))));
    const pidIn = el("input", {
      type: "text",
      id: "p-id",
      className: "mono",
      value: state.packagesPackageName ? `problem:${state.packagesPackageName.replace(/^pkg-/, "")}` : "problem:sched-opt"
    });
    pidField.appendChild(pidIn);
    r1.appendChild(pidField);

    const activeSchemaRev = state.schemaDraft.registeredRevision || state.packagesSchemaRev || state.candidateDesigner.schemaRev || "sha256:mock-schema-ten-junction-v1";
    const schemaPicker = entityPicker({
      label: t("fieldParamSchemaRev"),
      id: "p-param",
      value: activeSchemaRev,
      items: state.schemasList,
      itemToOption: (s) => ({ value: s.revision || s, label: s.revision ? s.revision.slice(0, 24) + "…" : s, sub: s.kind }),
      onSelect: (val) => {
        state.candidateDesigner.schemaRev = val;
      },
      placeholder: "sha256:..."
    });
    r1.appendChild(schemaPicker.node);
    createCard.appendChild(r1);

    const r2 = el("div", "form-row");
    const constField = el("div", "form-field");
    constField.appendChild(el("label", "", txt(t("fieldConstraintRev"))));
    const constIn = el("input", { type: "text", id: "p-constraint", className: "mono", value: DEFAULT_SHA256_0 });
    constField.appendChild(constIn);
    r2.appendChild(constField);

    const metField = el("div", "form-field");
    metField.appendChild(el("label", "", txt(t("fieldMetricSchemaRev"))));
    const metIn = el("input", { type: "text", id: "p-metric", className: "mono", value: DEFAULT_SHA256_1 });
    metField.appendChild(metIn);
    r2.appendChild(metField);
    createCard.appendChild(r2);

    const r3 = el("div", "form-row one");
    const capField = el("div", "form-field");
    capField.appendChild(el("label", "", txt(t("fieldSimCaps"))));
    const capIn = el("input", { type: "text", id: "p-caps", className: "mono", value: "tcad, spis, spice" });
    capField.appendChild(capIn);
    r3.appendChild(capField);
    createCard.appendChild(r3);

    const btnRow = el("div", { style: "display: flex; gap: 8px; align-items: center; margin-top: 14px; flex-wrap: wrap;" });
    const prevBtn = el("button", "plain", txt(t("stepPrev")));
    prevBtn.type = "button";
    prevBtn.onclick = () => { activeStage = 2; navigate("#/compose?step=2"); };

    const genBtn = el("button", "plain", txt(t("btnGenProblemPreview")));
    genBtn.type = "button";

    const regBtn = el("button", "plain primary", txt(t("btnConfirmRegister")));
    regBtn.type = "button";
    regBtn.id = "btn-confirm-problem";
    regBtn.disabled = true;

    const nextBtn = el("button", "plain", txt(t("toStepStudy")));
    nextBtn.type = "button";
    nextBtn.onclick = () => { activeStage = 4; navigate("#/compose?step=4"); };

    btnRow.appendChild(prevBtn);
    btnRow.appendChild(genBtn);
    btnRow.appendChild(regBtn);
    btnRow.appendChild(nextBtn);
    createCard.appendChild(btnRow);

    const msg = el("div", "submit-msg", { style: "margin-top: 8px;" });
    const prev = el("pre", "preview log-well", { style: "margin-top: 10px;" });
    createCard.appendChild(msg);
    createCard.appendChild(prev);

    let builtContract = null;

    genBtn.onclick = async () => {
      const spec = {
        problem_id: pidIn.value.trim(),
        parameter_schema_revision: schemaPicker.getValue().trim(),
        constraint_revision: constIn.value.trim(),
        simulation_capabilities: capIn.value.split(",").map(x => x.trim()).filter(Boolean),
        metric_schema_revision: metIn.value.trim()
      };

      genBtn.disabled = true;
      msg.className = "submit-msg";
      msg.textContent = t("connecting");

      const r = await postJSON("/api/contracts/build", { kind: "problem", spec });
      genBtn.disabled = false;

      if (!r || !r.ok || !r.data) {
        msg.className = "submit-msg err";
        msg.textContent = (r && r.data && r.data.error) || t("netError");
        return;
      }

      builtContract = r.data.contract;
      prev.textContent = JSON.stringify(builtContract, null, 2);
      msg.className = "submit-msg ok";
      msg.textContent = t("msgPreviewDone");

      regBtn.disabled = !state.writes;
      if (!state.writes) regBtn.title = t("needAllowWrites");
    };

    regBtn.onclick = async () => {
      if (!builtContract) return;
      regBtn.disabled = true;
      msg.className = "submit-msg";
      msg.textContent = t("connecting");

      const r = await postJSON("/api/problems", builtContract);
      regBtn.disabled = false;

      if (!r || !r.ok || !r.data) {
        msg.className = "submit-msg err";
        msg.textContent = (r && r.data && r.data.error) || t("netError");
        return;
      }

      const pData = r.data.problem || r.data.contract || r.data;
      const rev = pData.problem_revision || "sha256:registered";
      state.candidateDesigner.problemId = pidIn.value.trim();
      state.candidateDesigner.problemRev = rev;

      msg.className = "submit-msg ok";
      msg.textContent = t("msgProblemRegistered", { rev });

      const advanceBtn = el("button", "plain primary", { style: "margin-top: 8px;" }, txt(t("readyAdvanceStep3")));
      advanceBtn.type = "button";
      advanceBtn.onclick = () => {
        activeStage = 4;
        navigate("#/compose?step=4");
      };
      msg.appendChild(el("div", { style: "margin-top: 6px;" }, advanceBtn));
    };

    wrap.appendChild(createCard);

    // Embedded Problems Catalog Table View
    const catalogView = await renderProblemsView({ isEmbedded: true });
    wrap.appendChild(catalogView);
    surface.appendChild(wrap);
  }

  // ================= Stage 4: Studies Workspace =================
  async function renderStage4Studies(surface) {
    if (activeDetailId) {
      const detailView = await renderStudiesView({ id: activeDetailId });
      surface.appendChild(detailView);
      return;
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
      value: "study:exp-" + Math.random().toString(36).slice(2, 7)
    });
    sField.appendChild(sIn);
    r1.appendChild(sField);

    const probPicker = entityPicker({
      label: t("fieldProblemId"),
      id: "s-problem",
      value: state.candidateDesigner.problemId || "demo-problem",
      items: state.problemsList,
      itemToOption: (p) => ({ value: p.problem_id || p, label: p.problem_id || p, sub: p.problem_revision || p.revision }),
      onSelect: (val) => {
        state.candidateDesigner.problemId = val;
        const matched = state.problemsList.find(p => p.problem_id === val);
        if (matched) {
          const rev = matched.problem_revision || matched.revision || DEFAULT_SHA256_2;
          state.candidateDesigner.problemRev = rev;
          const revIn = document.getElementById("s-rev");
          if (revIn) revIn.value = rev;
        }
      }
    });
    r1.appendChild(probPicker.node);
    createCard.appendChild(r1);

    const matchedProblem = state.problemsList.find(p => p.problem_id === (state.candidateDesigner.problemId || "demo-problem"));
    const initialProbRev = (matchedProblem && (matchedProblem.problem_revision || matchedProblem.revision)) || state.candidateDesigner.problemRev || DEFAULT_SHA256_2;

    const r2 = el("div", "form-row");
    const revField = el("div", "form-field");
    revField.appendChild(el("label", "", txt(t("fieldProblemRevision"))));
    const revIn = el("input", { type: "text", id: "s-rev", className: "mono", value: initialProbRev });
    revField.appendChild(revIn);
    r2.appendChild(revField);

    const autoField = el("div", "form-field");
    autoField.appendChild(el("label", "", txt(t("fieldAutomationProfile"))));
    const autoIn = el("input", { type: "text", id: "s-auto", className: "mono", value: "standard" });
    autoField.appendChild(autoIn);
    r2.appendChild(autoField);
    createCard.appendChild(r2);

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
    createCard.appendChild(r3);

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

    createBtn.onclick = async () => {
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

      const studyIdVal = sIn.value.trim();
      const probIdVal = probPicker.getValue().trim();

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
        msg.textContent = (r && r.data && r.data.error) || t("netError");
        return;
      }

      state.candidateDesigner.studyId = studyIdVal;
      state.candidateDesigner.problemId = probIdVal;

      msg.className = "submit-msg ok";
      msg.textContent = t("msgStudyCreated");

      const advanceBtn = el("button", "plain primary", { style: "margin-top: 8px;" }, txt(t("readyAdvanceStep4")));
      advanceBtn.type = "button";
      advanceBtn.onclick = () => {
        activeStage = 5;
        navigate("#/compose?step=5");
      };
      msg.appendChild(el("div", { style: "margin-top: 6px;" }, advanceBtn));
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
  footerRow.appendChild(el("span", "baseline-meta meta mono", txt(t("workbenchBaselineMeta") || "科研评测控制台 · 5阶段评测工作台流水线")));
  footerRow.appendChild(el("span", "baseline-telemetry meta mono", txt(`${t("overviewLiveSync") || "实时同步: 正常"} · ${fmtClockTime(new Date())}`)));
  baselineFooter.appendChild(footerRow);
  container.appendChild(baselineFooter);

  return container;
}
