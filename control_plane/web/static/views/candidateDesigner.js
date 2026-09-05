/**
 * Candidate Designer & Manual Smoke-Test Evaluation View · Swiss Technical Typography
 * Stage 5 in the 5-stage composition pipeline.
 *
 * Vocabulary & Doctrine:
 * - Single earned action surface for cohesive candidate configuration & evaluation dispatch
 * - Target study context binding directly injected into study queues
 * - Bidirectional Form ↔ JSON sync with instant local preflight & server validation
 * - Dense parameter rows with direct numeric inputs, range sliders, and reset actions
 * - Verbatim priority posting and gated replicate key
 */

import { t, fmtNumericValue } from "../i18n.js";
import { state } from "../state.js";
import { el, txt, tip, chip, preflightBox, entityPicker, emptyPanel, technicalDetails, showError } from "../ui.js";
import { fetchJSON, postJSON, mockSchemas, validateCandidateLocally, IS_MOCK } from "../api.js";
import { entityName } from "../display.js";
import { navigate } from "../router.js";

const DEFAULT_SHA256 = "sha256:" + "0".repeat(64);

function normalizeSha256Revision(rev, fallback) {
  if (typeof rev === "string") {
    const trimmed = rev.trim();
    if (/^sha256:[0-9a-f]{64}$/i.test(trimmed)) return trimmed.toLowerCase();
    if (trimmed.startsWith("sha256:")) {
      const hex = trimmed.slice(7).replace(/[^0-9a-f]/gi, "").toLowerCase();
      if (hex.length > 0) return "sha256:" + hex.padEnd(64, "0").slice(0, 64);
    }
  }
  return fallback || DEFAULT_SHA256;
}

export function renderCandidateDesigner({ onSubmitted } = {}) {
  const cdState = state.candidateDesigner;
  const container = el("div", "candidate-workbench-view");

  if (!IS_MOCK) {
    const study = state.studiesList.find(row => row.study_id === cdState.studyId) || state.studiesList[0];
    if (!study) {
      container.appendChild(emptyPanel(t("chooseStudy"), t("createStudyFirst")));
      container.appendChild(el("button", "plain primary", {onclick: () => navigate("#/compose?step=4")}, t("btnNewStudy")));
      return container;
    }
    cdState.studyId = study.study_id;
    cdState.problemId = study.problem_id;
    cdState.problemRev = study.problem_revision;
  }

  // Collapsible Swiss Field Guide
  const guide = el("details", "guide-details");
  guide.appendChild(el("summary", "", el("span", "guide-icon", "ℹ "), txt(t("candidateGuideSummary"))));
  guide.appendChild(el("div", "details-content", txt(t("candidateGuide"))));
  container.appendChild(guide);

  // Single Earned Action Surface Container for Candidate Composition & Dispatch
  const actionSurface = el("section", "action-surface candidate-designer-surface");
  container.appendChild(actionSurface);
  let draftVersion = 0;
  function invalidatePreview() {
    draftVersion += 1;
    cdState.previewResult = null;
    cdState.lastSubmittedEval = null;
    const submit = actionSurface.querySelector("#btn-confirm-eval");
    if (submit) submit.disabled = true;
    const preview = actionSurface.querySelector("pre.preview");
    if (preview) preview.textContent = "";
  }
  actionSurface.addEventListener("input", invalidatePreview);
  actionSurface.addEventListener("change", invalidatePreview);

  // ================= 1. Target Study Injection Strip =================
  const studyTargetBox = el("div", "target-study-strip");
  const sHead = el("div", "target-study-strip-head");
  sHead.appendChild(el("h3", "target-study-strip-title", txt(t("targetStudyBoxTitle"))));
  studyTargetBox.appendChild(sHead);
  studyTargetBox.appendChild(el("div", "sub dim", { style: "font-size: 11.5px; margin-bottom: 10px;" }, txt(t("targetStudyBoxTip"))));

  const studyPickerRow = el("div", "form-row", { style: "align-items: flex-end; margin-bottom: 4px;" });
  const studyPicker = entityPicker({
    label: t("fieldTargetStudy"),
    id: "target-study-select",
    value: cdState.studyId,
    items: state.studiesList,
    itemToOption: (s) => ({
      value: s.study_id || s,
      label: entityName(s.study_id || s, "study")
    }),
    onSelect: (val) => {
      cdState.studyId = val;
      const matched = state.studiesList.find(s => (s.study_id || s) === val);
      if (matched && matched.problem_id) {
        cdState.problemId = matched.problem_id;
        const matchedProb = state.problemsList.find(p => p.problem_id === matched.problem_id && (p.problem_revision || p.revision) === matched.problem_revision);
        cdState.problemRev = normalizeSha256Revision(
          (matchedProb && (matchedProb.problem_revision || matchedProb.revision)) || matched.problem_revision,
          DEFAULT_SHA256
        );
        if (matchedProb && matchedProb.parameter_schema_revision) {
          revInput.value = matchedProb.parameter_schema_revision;
          loadSchema(matchedProb.parameter_schema_revision);
        } else refreshDesigner();
      }
    },
    placeholder: t("chooseStudy")
  });
  studyPickerRow.appendChild(studyPicker.node);

  const quickStudyAction = el("div", "form-field", { style: "display: flex; justify-content: flex-end;" });
  const newStudyBtn = el("button", "plain", {
    style: "font-size: 11.5px; align-self: flex-end; padding: 5px 10px;",
    onclick: () => navigate("#/compose?step=4")
  }, "➕ " + t("submitCardStudy"));
  quickStudyAction.appendChild(newStudyBtn);
  studyPickerRow.appendChild(quickStudyAction);
  studyTargetBox.appendChild(studyPickerRow);
  actionSurface.appendChild(studyTargetBox);

  // ================= 2. Schema Dereference Row =================
  const revRow = el("div", "form-row one", { style: "margin-top: 14px;" });
  const revField = el("div", "form-field");
  revField.appendChild(el("label", "", txt(t("fieldSchemaRevision"))));

  const revInputGroup = el("div", { style: "display: flex; gap: 8px; align-items: center;" });
  const boundProblem = state.problemsList.find(p => p.problem_id === cdState.problemId &&
    (p.problem_revision || p.revision) === cdState.problemRev) || state.problemsList.find(p => p.problem_id === cdState.problemId);
  const activeRev = (boundProblem && boundProblem.parameter_schema_revision) || cdState.schemaRev || state.schemaDraft.registeredRevision || state.packagesSchemaRev;
  if (activeRev !== cdState.schemaRev || cdState.loadedSchemaRev !== activeRev) {
    cdState.schemaRev = activeRev;
    cdState.schemaDoc = null;
    cdState.previewResult = null;
  }
  const revInput = el("input", {
    type: "text",
    id: "c-schema-rev",
    className: "mono",
    style: "flex: 1;",
    value: activeRev,
    placeholder: "sha256:..."
  });
  revInput.oninput = () => {
    cdState.schemaRev = revInput.value.trim();
  };

  const derefBtn = el("button", "plain primary", txt(t("btnDereferenceSchema")));
  derefBtn.type = "button";
  derefBtn.id = "btn-deref-schema";
  revInputGroup.appendChild(revInput);
  revInputGroup.appendChild(derefBtn);
  revField.appendChild(revInputGroup);
  revRow.appendChild(revField);
  actionSurface.appendChild(technicalDetails(t("templateConnectionDetails"), revRow));

  // Preset schema buttons
  const presetBar = el("div", "deck-samples", { style: "margin-top: 8px;" });
  presetBar.appendChild(el("span", "sample-label meta", txt(t("presetSchemas"))));

  function makePresetBtn(name, rev) {
    const b = el("button", "plain sample-pill-btn", txt(name));
    b.type = "button";
    b.onclick = () => {
      cdState.schemaRev = rev;
      revInput.value = rev;
      loadSchema(rev);
    };
    return b;
  }

  presetBar.appendChild(makePresetBtn(t("presetSolarTenJunction"), "sha256:mock-schema-ten-junction-v1"));
  presetBar.appendChild(makePresetBtn(t("presetCmosInverter"), "sha256:mock-schema-cmos-inverter-v1"));
  presetBar.appendChild(makePresetBtn(t("presetSpisPlasma"), "sha256:mock-schema-spis-plasma-v1"));

  if (state.schemaDraft.registeredRevision) {
    presetBar.appendChild(makePresetBtn(t("presetSchemaDraft", { rev: state.schemaDraft.registeredRevision.slice(0, 14) + "…" }), state.schemaDraft.registeredRevision));
  }
  if (IS_MOCK) actionSurface.appendChild(presetBar);

  const derefMsg = el("div", "submit-msg", { style: "margin-top: 4px; margin-bottom: 12px;" });
  actionSurface.appendChild(derefMsg);

  let schemaLoadSequence = 0;
  // Schema Loader
  async function loadSchema(rev) {
    const sequence = ++schemaLoadSequence;
    if (!rev) {
      derefMsg.className = "submit-msg err";
      derefMsg.textContent = t("errNoSchemaLoaded");
      return;
    }
    invalidatePreview();
    cdState.schemaDoc = null;
    refreshDesigner();
    derefBtn.disabled = true;
    derefMsg.className = "submit-msg";
    derefMsg.textContent = t("connecting");

    const r = await fetchJSON(`/api/schemas/${encodeURIComponent(rev)}`);
    if (sequence !== schemaLoadSequence) return;
    derefBtn.disabled = false;

    if (!r || !r.ok || !r.data) {
      derefMsg.className = "submit-msg err";
      showError(derefMsg, (r && r.data && r.data.error) || t("netError"));
      cdState.schemaDoc = null;
      return;
    }

    const doc = r.data.schema || r.data;
    cdState.schemaDoc = doc;
    cdState.schemaRev = rev;
    cdState.loadedSchemaRev = rev;

    // Initialize default params from schema
    const initialParams = {};
    (doc.parameters || []).forEach(p => {
      if (p.role === "variable") {
        initialParams[p.name] = (p.default !== undefined) ? p.default : (p.bounds ? p.bounds.min : 1.0);
      }
    });
    cdState.params = initialParams;
    cdState.rawJson = JSON.stringify(initialParams, null, 2);
    cdState.jsonError = null;

    // Initialize extracts
    const schemaExtracts = doc.extract_names || (doc.extracts || []).map(e => e.name || e) || ["voc", "jsc", "pmpp"];
    cdState.requestedOutputs = schemaExtracts.slice(0, 4);
    cdState.validation = validateCandidateLocally(doc, cdState.params);

    derefMsg.className = "submit-msg ok";
    derefMsg.textContent = t("schemaLoadedOk", { count: (doc.parameters || []).length });

    refreshDesigner();
  }

  derefBtn.onclick = () => loadSchema(revInput.value.trim());

  const effectiveSchemaRev = cdState.schemaRev || activeRev;

  // Dynamic Designer Workspace Area
  const dynamicArea = el("div", "designer-dynamic-area");
  actionSurface.appendChild(dynamicArea);

  function refreshDesigner() {
    dynamicArea.textContent = "";
    const doc = cdState.schemaDoc || (IS_MOCK && effectiveSchemaRev ? mockSchemas[effectiveSchemaRev] : null);

    if (!doc) {
      dynamicArea.appendChild(el("div", "dim sub", { style: "margin-top: 14px; font-style: italic;" }, txt(t("errNoSchemaLoaded"))));
      return;
    }

    // Mode Switcher Tabs
    const modeTabs = el("div", "mode-tabs");
    const tabForm = el("button", `mode-tab ${cdState.mode === "form" ? "active" : ""}`, txt(t("tabFormMode")));
    tabForm.type = "button";

    const tabJson = el("button", `mode-tab ${cdState.mode === "json" ? "active" : ""}`, txt(t("tabJsonMode")));
    tabJson.type = "button";
    if (cdState.jsonError) {
      tabJson.appendChild(el("span", "tab-badge-err", "!"));
    }
    tabForm.onclick = () => {
      cdState.mode = "form";
      refreshDesigner();
    };
    tabJson.onclick = () => {
      cdState.mode = "json";
      refreshDesigner();
    };

    modeTabs.appendChild(tabForm);
    modeTabs.appendChild(tabJson);
    dynamicArea.appendChild(modeTabs);

    const varParams = [];
    const fixParams = [];
    (doc.parameters || []).forEach(p => {
      if (p.role === "variable") varParams.push(p);
      else fixParams.push(p);
    });

    let onFieldValChange = null;

    // Form Mode Editor
    if (cdState.mode === "form") {
      const formWrap = el("div", "form-editor-wrap");
      const paramList = el("div", "param-editor-list");

      varParams.forEach(p => {
        const row = el("div", "param-editor-row");

        // Column 1: Meta (Name, Type, Unit, Bounds)
        const metaCol = el("div", "param-meta-col");
        metaCol.appendChild(el("span", "param-meta-name", txt(p.name)));

        const badgeRow = el("div", "param-meta-badges");
        badgeRow.appendChild(chip("info", t("type_" + (p.type || "float"))));
        if (p.unit) badgeRow.appendChild(chip("warn", p.unit));
        if (p.bounds) {
          badgeRow.appendChild(el("span", "mono sub", txt(`[${p.bounds.min}, ${p.bounds.max}]`)));
        }
        metaCol.appendChild(badgeRow);
        row.appendChild(metaCol);

        // Column 2: Direct numeric input + Slider
        const ctrlsCol = el("div", "param-ctrls-col");
        const curVal = cdState.params[p.name] !== undefined ? cdState.params[p.name] : (p.default !== undefined ? p.default : 0);

        const numIn = el("input", {
          type: (p.type === "float" || p.type === "int") ? "number" : "text",
          step: p.type === "float" ? "any" : (p.type === "int" ? "1" : undefined),
          value: curVal
        });

        function handleValChange(val) {
          cdState.params[p.name] = val;
          cdState.rawJson = JSON.stringify(cdState.params, null, 2);
          cdState.jsonError = null;
          cdState.validation = validateCandidateLocally(doc, cdState.params);
          if (onFieldValChange) onFieldValChange();
        }

        if (p.bounds && (p.type === "float" || p.type === "int")) {
          const slider = el("input", {
            type: "range",
            min: String(p.bounds.min),
            max: String(p.bounds.max),
            step: p.type === "int" ? "1" : "0.01",
            value: String(curVal)
          });

          slider.oninput = () => {
            const val = p.type === "int" ? parseInt(slider.value, 10) : parseFloat(slider.value);
            numIn.value = val;
            handleValChange(val);
          };

          numIn.oninput = () => {
            const val = (p.type === "float" || p.type === "int") ? Number(numIn.value) : numIn.value;
            slider.value = String(val);
            handleValChange(val);
          };

          ctrlsCol.appendChild(slider);
        } else {
          numIn.oninput = () => {
            const val = (p.type === "float" || p.type === "int") ? Number(numIn.value) : numIn.value;
            handleValChange(val);
          };
        }
        ctrlsCol.appendChild(numIn);
        row.appendChild(ctrlsCol);

        // Column 3: Reset Button
        const actCol = el("div", "param-actions-col");
        if (p.default !== undefined) {
          const rstBtn = el("button", "plain", { style: "padding: 2px 7px; font-size: 11px;" }, txt(`${t("resetToDefault")} (${p.default})`));
          rstBtn.type = "button";
          rstBtn.onclick = () => {
            numIn.value = p.default;
            handleValChange(p.default);
            refreshDesigner();
          };
          actCol.appendChild(rstBtn);
        }
        row.appendChild(actCol);

        paramList.appendChild(row);
      });
      formWrap.appendChild(paramList);

      if (fixParams.length > 0) {
        const fixBox = el("div", "dim sub", { style: "margin: 8px 0 14px; padding: 6px 10px; background: var(--paper-100); border-radius: var(--radius);" },
          txt(`${t("fixedParamsNotice", { count: fixParams.length })}: `),
          el("span", "mono", txt(fixParams.map(fp => `${fp.name}=${fp.value !== undefined ? fmtNumericValue(fp.value) : "const"}`).join(", ")))
        );
        formWrap.appendChild(fixBox);
      }
      dynamicArea.appendChild(formWrap);
    } else {
      // Raw JSON Mode
      const jsonWrap = el("div", "json-editor-wrap");
      const rawTa = el("textarea", {
        id: "c-raw-json",
        className: "deck-textarea mono",
        style: "min-height: 200px; margin-bottom: 12px;",
        value: cdState.rawJson
      });

      rawTa.oninput = () => {
        cdState.rawJson = rawTa.value;
        try {
          const parsed = JSON.parse(rawTa.value);
          if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
            throw new Error("JSON root must be a dictionary object {...}");
          }
          cdState.jsonError = null;
          cdState.params = parsed;
          cdState.validation = validateCandidateLocally(doc, cdState.params);
        } catch (e) {
          cdState.jsonError = e.message;
          cdState.validation = { valid: false, issues: [{ code: "syntax_error", message: e.message }] };
        }
        if (onFieldValChange) onFieldValChange();
      };

      jsonWrap.appendChild(rawTa);
      if (cdState.jsonError) {
        jsonWrap.appendChild(el("div", "syntax-block-alert", txt(t("jsonParseErrorPrefix", { err: cdState.jsonError }))));
      }
      dynamicArea.appendChild(jsonWrap);
    }

    // Preflight Status Box
    const preflightContainer = el("div", "preflight-container", { style: "margin-bottom: 10px;" });
    dynamicArea.appendChild(preflightContainer);

    // Server Validate Button
    const srvValBtn = el("button", "plain", txt(t("btnServerValidate")));
    srvValBtn.type = "button";
    srvValBtn.id = "btn-server-validate";
    srvValBtn.onclick = async () => {
      if (cdState.jsonError) return;
      srvValBtn.disabled = true;
      const r = await postJSON("/api/candidates/validate", {
        schema_revision: cdState.schemaRev,
        parameters: cdState.params
      });
      srvValBtn.disabled = false;
      if (r && r.ok && r.data) {
        cdState.serverValidationResult = r.data;
        cdState.validation = r.data;
        updatePreflightSection();
      } else {
        cdState.serverValidationResult = null;
        cdState.previewResult = null;
        cdState.validation = {
          valid: false,
          issues: [{ code: "server_validation_error", message: (r && r.data && r.data.error) || t("netError") }]
        };
        updatePreflightSection();
      }
    };
    dynamicArea.appendChild(srvValBtn);

    // Evaluation Controls Sub-Section
    const evSectionTitle = el("h3", "action-surface-heading", { style: "margin-top: 20px;" },
      txt(t("evalDispatchTitle"))
    );
    dynamicArea.appendChild(evSectionTitle);

    // Problem ID & Revision Row
    const matchedProblem = state.problemsList.find(p => p.problem_id === cdState.problemId && (p.problem_revision || p.revision) === cdState.problemRev);
    const matchedProbRev = matchedProblem && (matchedProblem.problem_revision || matchedProblem.revision);
    const effectiveProbRev = normalizeSha256Revision(
      matchedProbRev || cdState.problemRev,
      DEFAULT_SHA256
    );
    cdState.problemRev = effectiveProbRev;
    const probRow = el("div", "form-row");
    probRow.appendChild(el("label", "form-field", t("fieldProblemIdTechnical"),
      el("input", {id: "e-pid", value: cdState.problemId, readOnly: true})));

    const prevField = el("div", "form-field");
    prevField.appendChild(el("label", "", txt(t("fieldProblemRevision"))));
    const prevIn = el("input", { type: "text", id: "e-prev", className: "mono", value: cdState.problemRev, readOnly: true });
    prevField.appendChild(prevIn);
    probRow.appendChild(prevField);
    const runAdvanced = technicalDetails(t("advancedSettings"), probRow);

    // Fidelity Row
    const fidRow = el("div", "form-row one");
    const fidField = el("div", "form-field");
    fidField.appendChild(el("label", "", txt(t("fieldFidelity")), tip(null, "tipFidelity")));
    const fidIn = el("input", { type: "text", id: "e-fid", value: cdState.fidelity || "high" });
    fidIn.oninput = () => { cdState.fidelity = fidIn.value.trim(); };
    fidField.appendChild(fidIn);
    fidRow.appendChild(fidField);
    dynamicArea.appendChild(fidRow);

    // Requested Outputs (Checkboxes + Custom Add)
    const outputsSection = el("div", "form-field", { style: "margin-bottom: 12px;" });
    outputsSection.appendChild(el("label", "", txt(t("extractsTitle"))));
    outputsSection.appendChild(el("div", "form-help", txt(t("extractsDesc"))));

    const declaredExtracts = (doc.extract_names || (doc.extracts || []).map(e => e.name || e) || ["voc", "jsc", "pmpp"]).slice();
    cdState.requestedOutputs.forEach(out => {
      if (!declaredExtracts.includes(out)) declaredExtracts.push(out);
    });

    const outListWrap = el("div", "outputs-list-grid");
    function renderExtractCheckboxes() {
      outListWrap.textContent = "";
      declaredExtracts.forEach(extName => {
        const isChecked = cdState.requestedOutputs.includes(extName);
        const lbl = el("label", "output-checkbox-label");
        const chk = el("input", { type: "checkbox", value: extName, checked: isChecked });
        chk.onchange = () => {
          if (chk.checked) {
            if (!cdState.requestedOutputs.includes(extName)) cdState.requestedOutputs.push(extName);
          } else {
            cdState.requestedOutputs = cdState.requestedOutputs.filter(x => x !== extName);
          }
        };
        lbl.appendChild(chk);
        lbl.appendChild(el("span", "", txt(extName)));
        outListWrap.appendChild(lbl);
      });
    }
    renderExtractCheckboxes();
    outputsSection.appendChild(outListWrap);

    // Custom extract add input
    const customRow = el("div", "output-custom-row");
    const customIn = el("input", {
      type: "text",
      placeholder: t("customExtractPlaceholder"),
      value: cdState.customExtractInput || ""
    });
    customIn.oninput = () => { cdState.customExtractInput = customIn.value.trim(); };

    const addExtractBtn = el("button", "plain", txt(t("btnAddExtract")));
    addExtractBtn.type = "button";
    addExtractBtn.onclick = () => {
      const val = customIn.value.trim();
      if (!val) return;
      if (!declaredExtracts.includes(val)) declaredExtracts.push(val);
      if (!cdState.requestedOutputs.includes(val)) cdState.requestedOutputs.push(val);
      customIn.value = "";
      cdState.customExtractInput = "";
      invalidatePreview();
      renderExtractCheckboxes();
    };
    customRow.appendChild(customIn);
    customRow.appendChild(addExtractBtn);
    outputsSection.appendChild(customRow);
    dynamicArea.appendChild(outputsSection);

    // Evidence Profile & Independence Row
    const indepRow = el("div", "form-row");
    const evField = el("div", "form-field");
    evField.appendChild(el("label", "", txt(t("fieldEvidenceProfile"))));
    const evIn = el("input", { type: "text", id: "e-evidence", value: cdState.evidenceProfile || "default" });
    evIn.oninput = () => { cdState.evidenceProfile = evIn.value.trim(); };
    evField.appendChild(evIn);
    indepRow.appendChild(evField);

    const indField = el("div", "form-field");
    indField.appendChild(el("label", "", txt(t("fieldIndepReq"))));
    const indSelect = el("select", { id: "e-ind" });
    [
      { val: "normal", label: t("indepNormal") },
      { val: "independent", label: t("indepIndependent") }
    ].forEach(opt => {
      const o = el("option", { value: opt.val }, txt(opt.label));
      if (cdState.independence === opt.val) o.selected = true;
      indSelect.appendChild(o);
    });
    indSelect.onchange = () => {
      cdState.independence = indSelect.value;
      updateReplicateKeyGating();
    };
    indField.appendChild(indSelect);
    indepRow.appendChild(indField);
    runAdvanced.lastElementChild.appendChild(indepRow);

    // Priority (verbatim text) & Replicate Key (gated) Row
    const prioRow = el("div", "form-row");
    const prioField = el("div", "form-field");
    prioField.appendChild(el("label", "", txt(t("fieldPriority")), tip(null, "tipPriority")));
    const prioSelect = el("select", { id: "e-priority" });
    [
      { val: "low", label: t("priorityLow") },
      { val: "normal", label: t("priorityNormal") },
      { val: "high", label: t("priorityHigh") }
    ].forEach(opt => {
      const o = el("option", { value: opt.val }, txt(opt.label));
      if (cdState.priority === opt.val) o.selected = true;
      prioSelect.appendChild(o);
    });
    prioSelect.onchange = () => {
      cdState.priority = prioSelect.value; // POSTED VERBATIM AS TEXT
    };
    prioField.appendChild(prioSelect);
    prioRow.appendChild(prioField);

    const repField = el("div", "form-field");
    const repLabel = el("label", "", txt(t("fieldRepKey")));
    repField.appendChild(repLabel);
    const repIn = el("input", {
      type: "text",
      id: "e-rep",
      placeholder: "rep-uuid-xxxx",
      value: cdState.replicateKey || ""
    });
    repIn.oninput = () => { cdState.replicateKey = repIn.value.trim(); };
    repField.appendChild(repIn);
    const repHelp = el("div", "form-help", txt(""));
    repField.appendChild(repHelp);
    prioRow.appendChild(repField);
    runAdvanced.lastElementChild.appendChild(prioRow);
    dynamicArea.appendChild(runAdvanced);

    function updateReplicateKeyGating() {
      const isIndependent = cdState.independence === "independent";
      repIn.disabled = !isIndependent;
      if (!isIndependent) {
        repIn.style.opacity = "0.5";
        repIn.title = t("replicateKeyDisabledTip");
        repHelp.textContent = t("replicateKeyDisabledTip");
      } else {
        repIn.style.opacity = "1";
        repIn.title = t("replicateKeyEnabledTip");
        repHelp.textContent = t("replicateKeyEnabledTip");
      }
    }
    updateReplicateKeyGating();

    // Action Buttons & Feedback Preview
    const actRow = el("div", { style: "display: flex; gap: 8px; align-items: center; margin-top: 16px; flex-wrap: wrap;" });
    const previewBtn = el("button", "plain", txt(t("btnGenCandPreview")));
    previewBtn.type = "button";
    previewBtn.id = "btn-gen-preview";

    const submitBtn = el("button", "plain primary", txt(t("btnSubmitIntoStudy")));
    submitBtn.type = "button";
    submitBtn.id = "btn-confirm-eval";
    submitBtn.disabled = true;

    actRow.appendChild(previewBtn);
    actRow.appendChild(submitBtn);
    dynamicArea.appendChild(actRow);

    const em = el("div", "submit-msg", { style: "margin-top: 8px;" });
    const ep = el("pre", "preview log-well", { style: "margin-top: 10px;" });
    dynamicArea.appendChild(em);
    dynamicArea.appendChild(technicalDetails(t("requestDetails"), ep));

    function updateActionButtonsState() {
      const isBlocked = !!cdState.jsonError || !cdState.validation || !cdState.validation.valid;
      srvValBtn.disabled = !!cdState.jsonError;
      previewBtn.disabled = isBlocked;
      if (isBlocked || !cdState.previewResult) {
        submitBtn.disabled = true;
      } else {
        submitBtn.disabled = !state.writes;
        if (!state.writes) submitBtn.title = t("needAllowWrites");
      }
    }

    function updatePreflightSection() {
      preflightContainer.textContent = "";
      preflightContainer.appendChild(preflightBox(cdState.validation, cdState.jsonError));
      updateActionButtonsState();
    }

    onFieldValChange = () => {
      cdState.previewResult = null;
      cdState.lastSubmittedEval = null;
      updatePreflightSection();
    };

    updatePreflightSection();

    if (cdState.previewResult) {
      ep.textContent = JSON.stringify(cdState.previewResult, null, 2);
    }
    if (cdState.lastSubmittedEval) {
      const { evalId, studyId } = cdState.lastSubmittedEval;
      em.className = "submit-msg ok";
      em.textContent = t("msgEvalSubmitted");
      const toQueueBtn = el("button", "plain primary", {
        style: "margin-top: 8px;",
        onclick: () => {
          navigate(`#/study/${encodeURIComponent(studyId)}`);
        }
      }, t("btnGoToStudyQueue", { id: studyId }));
      em.appendChild(el("div", { style: "margin-top: 6px;" }, toQueueBtn));
    }

    // Generate Preview Action
    previewBtn.onclick = async () => {
      if (cdState.jsonError || !cdState.validation || !cdState.validation.valid) {
        em.className = "submit-msg err";
        em.textContent = t("errFixValidationBeforeSubmit");
        return;
      }

      const version = draftVersion;
      previewBtn.disabled = true;
      em.className = "submit-msg";
      em.textContent = t("connecting");

      const probId = cdState.problemId || (state.problemsList[0] && state.problemsList[0].problem_id) || "demo-problem";
      const matchedProb = state.problemsList.find(p => p.problem_id === probId && (p.problem_revision || p.revision) === cdState.problemRev);
      const matchedProbRev = matchedProb && (matchedProb.problem_revision || matchedProb.revision);
      const probRev = normalizeSha256Revision(
        matchedProbRev || cdState.problemRev,
        DEFAULT_SHA256
      );
      const candSpec = {
        problem_id: probId,
        problem_revision: probRev,
        parameters: cdState.params
      };

      const cr = await postJSON("/api/contracts/build", { kind: "candidate", spec: candSpec });
      if (version !== draftVersion) { previewBtn.disabled = false; return; }
      if (!cr || !cr.ok || !cr.data) {
        previewBtn.disabled = false;
        em.className = "submit-msg err";
        showError(em, (cr && cr.data && cr.data.error) || t("netError"));
        return;
      }

      cdState.candidateContract = cr.data.contract;

      const reqSpec = {
        candidate_id: cdState.candidateContract.candidate_id,
        fidelity: cdState.fidelity || "high",
        requested_outputs: cdState.requestedOutputs,
        evidence_profile: cdState.evidenceProfile || "default",
        independence_requirement: cdState.independence || "normal",
        priority: cdState.priority || "normal", // POSTED VERBATIM AS TEXT!
        replicate_key: (cdState.independence === "independent" && cdState.replicateKey) ? cdState.replicateKey : undefined
      };

      const rr = await postJSON("/api/contracts/build", { kind: "evaluation_request", spec: reqSpec });
      previewBtn.disabled = false;
      if (version !== draftVersion) return;

      if (!rr || !rr.ok || !rr.data) {
        em.className = "submit-msg err";
        showError(em, (rr && rr.data && rr.data.error) || t("netError"));
        return;
      }

      cdState.requestContract = rr.data.contract;
      cdState.previewResult = { candidate: cdState.candidateContract, evaluation_request: cdState.requestContract };

      em.className = "submit-msg ok";
      em.textContent = `${t("msgPreviewDone")} (${t("targetStudyLabel", { id: entityName(cdState.studyId, "study") })})`;
      ep.textContent = JSON.stringify(cdState.previewResult, null, 2);

      updateActionButtonsState();
    };

    // Direct Evaluation Submission into Target Study Action
    submitBtn.onclick = async () => {
      if (!state.writes) {
        em.className = "submit-msg err";
        em.textContent = t("readonlyNote");
        return;
      }
      if (!cdState.previewResult) return;

      submitBtn.disabled = true;
      em.className = "submit-msg";
      em.textContent = t("connecting");

      const targetStudyId = (studyPicker && studyPicker.getValue && studyPicker.getValue()) || cdState.studyId || (state.studiesList[0] && (state.studiesList[0].study_id || state.studiesList[0])) || "demo-study-a";
      const r = await postJSON("/api/evaluations", {
        study_id: targetStudyId,
        candidate: cdState.candidateContract,
        request: cdState.requestContract
      });
      submitBtn.disabled = false;

      if (!r || !r.ok || !r.data) {
        em.className = "submit-msg err";
        showError(em, (r && r.data && r.data.error) || t("netError"));
        return;
      }

      const evalId = r.data.evaluation_id || "eval:mock-created";
      cdState.studyId = targetStudyId;
      cdState.lastSubmittedEval = { evalId, studyId: targetStudyId };
      em.className = "submit-msg ok";
      em.textContent = `🚀 ${t("msgEvalSubmitted")} Evaluation ID: ${evalId} ➔ ${t("targetStudyLabel", { id: targetStudyId })}`;

      const toQueueBtn = el("button", "plain primary", {
        style: "margin-top: 8px;",
        onclick: () => {
          if (cdState.studyId) {
            navigate(`#/study/${encodeURIComponent(cdState.studyId)}`);
          } else {
            navigate("#/compose?step=4");
          }
        }
      }, t("btnGoToStudyQueue", { id: targetStudyId }));
      em.appendChild(el("div", { style: "margin-top: 6px;" }, toQueueBtn));
      if (onSubmitted) onSubmitted(r.data);
    };
  }

  // Auto load mock schema if available
  if (!cdState.schemaDoc && effectiveSchemaRev) {
    if (IS_MOCK && mockSchemas[effectiveSchemaRev]) {
      cdState.schemaDoc = mockSchemas[effectiveSchemaRev];
      cdState.schemaRev = effectiveSchemaRev;
      cdState.loadedSchemaRev = effectiveSchemaRev;
      if (!cdState.params || Object.keys(cdState.params).length === 0) {
        const initialParams = {};
        (cdState.schemaDoc.parameters || []).forEach(p => {
          if (p.role === "variable") {
            initialParams[p.name] = (p.default !== undefined) ? p.default : (p.bounds ? p.bounds.min : 1.0);
          }
        });
        cdState.params = initialParams;
        cdState.rawJson = JSON.stringify(initialParams, null, 2);
      }
      cdState.validation = validateCandidateLocally(cdState.schemaDoc, cdState.params);
    } else {
      loadSchema(effectiveSchemaRev);
    }
  }

  refreshDesigner();
  return container;
}
