/**
 * Schemas View & Persistent Schema Draft Editor · Swiss Technical Typography
 * Stage 2 in the 5-stage composition pipeline.
 *
 * Vocabulary & Doctrine:
 * - Collapsible Swiss field guide
 * - Flat rule-delimited sections with typographic hierarchy
 * - Dense technical tables with constrained monospace inputs & zero horizontal overflow
 * - Real-time Form ↔ JSON bidirectional synchronization with persistent state
 * - Clear immutable revision tagging and downstream handoffs
 */

import { t, fmtNumericValue } from "../i18n.js";
import { state } from "../state.js";
import { el, txt, pageHead, emptyPanel, errorBlock, chip, monoHash } from "../ui.js";
import { fetchJSON, postJSON, mockSchemas } from "../api.js";
import { navigate } from "../router.js";

const PRESET_SCHEMAS = {
  solar: {
    kind: "parameter-schema",
    problem_hint: "ten-junction-thickness-tcad",
    parameters: [
      { name: "t_total1", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 0.551, deck_line: 3 },
      { name: "t_total2", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 0.820, deck_line: 4 },
      { name: "t_total3", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 1.250, deck_line: 5 },
      { name: "t_total4", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 1.640, deck_line: 6 },
      { name: "t_total5", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 2.100, deck_line: 7 },
      { name: "mesh_bias", type: "float", role: "fixed", value: 1.0, deck_line: 10 },
      { name: "ambient_temperature", type: "float", unit: "K", role: "fixed", value: 300.0, deck_line: 12 }
    ],
    extracts: [
      { name: "voc", expression: 'max(v."anode")' },
      { name: "jsc", expression: 'max(i."cathode")' },
      { name: "pmpp", expression: 'max(v."anode"*i."cathode")' }
    ],
    extract_names: ["voc", "jsc", "pmpp"]
  },
  cmos: {
    kind: "parameter-schema",
    problem_hint: "cmos-inverter-delay-opt",
    parameters: [
      { name: "wn_width_nm", type: "float", unit: "nm", role: "variable", bounds: { min: 50, max: 1000 }, default: 120.0, deck_line: 3 },
      { name: "wp_width_nm", type: "float", unit: "nm", role: "variable", bounds: { min: 50, max: 2000 }, default: 240.0, deck_line: 4 },
      { name: "channel_length_nm", type: "float", unit: "nm", role: "fixed", value: 45.0, deck_line: 5 },
      { name: "supply_voltage_v", type: "float", unit: "V", role: "variable", bounds: { min: 0.6, max: 1.2 }, default: 0.9, deck_line: 8 },
      { name: "temperature_k", type: "float", unit: "K", role: "fixed", value: 300.0, deck_line: 10 }
    ],
    extracts: [
      { name: "tpdr", expression: "max(time)" },
      { name: "power_uw", expression: 'max(i."vdd"*v."vdd")' }
    ],
    extract_names: ["tpdr", "power_uw"]
  },
  plasma: {
    kind: "parameter-schema",
    problem_hint: "spis-plasma-charging",
    parameters: [
      { name: "plasma_density_m3", type: "float", unit: "m^-3", role: "variable", bounds: { min: 1e6, max: 1e12 }, default: 1.0e8, deck_line: 3 },
      { name: "electron_temp_ev", type: "float", unit: "eV", role: "variable", bounds: { min: 0.5, max: 20.0 }, default: 2.5, deck_line: 4 },
      { name: "ion_temp_ev", type: "float", unit: "eV", role: "fixed", value: 0.1, deck_line: 5 },
      { name: "bias_potential_v", type: "float", unit: "V", role: "variable", bounds: { min: -100.0, max: 100.0 }, default: 0.0, deck_line: 6 }
    ],
    extracts: [
      { name: "floating_pot_v", expression: 'max(v."probe")' }
    ],
    extract_names: ["floating_pot_v"]
  }
};

export async function renderSchemasView({ revision } = {}) {
  const container = el("div", "schemas-view");

  if (revision) {
    return renderSchemaDetail(revision, container);
  }

  // Collapsible Swiss Field Guide
  const guide = el("details", "guide-details");
  guide.appendChild(el("summary", "", el("span", "guide-icon", "ℹ "), txt(t("schemasGuideSummary"))));
  guide.appendChild(el("div", "details-content", txt(t("schemasGuide"))));
  container.appendChild(guide);

  // ================= Section 1: Persistent Schema Draft Workspace =================
  const draftSection = el("section", "rule-section schema-workbench-section");
  container.appendChild(draftSection);

  function renderDraftWorkspace() {
    draftSection.textContent = "";
    const draft = state.schemaDraft;

    // Preset Seed Buttons Bar
    const sampleBar = el("div", "deck-samples");
    sampleBar.appendChild(el("span", "sample-label meta", txt(t("presetSchemas"))));

    function schemaSampleBtn(name, sampleObj) {
      const b = el("button", "plain sample-pill-btn", txt(name));
      b.type = "button";
      b.onclick = () => {
        draft.doc = JSON.parse(JSON.stringify(sampleObj));
        draft.rawJson = JSON.stringify(sampleObj, null, 2);
        draft.jsonError = null;
        draft.registeredRevision = null;
        renderDraftWorkspace();
      };
      return b;
    }

    sampleBar.appendChild(schemaSampleBtn(t("presetSolarTenJunction"), PRESET_SCHEMAS.solar));
    sampleBar.appendChild(schemaSampleBtn(t("presetCmosInverter"), PRESET_SCHEMAS.cmos));
    sampleBar.appendChild(schemaSampleBtn(t("presetSpisPlasma"), PRESET_SCHEMAS.plasma));

    const clearDraftBtn = el("button", "plain clear-deck-btn", txt(t("btnClearDraft")));
    clearDraftBtn.type = "button";
    clearDraftBtn.onclick = () => {
      draft.doc = {
        kind: "parameter-schema",
        problem_hint: "custom-schema",
        parameters: [
          { name: "param_1", type: "float", role: "variable", bounds: { min: 0.1, max: 10.0 }, default: 1.0 }
        ],
        extracts: [
          { name: "metric_1", expression: "max(output)" }
        ],
        extract_names: ["metric_1"]
      };
      draft.rawJson = JSON.stringify(draft.doc, null, 2);
      draft.jsonError = null;
      draft.registeredRevision = null;
      renderDraftWorkspace();
    };
    sampleBar.appendChild(clearDraftBtn);
    draftSection.appendChild(sampleBar);

    // Registration Success Banner or Draft Status Strip
    if (draft.registeredRevision) {
      const regBanner = el("div", "schema-registered-banner");
      const titleSpan = el("div", "schema-registered-title", txt(`✓ ${t("schemaDraftRegistered", { rev: draft.registeredRevision.slice(0, 16) + "…" })}`));
      regBanner.appendChild(titleSpan);

      const regActions = el("div", { style: "display: flex; gap: 8px; flex-wrap: wrap;" });
      const newDraftBtn = el("button", "plain", txt(t("btnStartNewDraftFromThis")));
      newDraftBtn.type = "button";
      newDraftBtn.onclick = () => {
        draft.registeredRevision = null;
        renderDraftWorkspace();
      };
      regActions.appendChild(newDraftBtn);

      const toProbBtn = el("button", "plain", txt("📐 " + t("toStepProblem")));
      toProbBtn.type = "button";
      toProbBtn.onclick = () => {
        state.candidateDesigner.schemaRev = draft.registeredRevision;
        navigate("#/compose?step=3");
      };
      regActions.appendChild(toProbBtn);

      const toCandBtn = el("button", "plain primary", txt("⚡ " + t("toStepCandidate")));
      toCandBtn.type = "button";
      toCandBtn.onclick = () => {
        state.candidateDesigner.schemaRev = draft.registeredRevision;
        navigate("#/compose?step=5");
      };
      regActions.appendChild(toCandBtn);

      regBanner.appendChild(regActions);
      draftSection.appendChild(regBanner);
    } else {
      const draftNotice = el("div", "schema-draft-status-strip");
      const pCount = (draft.doc.parameters || []).length;
      const vCount = (draft.doc.parameters || []).filter(p => p.role === "variable").length;
      const fCount = pCount - vCount;
      const eCount = (draft.doc.extracts || draft.doc.extract_names || []).length;

      draftNotice.appendChild(el("span", "dim", txt(t("schemaDraftNotice"))));
      draftNotice.appendChild(el("span", "mono sub", txt(t("schemaDraftSummary", { params: pCount, vars: vCount, fixed: fCount, extracts: eCount }))));
      draftSection.appendChild(draftNotice);
    }

    // Mode Switcher Tabs (Form vs JSON)
    const modeTabs = el("div", "mode-tabs");
    const tabForm = el("button", `mode-tab ${draft.mode === "form" ? "active" : ""}`, txt(t("tabFormMode")));
    tabForm.type = "button";

    const tabJson = el("button", `mode-tab ${draft.mode === "json" ? "active" : ""}`, txt(t("tabJsonMode")));
    tabJson.type = "button";
    if (draft.jsonError) {
      tabJson.appendChild(el("span", "tab-badge-err", "!"));
    }

    tabForm.onclick = () => {
      draft.mode = "form";
      renderDraftWorkspace();
    };
    tabJson.onclick = () => {
      draft.mode = "json";
      renderDraftWorkspace();
    };

    modeTabs.appendChild(tabForm);
    modeTabs.appendChild(tabJson);
    draftSection.appendChild(modeTabs);

    // ================= Form Mode Editor =================
    if (draft.mode === "form") {
      const formEditor = el("div", "schema-form-editor");

      // Problem Hint Input
      const hintRow = el("div", "form-row one", { style: "margin-bottom: 14px;" });
      const hintField = el("div", "form-field");
      hintField.appendChild(el("label", "", txt(t("fieldProblemHint"))));
      const hintIn = el("input", {
        type: "text",
        value: draft.doc.problem_hint || "custom-schema",
        placeholder: "solar-cell-tcad"
      });
      hintIn.oninput = () => {
        draft.doc.problem_hint = hintIn.value.trim();
        draft.rawJson = JSON.stringify(draft.doc, null, 2);
        draft.registeredRevision = null;
      };
      hintField.appendChild(hintIn);
      hintRow.appendChild(hintField);
      formEditor.appendChild(hintRow);

      // Parameters Controls Header
      const paramsHead = el("div", "section-filter-bar", { style: "margin: 16px 0 8px;" });
      const pTitle = el("h3", "section-title",
        txt(`${t("sectionParameters")} (${(draft.doc.parameters || []).length})`)
      );
      paramsHead.appendChild(pTitle);

      const pActions = el("div", { style: "display: flex; gap: 6px; flex-wrap: wrap;" });
      const addParamBtn = el("button", "plain primary", txt(t("btnAddParam")));
      addParamBtn.type = "button";
      addParamBtn.onclick = () => {
        if (!draft.doc.parameters) draft.doc.parameters = [];
        const newIdx = draft.doc.parameters.length + 1;
        draft.doc.parameters.push({
          name: `param_${newIdx}`,
          type: "float",
          role: "variable",
          bounds: { min: 0.1, max: 10.0 },
          default: 1.0
        });
        draft.rawJson = JSON.stringify(draft.doc, null, 2);
        draft.registeredRevision = null;
        renderDraftWorkspace();
      };

      const setVarBtn = el("button", "plain", txt(t("setAllVariable")));
      setVarBtn.type = "button";
      setVarBtn.onclick = () => {
        (draft.doc.parameters || []).forEach(p => {
          p.role = "variable";
          if (!p.bounds) p.bounds = { min: 0.1, max: 10.0 };
          if (p.default === undefined && p.value !== undefined) {
            p.default = p.value;
            delete p.value;
          }
        });
        draft.rawJson = JSON.stringify(draft.doc, null, 2);
        draft.registeredRevision = null;
        renderDraftWorkspace();
      };

      const setFixBtn = el("button", "plain", txt(t("setAllFixed")));
      setFixBtn.type = "button";
      setFixBtn.onclick = () => {
        (draft.doc.parameters || []).forEach(p => {
          p.role = "fixed";
          if (p.value === undefined && p.default !== undefined) {
            p.value = p.default;
            delete p.default;
          }
        });
        draft.rawJson = JSON.stringify(draft.doc, null, 2);
        draft.registeredRevision = null;
        renderDraftWorkspace();
      };

      pActions.appendChild(addParamBtn);
      pActions.appendChild(setVarBtn);
      pActions.appendChild(setFixBtn);
      paramsHead.appendChild(pActions);
      formEditor.appendChild(paramsHead);

      // Dense Parameters Table
      const pTableWrap = el("div", "table-dense-wrap");
      const pTable = el("table", "data-table table-dense");
      const pThead = el("thead");
      const pTrH = el("tr");
      pTrH.appendChild(el("th", "", txt(t("thParamName"))));
      pTrH.appendChild(el("th", "", txt(t("thParamRoleCol"))));
      pTrH.appendChild(el("th", "", txt(t("thParamTypeCol"))));
      pTrH.appendChild(el("th", "", txt(t("thParamBoundsCol"))));
      pTrH.appendChild(el("th", "", txt(t("thParamValueCol"))));
      pTrH.appendChild(el("th", "", txt(t("thParamUnitCol"))));
      pTrH.appendChild(el("th", "num", txt(t("thActions"))));
      pThead.appendChild(pTrH);
      pTable.appendChild(pThead);

      const pTbody = el("tbody");
      (draft.doc.parameters || []).forEach((p, idx) => {
        const tr = el("tr");

        // Name
        const tdName = el("td");
        const nameIn = el("input", {
          type: "text",
          value: p.name || "",
          style: "width: 150px; font-family: var(--font-mono); font-weight: 600;"
        });
        nameIn.oninput = () => {
          p.name = nameIn.value.trim();
          draft.rawJson = JSON.stringify(draft.doc, null, 2);
          draft.registeredRevision = null;
        };
        tdName.appendChild(nameIn);
        tr.appendChild(tdName);

        // Role Select
        const tdRole = el("td");
        const roleSel = el("select", { style: "width: 95px;" });
        const optV = el("option", { value: "variable" }, txt(t("roleVariable")));
        const optF = el("option", { value: "fixed" }, txt(t("roleFixed")));
        if (p.role === "fixed") optF.selected = true;
        else optV.selected = true;
        roleSel.appendChild(optV);
        roleSel.appendChild(optF);
        roleSel.onchange = () => {
          p.role = roleSel.value;
          if (p.role === "variable" && !p.bounds) {
            p.bounds = { min: 0.1, max: 10.0 };
            p.default = p.value !== undefined ? p.value : 1.0;
            delete p.value;
          } else if (p.role === "fixed") {
            p.value = p.default !== undefined ? p.default : 1.0;
            delete p.default;
          }
          draft.rawJson = JSON.stringify(draft.doc, null, 2);
          draft.registeredRevision = null;
          renderDraftWorkspace();
        };
        tdRole.appendChild(roleSel);
        tr.appendChild(tdRole);

        // Type Select
        const tdType = el("td");
        const typeSel = el("select", { style: "width: 80px;" });
        ["float", "int", "bool", "string"].forEach(typ => {
          const o = el("option", { value: typ }, typ);
          if (p.type === typ) o.selected = true;
          typeSel.appendChild(o);
        });
        typeSel.onchange = () => {
          p.type = typeSel.value;
          draft.rawJson = JSON.stringify(draft.doc, null, 2);
          draft.registeredRevision = null;
        };
        tdType.appendChild(typeSel);
        tr.appendChild(tdType);

        // Bounds
        const tdBounds = el("td");
        if (p.role === "variable") {
          const bWrap = el("div", { style: "display: flex; gap: 4px; align-items: center;" });
          const curMin = (p.bounds && p.bounds.min !== undefined) ? p.bounds.min : 0.1;
          const curMax = (p.bounds && p.bounds.max !== undefined) ? p.bounds.max : 10.0;
          const minIn = el("input", { type: "number", value: curMin, step: "any", style: "width: 55px; font-family: var(--font-mono);" });
          const maxIn = el("input", { type: "number", value: curMax, step: "any", style: "width: 55px; font-family: var(--font-mono);" });
          minIn.oninput = () => {
            if (!p.bounds) p.bounds = {};
            p.bounds.min = parseFloat(minIn.value);
            draft.rawJson = JSON.stringify(draft.doc, null, 2);
            draft.registeredRevision = null;
          };
          maxIn.oninput = () => {
            if (!p.bounds) p.bounds = {};
            p.bounds.max = parseFloat(maxIn.value);
            draft.rawJson = JSON.stringify(draft.doc, null, 2);
            draft.registeredRevision = null;
          };
          bWrap.appendChild(minIn);
          bWrap.appendChild(txt(" ~ "));
          bWrap.appendChild(maxIn);
          tdBounds.appendChild(bWrap);
        } else {
          tdBounds.appendChild(el("span", "dim sub", txt(t("fixedNotice"))));
        }
        tr.appendChild(tdBounds);

        // Default / Value
        const tdVal = el("td");
        const valIn = el("input", {
          type: (p.type === "float" || p.type === "int") ? "number" : "text",
          step: p.type === "float" ? "any" : (p.type === "int" ? "1" : undefined),
          value: p.role === "variable" ? (p.default !== undefined ? p.default : "") : (p.value !== undefined ? p.value : ""),
          style: "width: 80px; font-family: var(--font-mono);"
        });
        valIn.oninput = () => {
          const raw = valIn.value;
          const parsed = (p.type === "float" || p.type === "int") ? parseFloat(raw) : raw;
          if (p.role === "variable") p.default = parsed;
          else p.value = parsed;
          draft.rawJson = JSON.stringify(draft.doc, null, 2);
          draft.registeredRevision = null;
        };
        tdVal.appendChild(valIn);
        tr.appendChild(tdVal);

        // Unit
        const tdUnit = el("td");
        const unitIn = el("input", {
          type: "text",
          value: p.unit || "",
          placeholder: "um/nm/V",
          style: "width: 70px; font-family: var(--font-mono);"
        });
        unitIn.oninput = () => {
          p.unit = unitIn.value.trim() || undefined;
          draft.rawJson = JSON.stringify(draft.doc, null, 2);
          draft.registeredRevision = null;
        };
        tdUnit.appendChild(unitIn);
        tr.appendChild(tdUnit);

        // Delete Action
        const tdAct = el("td", "num");
        const delBtn = el("button", "plain", {
          style: "padding: 2px 7px; font-size: 11px; color: var(--tone-bad-fg);",
          title: t("btnDeleteParam")
        }, "🗑️ " + t("btnDeleteParam"));
        delBtn.type = "button";
        delBtn.onclick = () => {
          draft.doc.parameters.splice(idx, 1);
          draft.rawJson = JSON.stringify(draft.doc, null, 2);
          draft.registeredRevision = null;
          renderDraftWorkspace();
        };
        tdAct.appendChild(delBtn);
        tr.appendChild(tdAct);

        pTbody.appendChild(tr);
      });
      pTable.appendChild(pTbody);
      pTableWrap.appendChild(pTable);
      formEditor.appendChild(pTableWrap);

      // Extracts Sub-Section
      const extractsHead = el("div", "section-filter-bar", { style: "margin: 20px 0 8px;" });
      const eCount = (draft.doc.extracts || []).length;
      extractsHead.appendChild(el("h3", "section-title",
        txt(`${t("sectionExtracts")} (${eCount})`)
      ));

      const addExtBtn = el("button", "plain primary", txt(t("btnAddExtract")));
      addExtBtn.type = "button";
      addExtBtn.onclick = () => {
        if (!draft.doc.extracts) draft.doc.extracts = [];
        const eIdx = draft.doc.extracts.length + 1;
        draft.doc.extracts.push({
          name: `extract_${eIdx}`,
          expression: `max(output_${eIdx})`
        });
        draft.doc.extract_names = draft.doc.extracts.map(e => e.name);
        draft.rawJson = JSON.stringify(draft.doc, null, 2);
        draft.registeredRevision = null;
        renderDraftWorkspace();
      };
      extractsHead.appendChild(addExtBtn);
      formEditor.appendChild(extractsHead);

      const eTableWrap = el("div", "table-dense-wrap");
      const eTable = el("table", "data-table table-dense");
      const eThead = el("thead");
      const eTrH = el("tr");
      eTrH.appendChild(el("th", "", txt(t("thExtractName"))));
      eTrH.appendChild(el("th", "", txt(t("thExtractExpr"))));
      eTrH.appendChild(el("th", "num", txt(t("thActions"))));
      eThead.appendChild(eTrH);
      eTable.appendChild(eThead);

      const eTbody = el("tbody");
      (draft.doc.extracts || []).forEach((ext, idx) => {
        const tr = el("tr");

        const tdName = el("td");
        const extNameIn = el("input", {
          type: "text",
          value: ext.name || "",
          style: "width: 160px; font-family: var(--font-mono); font-weight: 600;"
        });
        extNameIn.oninput = () => {
          ext.name = extNameIn.value.trim();
          draft.doc.extract_names = draft.doc.extracts.map(e => e.name);
          draft.rawJson = JSON.stringify(draft.doc, null, 2);
          draft.registeredRevision = null;
        };
        tdName.appendChild(extNameIn);
        tr.appendChild(tdName);

        const tdExpr = el("td");
        const extExprIn = el("input", {
          type: "text",
          value: ext.expression || "",
          placeholder: 'max(v."anode")',
          style: "width: 95%; font-family: var(--font-mono);"
        });
        extExprIn.oninput = () => {
          ext.expression = extExprIn.value.trim();
          draft.rawJson = JSON.stringify(draft.doc, null, 2);
          draft.registeredRevision = null;
        };
        tdExpr.appendChild(extExprIn);
        tr.appendChild(tdExpr);

        const tdAct = el("td", "num");
        const delExtBtn = el("button", "plain", {
          style: "padding: 2px 7px; font-size: 11px; color: var(--tone-bad-fg);",
          title: t("btnDeleteExtract")
        }, "🗑️ " + t("btnDeleteExtract"));
        delExtBtn.type = "button";
        delExtBtn.onclick = () => {
          draft.doc.extracts.splice(idx, 1);
          draft.doc.extract_names = draft.doc.extracts.map(e => e.name);
          draft.rawJson = JSON.stringify(draft.doc, null, 2);
          draft.registeredRevision = null;
          renderDraftWorkspace();
        };
        tdAct.appendChild(delExtBtn);
        tr.appendChild(tdAct);

        eTbody.appendChild(tr);
      });
      eTable.appendChild(eTbody);
      eTableWrap.appendChild(eTable);
      formEditor.appendChild(eTableWrap);

      draftSection.appendChild(formEditor);
    } else {
      // ================= JSON Mode Editor =================
      const jsonEditorWrap = el("div", "schema-json-editor-wrap");
      const ta = el("textarea", {
        id: "schema-json-textarea",
        className: "deck-textarea mono",
        style: "min-height: 240px;",
        value: draft.rawJson,
        "aria-label": "Schema JSON Source"
      });

      ta.oninput = () => {
        draft.rawJson = ta.value;
        try {
          const parsed = JSON.parse(ta.value);
          if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
            throw new Error("Schema root must be a JSON object {...}");
          }
          if (!parsed.parameters || !Array.isArray(parsed.parameters)) {
            throw new Error("Schema must declare 'parameters' array");
          }
          draft.doc = parsed;
          draft.jsonError = null;
          draft.registeredRevision = null;
        } catch (e) {
          draft.jsonError = e.message;
        }
        updateRegisterBtnState();
      };

      jsonEditorWrap.appendChild(ta);
      if (draft.jsonError) {
        const alertBox = el("div", "syntax-block-alert", txt(t("jsonParseErrorPrefix", { err: draft.jsonError })));
        jsonEditorWrap.appendChild(alertBox);
      }
      draftSection.appendChild(jsonEditorWrap);
    }

    // Action Row: Register + Advance Buttons
    const actRow = el("div", { style: "display: flex; gap: 8px; align-items: center; margin-top: 16px; flex-wrap: wrap;" });
    const regBtn = el("button", "plain primary", txt(t("btnRegisterSchema")));
    regBtn.type = "button";
    regBtn.id = "btn-reg-schema-draft";

    const nextBtn = el("button", "plain", txt(t("toStepProblem")));
    nextBtn.type = "button";
    nextBtn.onclick = () => {
      navigate("#/compose?step=3");
    };

    actRow.appendChild(regBtn);
    actRow.appendChild(nextBtn);
    draftSection.appendChild(actRow);

    const submitMsg = el("div", "submit-msg", { style: "margin-top: 8px;" });
    draftSection.appendChild(submitMsg);

    function updateRegisterBtnState() {
      if (draft.jsonError) {
        regBtn.disabled = true;
        regBtn.title = t("fixJsonSyntaxFirst");
      } else {
        if (!state.writes) regBtn.title = t("needAllowWrites");
        else regBtn.title = "";
      }
    }
    updateRegisterBtnState();

    regBtn.onclick = async () => {
      if (draft.jsonError) return;
      if (!state.writes) {
        submitMsg.className = "submit-msg err";
        submitMsg.textContent = t("readonlyNote");
        return;
      }

      regBtn.disabled = true;
      submitMsg.className = "submit-msg";
      submitMsg.textContent = t("connecting");

      const submitDoc = JSON.parse(JSON.stringify(draft.doc));
      delete submitDoc.extract_names;
      const r = await postJSON("/api/schemas", submitDoc);
      regBtn.disabled = false;

      if (!r || !r.ok || !r.data) {
        submitMsg.className = "submit-msg err";
        submitMsg.textContent = (r && r.data && r.data.error) || t("netError");
        return;
      }

      const rev = r.data.revision || "sha256:registered";
      draft.registeredRevision = rev;
      draft.registeredAt = new Date().toISOString();
      state.packagesSchemaRev = rev;
      state.candidateDesigner.schemaRev = rev;
      state.candidateDesigner.schemaDoc = JSON.parse(JSON.stringify(draft.doc));

      submitMsg.className = "submit-msg ok";
      submitMsg.textContent = t("schemaRegisteredOk", { rev });

      renderDraftWorkspace();
      refreshCatalogTable();
    };
  }

  renderDraftWorkspace();

  // ================= Section 2: Schemas Catalog Table =================
  const catalogSection = el("section", "rule-section schemas-catalog-section");
  const catalogHead = el("div", "section-filter-bar");
  catalogHead.appendChild(el("h3", "section-title", txt(t("catalogSchemasTitle"))));
  catalogSection.appendChild(catalogHead);
  container.appendChild(catalogSection);

  const catalogTableWrap = el("div", "catalog-table-container");
  catalogSection.appendChild(catalogTableWrap);

  async function refreshCatalogTable() {
    catalogTableWrap.textContent = "";

    const r = await fetchJSON("/api/schemas");
    let schemas = (r && r.ok && r.data && (r.data.items || r.data.schemas || (Array.isArray(r.data) ? r.data : []))) || [];

    if (schemas.length === 0 && Object.keys(mockSchemas).length > 0) {
      schemas = Object.keys(mockSchemas).map(rev => ({
        revision: rev,
        kind: mockSchemas[rev].kind || "parameter-schema",
        parameter_count: (mockSchemas[rev].parameters || []).length,
        parameters_count: (mockSchemas[rev].parameters || []).length,
        extract_names: mockSchemas[rev].extract_names || (mockSchemas[rev].extracts || []).map(e => e.name || e),
        extracts_count: (mockSchemas[rev].extracts || mockSchemas[rev].extract_names || []).length,
        source_package: mockSchemas[rev].source_package || null
      }));
    }
    state.schemasList = schemas;

    if (schemas.length === 0) {
      catalogTableWrap.appendChild(emptyPanel(t("noSchemasTitle"), t("noSchemasDesc")));
      return;
    }

    const tableWrap = el("div", "table-dense-wrap");
    const table = el("table", "data-table table-dense");
    const thead = el("thead");
    const trH = el("tr");
    trH.appendChild(el("th", "", txt(t("thSchemaRevision"))));
    trH.appendChild(el("th", "", txt(t("thSchemaKind"))));
    trH.appendChild(el("th", "num", txt(t("thParamCount"))));
    trH.appendChild(el("th", "num", txt(t("thExtractCount"))));
    trH.appendChild(el("th", "", txt(t("thSourcePackage"))));
    trH.appendChild(el("th", "num", txt(t("thActions"))));
    thead.appendChild(trH);
    table.appendChild(thead);

    const tbody = el("tbody");
    schemas.forEach(s => {
      const rev = s.revision || s.schema_revision || (typeof s === "string" ? s : "—");
      const kind = s.kind || "parameter-schema";
      const pCount = s.parameter_count !== undefined ? s.parameter_count : (s.parameters_count !== undefined ? s.parameters_count : ((s.parameters || []).length || "—"));
      const eCount = s.extract_names ? s.extract_names.length : (s.extracts_count !== undefined ? s.extracts_count : ((s.extracts || []).length || "—"));
      const pkg = s.source_package ? (s.source_package.artifact_id || s.source_package.package_name || "—") : "—";

      const tr = el("tr");
      const tdRev = el("td", "col-problem-id");
      const aRev = el("a", "mono", {
        href: `#/schema/${encodeURIComponent(rev)}`,
        title: rev
      }, monoHash(rev, { len: 16 }));
      tdRev.appendChild(aRev);
      tr.appendChild(tdRev);

      tr.appendChild(el("td", "", chip("info", kind)));
      tr.appendChild(el("td", "num mono", txt(pCount)));
      tr.appendChild(el("td", "num mono", txt(eCount)));
      tr.appendChild(el("td", "mono dim mono-art-id", { title: pkg }, txt(pkg)));

      const tdAct = el("td", "num");
      const actGroup = el("div", { style: "display: inline-flex; gap: 5px; align-items: center;" });

      const seedDraftBtn = el("button", "plain", {
        style: "padding: 2px 6px; font-size: 11px;",
        title: t("tipLoadSchemaToDraft"),
        onclick: async () => {
          const res = await fetchJSON(`/api/schemas/${encodeURIComponent(rev)}`);
          const doc = (res && res.ok && res.data) ? (res.data.schema || res.data) : (mockSchemas[rev] || null);
          if (doc) {
            state.schemaDraft.doc = JSON.parse(JSON.stringify(doc));
            state.schemaDraft.rawJson = JSON.stringify(doc, null, 2);
            state.schemaDraft.jsonError = null;
            state.schemaDraft.registeredRevision = rev;
            renderDraftWorkspace();
            window.scrollTo({ top: 0, behavior: "smooth" });
          }
        }
      }, t("btnForkDraftFromSchema"));

      const probBtn = el("button", "plain", {
        style: "padding: 2px 6px; font-size: 11px;",
        onclick: () => {
          state.candidateDesigner.schemaRev = rev;
          state.packagesSchemaRev = rev;
          navigate("#/compose?step=3");
        }
      }, "📐 " + t("btnRegisterProblem"));

      const candBtn = el("button", "plain primary", {
        style: "padding: 2px 6px; font-size: 11px;",
        onclick: () => {
          state.candidateDesigner.schemaRev = rev;
          navigate("#/compose?step=5");
        }
      }, "⚡ " + t("btnDesignCandidate"));

      actGroup.appendChild(seedDraftBtn);
      actGroup.appendChild(probBtn);
      actGroup.appendChild(candBtn);
      tdAct.appendChild(actGroup);
      tr.appendChild(tdAct);

      tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    tableWrap.appendChild(table);
    catalogTableWrap.appendChild(tableWrap);
  }

  await refreshCatalogTable();
  return container;
}

/**
 * Schema Detail View (Clean flat layout without generic card wrappers)
 */
async function renderSchemaDetail(rev, container) {
  let doc = mockSchemas[rev] || null;
  if (!doc) {
    const r = await fetchJSON(`/api/schemas/${encodeURIComponent(rev)}`);
    if (r && r.ok && r.data) {
      doc = r.data.schema || r.data;
    }
  }

  const head = pageHead(t("schemaTitle", { rev: rev.slice(0, 16) + "…" }), t("schemaDesc"), [
    el("button", "plain", { onclick: () => navigate("#/compose?step=2") }, "⬅️ " + t("btnBackToList")),
    el("button", "plain", {
      onclick: () => {
        if (doc) {
          state.schemaDraft.doc = JSON.parse(JSON.stringify(doc));
          state.schemaDraft.rawJson = JSON.stringify(doc, null, 2);
          state.schemaDraft.jsonError = null;
          state.schemaDraft.registeredRevision = rev;
        }
        navigate("#/compose?step=2");
      }
    }, t("btnForkDraftFromSchema")),
    el("button", "plain", {
      onclick: () => {
        state.candidateDesigner.schemaRev = rev;
        state.packagesSchemaRev = rev;
        navigate("#/compose?step=3");
      }
    }, t("btnCreateProblemWithSchema")),
    el("button", "plain primary", {
      onclick: () => {
        state.candidateDesigner.schemaRev = rev;
        navigate("#/compose?step=5");
      }
    }, t("btnDesignCandidateWithSchema"))
  ]);
  container.appendChild(head);

  if (!doc) {
    container.appendChild(errorBlock(t("statusError", { status: 404 }) + t("netError"), t("generalErrorHint")));
    return container;
  }

  state.schema = doc;
  state.schemaRev = rev;

  // Metadata Strip (Flat KV Rail)
  const metaStrip = el("div", "detail-meta-strip");
  const metaGrid = el("div", "detail-kv-grid");

  const rRev = el("div", "detail-kv-item");
  rRev.appendChild(el("span", "kv-key", txt(t("metaRevision"))));
  rRev.appendChild(el("span", "kv-val mono", monoHash(rev, { len: 24 })));
  metaGrid.appendChild(rRev);

  const rKind = el("div", "detail-kv-item");
  rKind.appendChild(el("span", "kv-key", txt(t("metaKind"))));
  rKind.appendChild(el("span", "kv-val", chip("info", doc.kind || "parameter-schema")));
  metaGrid.appendChild(rKind);

  if (doc.problem_hint) {
    const rHint = el("div", "detail-kv-item");
    rHint.appendChild(el("span", "kv-key", txt(t("metaProblemHint"))));
    rHint.appendChild(el("span", "kv-val mono", txt(doc.problem_hint)));
    metaGrid.appendChild(rHint);
  }

  if (doc.source_package) {
    const rPkg = el("div", "detail-kv-item");
    rPkg.appendChild(el("span", "kv-key", txt(t("metaSourcePackage"))));
    rPkg.appendChild(el("span", "kv-val mono", txt(doc.source_package.artifact_id || doc.source_package.package_name || "—")));
    metaGrid.appendChild(rPkg);
  }

  metaStrip.appendChild(metaGrid);
  container.appendChild(metaStrip);

  // Parameters Section
  const params = doc.parameters || [];
  const paramSection = el("section", "rule-section");
  const pHead = el("div", "section-filter-bar");
  pHead.appendChild(el("h3", "section-title", txt(`${t("sectionParameters")} (${params.length})`)));
  paramSection.appendChild(pHead);

  const pTableWrap = el("div", "table-dense-wrap");
  const pTable = el("table", "data-table table-dense");
  const pThead = el("thead");
  const pTrH = el("tr");
  pTrH.appendChild(el("th", "", txt(t("thParamName"))));
  pTrH.appendChild(el("th", "", txt(t("thParamRoleCol"))));
  pTrH.appendChild(el("th", "", txt(t("thParamTypeCol"))));
  pTrH.appendChild(el("th", "", txt(t("thParamBoundsCol"))));
  pTrH.appendChild(el("th", "", txt(t("thParamUnitCol"))));
  pTrH.appendChild(el("th", "num", txt(t("thParamValueCol"))));
  pThead.appendChild(pTrH);
  pTable.appendChild(pThead);

  const pTbody = el("tbody");
  params.forEach(p => {
    const tr = el("tr");
    tr.appendChild(el("td", "mono", { style: "font-weight: 600;" }, txt(p.name)));
    tr.appendChild(el("td", "", chip(p.role === "variable" ? "ok" : "dim", p.role === "variable" ? t("roleVariable") : t("roleFixed"))));
    tr.appendChild(el("td", "", chip("info", p.type || "float")));
    tr.appendChild(el("td", "mono sub", txt(p.bounds ? `[${p.bounds.min}, ${p.bounds.max}]` : "—")));
    tr.appendChild(el("td", "mono", txt(p.unit || "—")));
    tr.appendChild(el("td", "num mono", txt(p.default !== undefined ? fmtNumericValue(p.default) : (p.value !== undefined ? fmtNumericValue(p.value) : "—"))));
    pTbody.appendChild(tr);
  });
  pTable.appendChild(pTbody);
  pTableWrap.appendChild(pTable);
  paramSection.appendChild(pTableWrap);
  container.appendChild(paramSection);

  // Extracts Section
  const extracts = doc.extracts || (doc.extract_names || []).map(n => ({ name: n }));
  if (extracts && extracts.length > 0) {
    const extSection = el("section", "rule-section");
    const eHead = el("div", "section-filter-bar");
    eHead.appendChild(el("h3", "section-title", txt(`${t("sectionExtracts")} (${extracts.length})`)));
    extSection.appendChild(eHead);

    const extWrap = el("div", "table-dense-wrap");
    const extTable = el("table", "data-table table-dense");
    const eThead = el("thead");
    const eTrH = el("tr");
    eTrH.appendChild(el("th", "", txt(t("thExtractName"))));
    eTrH.appendChild(el("th", "", txt(t("thExtractExpr"))));
    eThead.appendChild(eTrH);
    extTable.appendChild(eThead);

    const eTbody = el("tbody");
    extracts.forEach(e => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", { style: "font-weight: 600;" }, txt(e.name || e)));
      tr.appendChild(el("td", "mono sub payload-trunc", { title: e.expression || "—" }, txt(e.expression || "—")));
      eTbody.appendChild(tr);
    });
    extTable.appendChild(eTbody);
    extWrap.appendChild(extTable);
    extSection.appendChild(extWrap);
    container.appendChild(extSection);
  }

  return container;
}
