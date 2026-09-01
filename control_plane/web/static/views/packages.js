/**
 * Package Landing & Deck Parser Workspace · Swiss Technical Layout
 * Stage 1 in the 5-stage composition pipeline.
 *
 * Vocabulary:
 * - Rule-delimited sections with typographic hierarchy (Display > Section > Meta)
 * - Inset wells for drag-and-drop targets and terminal log previews
 * - Single-level action card for form submission (zero card-in-card nesting)
 * - Dense technical tables with truncated hashes and tabular numerals
 */

import { t, fmtClockTime, fmtDate } from "../i18n.js";
import { state } from "../state.js";
import { el, txt, pageHead, emptyPanel, chip, monoHash } from "../ui.js";
import { postJSON, fetchJSON, SAMPLE_DECKS } from "../api.js";
import { navigate } from "../router.js";

let packageJobTimer = null;

export function renderPackagesView() {
  const container = el("div", "packages-view");

  if (state.packagesJobId && state.packagesJobStatus !== "registered" && state.packagesJobStatus !== "failed") {
    if (!packageJobTimer) {
      startPackageJobPolling(state.packagesJobId);
    }
  }

  // Swiss Field Guide (Stage Title provided cleanly by workbench shell & stage nav rail)
  // Collapsible Swiss Field Guide
  const guide = el("details", "guide-details");
  guide.appendChild(el("summary", "", el("span", "guide-icon", "ℹ "), txt(t("packagesGuideSummary") || "阶段 1 使用流程与操作说明")));
  guide.appendChild(el("div", "details-content", txt(t("packagesGuide") || "向导流程：1. 粘贴或拖入仿真 deck 文本并解析参数；2. 打标参数属性与取值范围并生成 Schema 草稿；3. 落地 Package 工件并验证；4. 注册 Problem 契约与 Study 研究；5. 在 Candidate 设计器中完成试算。")));
  container.appendChild(guide);

  // ================= Section 1: Deck Input & Upload =================
  const deckSection = el("section", "rule-section deck-workbench-section");

  // Inset Well for File Dropzone
  const dropZone = el("div", "drop-zone inset-well", {
    role: "button",
    tabIndex: 0,
    "aria-label": t("deckDropZone") || "Drop deck file here"
  });
  dropZone.appendChild(el("div", "drop-zone-icon", "📂"));
  const dropContent = el("div", "drop-zone-content");
  dropContent.appendChild(el("div", "drop-zone-text", txt(t("deckDropZone") || "将 .in / .deck 文本文件拖拽至此，或直接在下方输入 / 粘贴")));
  dropContent.appendChild(el("div", "drop-zone-hint dim sub", txt(t("deckDropHint") || "支持 硅基 TCAD、SPICE 电学网表、SPIS 等离子体空间仿真等格式")));
  dropZone.appendChild(dropContent);
  const fileInput = el("input", { type: "file", style: "display: none;" });
  dropZone.appendChild(fileInput);
  dropZone.onclick = () => fileInput.click();
  dropZone.onkeydown = (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fileInput.click();
    }
  };

  function handleFileDrop(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (e) => {
      state.packagesDeckText = e.target.result;
      const ta = document.getElementById("deck-textarea");
      if (ta) ta.value = state.packagesDeckText;
    };
    reader.readAsText(file);
  }

  fileInput.onchange = (e) => {
    if (e.target.files && e.target.files[0]) handleFileDrop(e.target.files[0]);
  };

  dropZone.ondragover = (e) => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
  };
  dropZone.ondragleave = () => {
    dropZone.classList.remove("drag-over");
  };
  dropZone.ondrop = (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    if (e.dataTransfer.files && e.dataTransfer.files[0]) handleFileDrop(e.dataTransfer.files[0]);
  };
  deckSection.appendChild(dropZone);

  // Preset Samples Bar
  const presetBar = el("div", "deck-samples");
  presetBar.appendChild(el("span", "sample-label meta", txt(t("presetSamples") || "快速示例：")));
  function makeSampleBtn(name, text) {
    const b = el("button", "plain sample-pill-btn", txt(name));
    b.type = "button";
    b.onclick = () => {
      state.packagesDeckText = text;
      const ta = document.getElementById("deck-textarea");
      if (ta) ta.value = text;
    };
    return b;
  }
  presetBar.appendChild(makeSampleBtn(t("sampleSolarDeck") || "示例 1: 10 结太阳能 TCAD", SAMPLE_DECKS.solar));
  presetBar.appendChild(makeSampleBtn(t("sampleCmosDeck") || "示例 2: CMOS 反相器 SPICE", SAMPLE_DECKS.cmos));
  presetBar.appendChild(makeSampleBtn(t("samplePlasmaDeck") || "示例 3: SPIS 等离子体", SAMPLE_DECKS.plasma));

  const clearBtn = el("button", "plain clear-deck-btn", txt(t("clearDeck") || "清空"));
  clearBtn.type = "button";
  clearBtn.onclick = () => {
    state.packagesDeckText = "";
    state.packagesParsed = null;
    const ta = document.getElementById("deck-textarea");
    if (ta) ta.value = "";
    renderInspectionSection();
  };
  presetBar.appendChild(clearBtn);
  deckSection.appendChild(presetBar);

  // Deck Textarea (Monospace Tabular Figure Font)
  const ta = el("textarea", {
    id: "deck-textarea",
    className: "deck-textarea mono",
    placeholder: t("deckPlaceholder") || "set param = 1.0 ...",
    value: state.packagesDeckText || SAMPLE_DECKS.solar,
    "aria-label": "Simulation Deck Source Code"
  });
  state.packagesDeckText = ta.value;
  ta.oninput = () => { state.packagesDeckText = ta.value; };
  deckSection.appendChild(ta);

  // Deck Action Row
  const actionRow = el("div", "deck-action-row");
  const parseBtn = el("button", "plain primary parse-action-btn", txt(t("btnParseDeck") || "解析 Deck"));
  parseBtn.type = "button";
  actionRow.appendChild(parseBtn);

  const parseMsg = el("div", "submit-msg deck-parse-msg");
  actionRow.appendChild(parseMsg);
  deckSection.appendChild(actionRow);

  container.appendChild(deckSection);

  // ================= Section 2: Dynamic Parsed Definitions Inspection =================
  const inspectionContainer = el("div", "deck-inspection-container");
  container.appendChild(inspectionContainer);

  parseBtn.onclick = async () => {
    const text = (document.getElementById("deck-textarea") || {}).value || "";
    if (!text.trim()) {
      parseMsg.className = "submit-msg err";
      parseMsg.textContent = t("parseNoParams") || "未在文本中发现合法的 set 或 extract 语句定义";
      return;
    }

    parseBtn.disabled = true;
    parseMsg.className = "submit-msg";
    parseMsg.textContent = t("connecting") || "Connecting...";

    const r = await postJSON("/api/packages/parse", { deck_text: text });
    parseBtn.disabled = false;

    if (!r || !r.ok || !r.data) {
      parseMsg.className = "submit-msg err";
      parseMsg.textContent = (r && r.data && r.data.error) || t("netError") || "Network error";
      return;
    }

    state.packagesParsed = r.data;
    const pCount = (r.data.parameters || []).length;
    const eCount = (r.data.extracts || []).length;
    parseMsg.className = "submit-msg ok";
    parseMsg.textContent = t("parseSuccess", {
      count: pCount + eCount,
      numeric: (r.data.parameters || []).filter(p => p.kind === "numeric").length,
      expression: (r.data.parameters || []).filter(p => p.kind === "expression").length,
      extracts: eCount
    });

    renderInspectionSection();
    renderLandingSection();
  };

  function buildSchemaDocFromParsed(parsed) {
    const numParams = (parsed.parameters || []).filter(p => p.kind === "numeric");
    const extracts = parsed.extracts || [];

    const parameters = numParams.map(p => {
      const val = p.value !== null ? p.value : 1.0;
      const minVal = val > 0 ? parseFloat((val * 0.2).toFixed(3)) : 0.0;
      const maxVal = val > 0 ? parseFloat((val * 5.0).toFixed(3)) : 10.0;
      return {
        name: p.name,
        type: "float",
        role: "variable",
        bounds: { min: minVal, max: maxVal },
        default: val,
        deck_line: p.line
      };
    });

    return {
      kind: "parameter-schema",
      problem_hint: state.packagesPackageName ? `problem:${state.packagesPackageName.replace(/^pkg-/, "")}` : "deck-derived-schema",
      parameters,
      extracts: extracts.map(e => ({ name: e.name, expression: e.expression, line: e.line })),
      extract_names: extracts.map(e => e.name)
    };
  }

  function renderInspectionSection() {
    inspectionContainer.textContent = "";
    if (!state.packagesParsed) return;

    const parsed = state.packagesParsed;
    const numParams = (parsed.parameters || []).filter(p => p.kind === "numeric");
    const exprParams = (parsed.parameters || []).filter(p => p.kind === "expression");
    const extracts = parsed.extracts || [];

    const inspectSection = el("section", "rule-section deck-inspection-section");
    inspectSection.appendChild(el("h3", "section-heading", txt(t("deckParsedSectionTitle") || "已解析参数与观测量")));

    // Stats & Action Strip (Rule-delimited, no card box)
    const statsBar = el("div", "parsed-summary-bar");
    statsBar.appendChild(el("div", "parsed-stats-text mono", txt(t("deckParsedStats", {
      num: numParams.length,
      expr: exprParams.length,
      ext: extracts.length
    }))));

    // PRIMARY ACTION BUTTON: Seed Persistent Schema Draft
    const seedDraftBtn = el("button", "plain primary seed-draft-action-btn", {
      onclick: () => {
        const schemaDoc = buildSchemaDocFromParsed(parsed);
        state.schemaDraft.doc = schemaDoc;
        state.schemaDraft.rawJson = JSON.stringify(schemaDoc, null, 2);
        state.schemaDraft.jsonError = null;
        state.schemaDraft.registeredRevision = null;
        state.schemaDraft.mode = "form";
        navigate("#/compose?step=2");
      }
    }, "🌱 " + (t("btnSeedDraftFromDeck") || "由此 Deck 生成 Schema 草稿 ➔"));
    statsBar.appendChild(seedDraftBtn);
    inspectSection.appendChild(statsBar);

    // Dense Parsed Definitions Table (Flat, rule-delimited)
    const tableWrap = el("div", "table-dense-wrap");
    const table = el("table", "data-table table-dense");
    const thead = el("thead");
    const trH = el("tr");
    trH.appendChild(el("th", "col-line", txt(t("thParamDeckLine") || "Line")));
    trH.appendChild(el("th", "col-name", txt(t("thParamName") || "Identifier")));
    trH.appendChild(el("th", "col-kind", txt(t("thParamKind") || "Kind")));
    trH.appendChild(el("th", "col-val num", txt(t("thParamValue") || "Value / Expression")));
    thead.appendChild(trH);
    table.appendChild(thead);

    const tbody = el("tbody");

    // Numeric params
    numParams.forEach(p => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono sub", txt(p.line)));
      tr.appendChild(el("td", "mono mono-name", txt(p.name)));
      tr.appendChild(el("td", "", el("span", "kind-indicator",
        el("span", "kind-dot numeric", { "aria-hidden": "true" }),
        txt(t("kindNumeric"))
      )));
      tr.appendChild(el("td", "mono num", txt(p.value !== null ? p.value : p.value_raw)));
      tbody.appendChild(tr);
    });

    // Expressions
    exprParams.forEach(p => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono sub", txt(p.line)));
      tr.appendChild(el("td", "mono", txt(p.name)));
      tr.appendChild(el("td", "", el("span", "kind-indicator",
        el("span", "kind-dot expression", { "aria-hidden": "true" }),
        txt(t("kindExpression"))
      )));
      tr.appendChild(el("td", "mono dim num", txt(p.value_raw)));
      tbody.appendChild(tr);
    });

    // Extracts
    extracts.forEach(e => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono sub", txt(e.line || "—")));
      tr.appendChild(el("td", "mono mono-name", txt(e.name)));
      tr.appendChild(el("td", "", el("span", "kind-indicator",
        el("span", "kind-dot extract", { "aria-hidden": "true" }),
        txt(t("kindExtract"))
      )));
      tr.appendChild(el("td", "mono dim num", txt(e.expression || "extract(...)")));
      tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    tableWrap.appendChild(table);
    inspectSection.appendChild(tableWrap);

    inspectionContainer.appendChild(inspectSection);
  }

  // ================= Section 3: Package Landing Async Job =================
  const landingContainer = el("div", "landing-container");
  container.appendChild(landingContainer);

  function renderLandingSection() {
    landingContainer.textContent = "";

    const landSection = el("section", "rule-section landing-section");
    landSection.appendChild(el("h3", "section-heading", txt(t("packageLandingSectionTitle") || "保存 Package 工件")));

    const formWrap = el("div", "package-landing-form");
    const formRow = el("div", "form-row two-col");

    const nameField = el("div", "form-field");
    nameField.appendChild(el("label", "", txt(t("fieldPackageName") || "Package 名称")));
    const nameIn = el("input", {
      type: "text",
      id: "pkg-name",
      placeholder: "pkg-solar-cell-tcad",
      value: state.packagesPackageName || "pkg-solar-cell-tcad"
    });
    nameIn.oninput = () => { state.packagesPackageName = nameIn.value.trim(); };
    nameField.appendChild(nameIn);
    nameField.appendChild(el("div", "form-help dim", txt(t("fieldPackageNameTip") || "需匹配 ^[a-z0-9][a-z0-9-]{2,63}$")));
    formRow.appendChild(nameField);

    const depField = el("div", "form-field");
    depField.appendChild(el("label", "", txt(t("fieldDependencies") || "依赖项 (可选)")));
    const depIn = el("input", { type: "text", placeholder: "core-tcad@v2.4" });
    depField.appendChild(depIn);
    formRow.appendChild(depField);
    formWrap.appendChild(formRow);

    const landActionRow = el("div", "landing-action-row");
    const landBtn = el("button", "plain primary", txt(t("btnSubmitPackage") || "保存 Package"));
    landBtn.type = "button";
    landBtn.id = "btn-submit-pkg";
    landBtn.disabled = !state.writes;
    if (!state.writes) landBtn.title = t("needAllowWrites") || "Requires --allow-writes";
    landActionRow.appendChild(landBtn);

    const landMsg = el("div", "submit-msg");
    landActionRow.appendChild(landMsg);
    formWrap.appendChild(landActionRow);

    // Job Terminal Logs Viewer Inset
    const logBox = el("pre", "log-well mono preview", {
      "aria-label": "Job execution logs"
    });
    formWrap.appendChild(logBox);

    // Cross actions when job succeeds
    const crossActionsWrap = el("div", "cross-actions-strip");
    formWrap.appendChild(crossActionsWrap);
    landSection.appendChild(formWrap);

    function updateLogsUI() {
      logBox.textContent = (state.packagesJobLogs || []).join("\n") || t("noActiveJobsNotice") || "暂无进行中的任务。保存 Package 即可开始执行落地流程。";
      if (state.packagesJobStatus === "registered") {
        crossActionsWrap.textContent = "";

        const seedDraftAct = el("button", "plain primary", txt("🌱 " + (t("btnSeedDraftFromDeck") || "由此 Deck 生成 Schema 草稿")));
        seedDraftAct.type = "button";
        seedDraftAct.onclick = () => {
          if (state.packagesParsed) {
            const doc = buildSchemaDocFromParsed(state.packagesParsed);
            state.schemaDraft.doc = doc;
            state.schemaDraft.rawJson = JSON.stringify(doc, null, 2);
            state.schemaDraft.registeredRevision = null;
          }
          navigate("#/compose?step=2");
        };

        const prefillProbBtn = el("button", "plain", txt(t("btnPrefillProblem") || "向导：一键预填 Problem 注册"));
        prefillProbBtn.type = "button";
        prefillProbBtn.onclick = () => {
          state.candidateDesigner.problemId = `problem:${state.packagesPackageName.replace(/^pkg-/, "")}`;
          navigate("#/compose?step=3");
        };

        const openCandBtn = el("button", "plain", txt(t("btnOpenCandidateDesigner") || "前往 Candidate 设计器"));
        openCandBtn.type = "button";
        openCandBtn.onclick = () => {
          navigate("#/compose?step=5");
        };

        crossActionsWrap.appendChild(seedDraftAct);
        crossActionsWrap.appendChild(prefillProbBtn);
        crossActionsWrap.appendChild(openCandBtn);
      }
    }
    updateLogsUI();

    landBtn.onclick = async () => {
      if (!state.writes) {
        landMsg.className = "submit-msg err";
        landMsg.textContent = t("readonlyNote") || "Read-only mode";
        return;
      }
      const pkgName = nameIn.value.trim();
      landMsg.className = "submit-msg";
      landMsg.textContent = t("connecting") || "Connecting...";

      const r = await postJSON("/api/packages", {
        package_name: pkgName,
        deck_text: state.packagesDeckText
      });
      landBtn.disabled = false;

      if (!r || !r.ok || !r.data) {
        landMsg.className = "submit-msg err";
        landMsg.textContent = (r && r.data && r.data.error) || t("netError") || "Network error";
        return;
      }

      const jobId = r.data.job_id;
      state.packagesJobId = jobId;
      state.packagesJobStatus = "queued";
      state.packagesJobLogs = [`[${fmtClockTime(new Date())}] [queued] Package landing job submitted. ID: ${jobId}`];

      landMsg.className = "submit-msg ok";
      landMsg.textContent = `Job submitted: ${jobId} (HTTP 202)`;
      updateLogsUI();

      startPackageJobPolling(jobId, () => {
        updateLogsUI();
        refreshPackagesCatalog();
      });
    };

    landingContainer.appendChild(landSection);
  }

  // ================= Section 4: Registered Packages Catalog =================
  const catalogSection = el("section", "rule-section packages-catalog-section");
  catalogSection.appendChild(el("h3", "section-heading", txt(t("packageCatalogSectionTitle") || "已登记 Package")));
  container.appendChild(catalogSection);

  const catalogTableWrap = el("div", "packages-catalog-table-wrap");
  catalogSection.appendChild(catalogTableWrap);

  function buildTable(packages) {
    try {
      catalogTableWrap.textContent = "";

      if (!packages || packages.length === 0) {
        catalogTableWrap.appendChild(emptyPanel(t("noPackagesTitle") || "暂无已落地的 Package 工件", t("noPackagesDesc") || "在上方输入仿真 deck 文本以提交 Package 落地任务。"));
        return;
      }
    const tableWrap = el("div", "table-dense-wrap");
    const table = el("table", "data-table table-dense");
    const thead = el("thead");
    const trH = el("tr");
    trH.appendChild(el("th", "col-pkg-name", txt(t("thPackageName") || "Package 名称")));
    trH.appendChild(el("th", "col-art-id", txt(t("thArtifactId") || "工件 ID")));
    trH.appendChild(el("th", "col-rev", txt(t("thRevision") || "Revision")));
    trH.appendChild(el("th", "col-status", txt(t("thStatus") || "Status")));
    trH.appendChild(el("th", "col-created", txt(t("thCreatedAt") || "创建时间")));
    trH.appendChild(el("th", "col-actions", txt(t("thActions") || "操作")));
    thead.appendChild(trH);
    table.appendChild(thead);

    const tbody = el("tbody");

    packages.forEach((pkg, idx) => {
      const pName = pkg.package_name || pkg.id || `pkg-item-${idx + 1}`;
      const artId = pkg.artifact_id || `pkg:${pName}`;
      const rev = pkg.revision || "sha256:registered";
      const status = pkg.status || "registered";
      const isExpanded = !!state.expandedPackageManifests[pName];

      const tr = el("tr");
      tr.appendChild(el("td", "mono mono-pkg-name", txt(pName)));
      tr.appendChild(el("td", "mono dim mono-art-id", txt(artId)));
      tr.appendChild(el("td", "mono col-rev-cell", monoHash(rev, { len: 16 })));
      tr.appendChild(el("td", "col-status-cell", chip(status === "registered" ? "ok" : "warn", status)));
      tr.appendChild(el("td", "sub dim mono-num", txt(pkg.created_at ? fmtDate(new Date(pkg.created_at)) : "—")));

      const tdAct = el("td", "col-actions-cell");
      const actGroup = el("div", "catalog-actions-group");

      // View Manifest & Files Button
      const manifestBtn = el("button", `plain catalog-action-btn ${isExpanded ? "active" : ""}`, {
        onclick: () => {
          state.expandedPackageManifests[pName] = !state.expandedPackageManifests[pName];
          buildTable(state.packagesList);
        }
      }, isExpanded ? (t("btnCollapseManifest") || "▲ 收起文件") : (t("btnViewManifest") || "📄 查看文件"));
      actGroup.appendChild(manifestBtn);

      // Seed Schema Draft Button
      const seedDraftBtn = el("button", "plain primary catalog-action-btn", {
        title: t("tipSeedSchemaFromPackage") || "Seed Schema draft from this package and proceed to editor",
        onclick: async () => {
          let deckText = pkg.deck_file_content || state.packagesDeckText || SAMPLE_DECKS.solar;
          const parseRes = await postJSON("/api/packages/parse", { deck_text: deckText });
          if (parseRes && parseRes.ok && parseRes.data) {
            const schemaDoc = buildSchemaDocFromParsed(parseRes.data);
            schemaDoc.problem_hint = `problem:${pName.replace(/^pkg-/, "")}`;
            schemaDoc.source_package = { artifact_id: artId, revision: rev };
            state.schemaDraft.doc = schemaDoc;
            state.schemaDraft.rawJson = JSON.stringify(schemaDoc, null, 2);
            state.schemaDraft.registeredRevision = null;
            state.schemaDraft.mode = "form";
          }
          state.packagesPackageName = pName;
          navigate("#/compose?step=2");
        }
      }, t("btnSeedSchemaFromPackage") || "🌱 生成 Schema");
      actGroup.appendChild(seedDraftBtn);

      // Prefill Problem
      const probBtn = el("button", "plain catalog-action-btn", {
        onclick: () => {
          state.candidateDesigner.problemId = `problem:${pName.replace(/^pkg-/, "")}`;
          navigate("#/compose?step=3");
        }
      }, "🎯 " + (t("submitCardProblem") || "注册 Problem"));
      actGroup.appendChild(probBtn);

      tdAct.appendChild(actGroup);
      tr.appendChild(tdAct);
      tbody.appendChild(tr);

      // Expanded Manifest Panel Row (Flat Inset Well, No nested card)
      if (isExpanded) {
        const trExp = el("tr", "manifest-expanded-row");
        const tdExp = el("td", { colspan: "6", className: "manifest-expanded-cell" });

        const manifestWell = el("div", "manifest-well inset-well");
        manifestWell.appendChild(el("h4", "manifest-title",
          txt(`📦 ${t("manifestModalTitle") || "Package Manifest"}: ${pName}`)
        ));

        // Metadata grid (Safe hash truncation with full tooltip to prevent horizontal overflow!)
        const mGrid = el("div", "manifest-meta-grid");
        mGrid.appendChild(el("div", "meta-cell", el("span", "meta-label meta", t("metaArtifactId") || "工件 ID: "), el("span", "mono", txt(artId))));
        mGrid.appendChild(el("div", "meta-cell", el("span", "meta-label meta", t("metaContentHash") || "内容哈希: "), monoHash(rev, { len: 24 })));
        mGrid.appendChild(el("div", "meta-cell", el("span", "meta-label meta", t("metaDeckPath") || "Deck 路径: "), el("span", "mono dim", txt(pkg.path || `data/inputs/packages/${pName}`))));
        manifestWell.appendChild(mGrid);

        const filesHead = el("div", "manifest-files-heading meta",
          txt(`📁 ${t("packageManifestFiles") || "Artifact Files"}:`)
        );
        manifestWell.appendChild(filesHead);

        const filesTableWrap = el("div", "table-dense-wrap manifest-files-table-wrap");
        const filesTable = el("table", "data-table table-dense");
        const fHead = el("thead");
        const fTrH = el("tr");
        fTrH.appendChild(el("th", "", txt(t("thFileName") || "File Name")));
        fTrH.appendChild(el("th", "num", txt(t("thFileSize") || "Size")));
        fTrH.appendChild(el("th", "", txt(t("thSha256") || "SHA256 Hash")));
        fHead.appendChild(fTrH);
        filesTable.appendChild(fHead);
        const fBody = el("tbody");
        const files = (pkg.files && pkg.files.length > 0) ? pkg.files : [
          { name: pkg.deck_file || "deck.in", bytes: 1420, sha256: rev.replace(/^sha256:/, "") },
          { name: "manifest.json", bytes: 348, sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" }
        ];
        files.forEach(f => {
          const fTr = el("tr");
          fTr.appendChild(el("td", "mono mono-name", txt(f.name)));
          fTr.appendChild(el("td", "num mono", txt(`${f.bytes || 1024} B`)));
          fTr.appendChild(el("td", "mono", monoHash(f.sha256, { len: 20 })));
          fBody.appendChild(fTr);
        });
        filesTable.appendChild(fBody);
        filesTableWrap.appendChild(filesTable);
        manifestWell.appendChild(filesTableWrap);

        tdExp.appendChild(manifestWell);
        trExp.appendChild(tdExp);
        tbody.appendChild(trExp);
      }
    });
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    catalogTableWrap.appendChild(tableWrap);
  } catch (err) {
    console.error("buildTable error:", err);
  }
}
  async function refreshPackagesCatalog() {
    if (state.packagesList && state.packagesList.length > 0) {
      buildTable(state.packagesList);
    }
    const r = await fetchJSON("/api/packages");
    let packages = (r && r.ok && r.data && (r.data.items || r.data.packages || (Array.isArray(r.data) ? r.data : []))) || [];
    if (packages.length > 0 || !state.packagesList || state.packagesList.length === 0) {
      state.packagesList = packages;
    }
    buildTable(state.packagesList);
  }

  refreshPackagesCatalog();

  if (state.packagesParsed) {
    renderInspectionSection();
    renderLandingSection();
  }

  return container;
}

function startPackageJobPolling(jobId, callback) {
  if (packageJobTimer) clearInterval(packageJobTimer);

  packageJobTimer = setInterval(async () => {
    const r = await fetchJSON(`/api/packages/jobs/${encodeURIComponent(jobId)}`);
    if (r && r.ok && r.data) {
      state.packagesJobStatus = r.data.status;
      if (r.data.log_tail) state.packagesJobLogs = r.data.log_tail;
      if (r.data.package) {
        state.packagesSchemaRev = r.data.package.revision || state.packagesSchemaRev;
      }
      if (callback) callback();
      if (r.data.status === "registered" || r.data.status === "failed") {
        clearInterval(packageJobTimer);
        packageJobTimer = null;
      }
    }
  }, 1000);
}
