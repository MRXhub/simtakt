/**
 * Shared DOM Primitives & Reusable Technical UI Components
 * Swiss Technical Typography & Flat Graphic Design System
 */

import { t } from "./i18n.js";

/**
 * Universal DOM Builder Element Factory
 */
export function el(tag, className = "", ...children) {
  const node = document.createElement(tag);
  if (typeof className === "string") {
    if (className) node.className = className;
  } else if (className && typeof className === "object") {
    children.unshift(className);
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (typeof child === "string" || typeof child === "number") {
      node.appendChild(document.createTextNode(String(child)));
    } else if (child instanceof Node) {
      node.appendChild(child);
    } else if (Array.isArray(child)) {
      child.forEach(c => {
        if (c !== null && c !== undefined && c !== false) {
          node.appendChild(typeof c === "object" && c instanceof Node ? c : document.createTextNode(String(c)));
        }
      });
    } else if (typeof child === "object") {
      // Attributes or style mapping
      Object.keys(child).forEach(k => {
        if (k === "style" && typeof child[k] === "string") {
          node.style.cssText = child[k];
        } else if (k.startsWith("on") && typeof child[k] === "function") {
          node[k] = child[k];
        } else if (k === "value") {
          node.value = child[k] !== undefined && child[k] !== null ? child[k] : "";
        } else if (k === "checked" || k === "disabled" || k === "selected" || k === "readOnly") {
          node[k] = !!child[k];
        } else if (k === "className" || k === "class") {
          node.className = child[k];
        } else {
          node.setAttribute(k, child[k]);
        }
      });
    }
  }
  return node;
}

/**
 * Text node helper
 */
export function txt(str) {
  return document.createTextNode(str === undefined || str === null ? "" : String(str));
}

/**
 * State Indicator: Precision dot + label text (demoting candy pills)
 */
export function statusIndicator(tone = "neutral", labelText = "", { sub = "", icon = "" } = {}) {
  const wrap = el("span", `state-indicator tone-${tone}`);
  const dot = el("span", `state-dot tone-${tone}`, { "aria-hidden": "true" });
  wrap.appendChild(dot);
  if (icon) {
    wrap.appendChild(el("span", "state-icon", txt(icon)));
  }
  wrap.appendChild(el("span", "state-label", txt(labelText)));
  if (sub) {
    wrap.appendChild(el("span", "state-sub dim sub", txt(sub)));
  }
  return wrap;
}

/**
 * Flat Semantic Chip Component (high contrast WCAG AA, minimal chrome)
 */
export function chip(tone, text, icon) {
  const toneClass = tone || "neutral";
  const c = el("span", `chip tone-${toneClass}`);
  const dot = el("span", `chip-dot tone-${toneClass}`, { "aria-hidden": "true" });
  c.appendChild(dot);
  if (icon) c.appendChild(el("span", "chip-icon", typeof icon === "string" ? txt(icon) : icon));
  c.appendChild(el("span", "chip-text", txt(text)));
  return c;
}

/**
 * Inline Question Tooltip Popover
 */
export function tip(text, key) {
  const tipText = key ? t(key) : text;
  const wrap = el("span", "tip-wrap");
  const btn = el("button", "tip-btn", {
    type: "button",
    "aria-label": (t("tipAriaLabel") || "Help tip") + ": " + tipText
  }, "?");
  const pop = el("span", "tip-pop", { role: "tooltip" }, txt(tipText));
  wrap.appendChild(btn);
  wrap.appendChild(pop);
  return wrap;
}

/**
 * Monospace Truncated Hash / ID Formatter (guarantees zero horizontal overflow)
 */
export function monoHash(hashStr, { len = 16, copyable = true, prefix = "", suffix = "", bold = false } = {}) {
  if (!hashStr) return el("span", "dim", "—");
  const fullStr = String(hashStr);
  const isSha = fullStr.startsWith("sha256:");
  const rawHash = isSha ? fullStr.replace(/^sha256:/, "") : fullStr;
  const displayHash = rawHash.length > len ? rawHash.slice(0, len) + "…" : rawHash;
  const displayFull = isSha ? `sha256:${displayHash}` : displayHash;

  const span = el("span", `mono mono-hash ${bold ? "mono-bold" : ""}`, {
    title: fullStr,
    "data-full-hash": fullStr
  });
  if (prefix && !displayFull.startsWith(prefix.trim())) span.appendChild(el("span", "mono-prefix dim", txt(prefix)));
  span.appendChild(txt(displayFull));
  if (suffix) span.appendChild(el("span", "mono-suffix dim", txt(suffix)));

  if (copyable) {
    span.classList.add("copyable");
    span.onclick = (e) => {
      e.stopPropagation();
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(fullStr).then(() => {
          const originalTitle = span.title;
          span.title = t("hashCopied") || "Copied!";
          span.classList.add("copied");
          setTimeout(() => {
            span.title = originalTitle;
            span.classList.remove("copied");
          }, 1500);
        }).catch(() => {});
      }
    };
  }

  return span;
}

/**
 * Metric Rail Strip: Rule-delimited asymmetrical metric bar
 * Replaces equal heavy KPI card rows with large tabular numerals, hairline dividers, and micro labels
 */
export function metricRail(items = []) {
  const rail = el("div", "metric-rail", { role: "region", "aria-label": "System Metrics" });

  items.forEach((item, idx) => {
    if (!item) return;
    const isPrimary = item.primary === true || idx === 0;
    const toneClass = (item.tone && item.val > 0) ? `tone-${item.tone}` : "";
    const primaryClass = isPrimary ? "is-primary" : "is-subordinate";
    const railItem = el("div", `metric-rail-item ${primaryClass} ${toneClass} ${item.accent ? "accent-item" : ""}`);
    
    // Label Row
    const labelRow = el("div", "metric-rail-label");
    if (item.dotTone && item.val > 0) {
      labelRow.appendChild(el("span", `state-dot tone-${item.dotTone}`, { "aria-hidden": "true" }));
    }
    labelRow.appendChild(txt(item.label || ""));
    if (item.tipKey) {
      labelRow.appendChild(tip(null, item.tipKey));
    }
    railItem.appendChild(labelRow);

    // Tabular Numeral Value
    const valStr = item.val !== undefined && item.val !== null ? String(item.val) : "—";
    const valNode = el("div", `metric-rail-val mono ${isPrimary ? "primary-numeral" : "subordinate-numeral"}`, txt(valStr));
    if (item.unit) {
      valNode.appendChild(el("span", "metric-rail-unit", txt(item.unit)));
    }
    railItem.appendChild(valNode);

    // Optional Secondary Meta
    if (item.sub) {
      railItem.appendChild(el("div", "metric-rail-sub", txt(item.sub)));
    }

    rail.appendChild(railItem);
  });

  return rail;
}


/**
 * Swiss Page Head: Display heading, body description, and action strip
 */
export function pageHead(titleContent, descText, actionNodes = []) {
  const head = el("div", "page-head");
  const main = el("div", "page-head-main");

  const h1 = el("h1");
  if (typeof titleContent === "string") h1.appendChild(txt(titleContent));
  else if (titleContent instanceof Node) h1.appendChild(titleContent);
  main.appendChild(h1);

  if (descText) {
    main.appendChild(el("div", "desc", txt(descText)));
  }
  head.appendChild(main);

  if (actionNodes && actionNodes.length > 0) {
    const act = el("div", "page-actions");
    actionNodes.forEach(a => { if (a) act.appendChild(a); });
    head.appendChild(act);
  }
  return head;
}

/**
 * Empty State Panel (hairline dashed border on paper plane)
 */
export function emptyPanel(title, desc) {
  const p = el("div", "empty-panel");
  p.appendChild(el("h3", "empty-panel-title", txt(title)));
  if (desc) p.appendChild(el("p", "empty-panel-desc", txt(desc)));
  return p;
}

/**
 * Error Alert Block
 */
export function errorBlock(msg, hint) {
  const b = el("div", "error-block");
  b.appendChild(el("h3", "", txt(msg)));
  if (hint) b.appendChild(el("p", "", txt(hint)));
  return b;
}

/**
 * Status Pill (Chip with state semantics)
 */
export function statusPill(status) {
  const s = String(status || "unknown").toLowerCase();
  let tone = "neutral";
  if (s === "qualified" || s === "completed" || s === "active" || s === "registered") tone = "ok";
  else if (s === "running" || s === "qualifying") tone = "info";
  else if (s === "queued" || s === "requested" || s === "staging" || s === "verifying") tone = "warn";
  else if (s === "recovering" || s === "reconciling" || s === "ambiguous") tone = "warn";
  else if (s === "failed" || s === "unresolved" || s === "blocked") tone = "bad";
  else if (s === "cancelled" || s === "deduplicating") tone = "dim";

  return chip(tone, s);
}

/**
 * Entity Picker Component
 * Supports select dropdown from API list + free-text manual fallback when list is empty or custom entry needed.
 */
export function entityPicker({
  label,
  id,
  value,
  items = [],
  itemToOption = (item) => ({ value: item.id || item, label: item.name || item.id || item, sub: item.sub }),
  onSelect,
  placeholder = t("pickerSelectPlaceholder") || "Select...",
  helpText = "",
  required = false
}) {
  let isManual = items.length === 0;
  let currentValue = value || "";

  const container = el("div", "entity-picker");
  const head = el("div", "entity-picker-head");
  head.appendChild(el("label", "entity-picker-label", txt(label), required ? el("span", { style: "color: var(--tone-bad-fg)" }, " *") : null));

  const toggleBtn = el("button", "entity-picker-mode-btn", { type: "button" });
  head.appendChild(toggleBtn);
  container.appendChild(head);

  const bodyWrap = el("div", "entity-picker-body");
  container.appendChild(bodyWrap);

  if (helpText) {
    container.appendChild(el("div", "form-help", txt(helpText)));
  }

  function renderMode() {
    bodyWrap.textContent = "";
    if (!isManual && items.length > 0) {
      toggleBtn.textContent = "✏️ " + (t("pickerManualFallback") || "Manual");
      const selRow = el("div", "entity-picker-select-row");
      const select = el("select", { id: id ? `${id}-select` : undefined });

      const optDefault = el("option", { value: "" }, placeholder);
      select.appendChild(optDefault);

      let found = false;
      items.forEach(item => {
        const optData = itemToOption(item);
        const opt = el("option", { value: optData.value }, optData.label + (optData.sub ? ` (${optData.sub})` : ""));
        if (optData.value === currentValue) {
          opt.selected = true;
          found = true;
        }
        select.appendChild(opt);
      });

      if (!found && currentValue) {
        const customOpt = el("option", { value: currentValue, selected: true }, currentValue + " (custom)");
        select.appendChild(customOpt);
      }

      select.onchange = () => {
        currentValue = select.value;
        if (onSelect) onSelect(currentValue);
      };
      selRow.appendChild(select);
      bodyWrap.appendChild(selRow);
    } else {
      toggleBtn.textContent = items.length > 0 ? "📋 " + (t("pickerSwitchDropdown") || "List") : "";
      toggleBtn.style.display = items.length > 0 ? "inline-block" : "none";

      const inRow = el("div", "entity-picker-input-row");
      const input = el("input", {
        type: "text",
        id: id || undefined,
        value: currentValue,
        placeholder: placeholder || "Enter ID..."
      });
      input.oninput = () => {
        currentValue = input.value.trim();
        if (onSelect) onSelect(currentValue);
      };
      inRow.appendChild(input);
      bodyWrap.appendChild(inRow);
    }
  }

  toggleBtn.onclick = () => {
    isManual = !isManual;
    renderMode();
  };

  renderMode();
  return {
    node: container,
    getValue: () => currentValue,
    setValue: (v) => {
      currentValue = v || "";
      renderMode();
    }
  };
}

/**
 * Preflight Status Box for Candidate Designer
 */
export function preflightBox(validation, jsonError) {
  const box = el("div");

  if (jsonError) {
    box.className = "preflight-box syntax-error";
    const head = el("div", "preflight-head");
    head.appendChild(el("span", { style: "font-weight: 800; font-size: 14px;" }, "✕ "));
    head.appendChild(txt(t("syntaxError") || "Syntax Error"));
    box.appendChild(head);

    const alert = el("div", "syntax-block-alert");
    alert.appendChild(el("span", "", "⛔ "));
    alert.appendChild(txt(t("syntaxErrorAlert") || "JSON Parse Failure"));
    box.appendChild(alert);

    const iss = el("div", "issue-item");
    iss.appendChild(el("span", "issue-tag", "SYNTAX"));
    iss.appendChild(txt(jsonError));
    box.appendChild(iss);
    return box;
  }

  if (!validation || validation.valid) {
    box.className = "preflight-box valid";
    const head = el("div", "preflight-head");
    head.appendChild(txt(t("preflightValid") || "Valid Candidate"));
    box.appendChild(head);
    return box;
  }

  box.className = "preflight-box invalid";
  const issues = validation.issues || [];
  const head = el("div", "preflight-head");
  head.appendChild(txt(t("preflightInvalid", { count: issues.length }) || `Invalid (${issues.length} issues)`));
  box.appendChild(head);

  const list = el("div", "issue-list");
  issues.forEach(iss => {
    const item = el("div", "issue-item");
    let tag = "ISSUE";
    if (iss.code === "missing_parameter") tag = "MISSING";
    else if (iss.code === "out_of_bounds") tag = "BOUNDS";
    else if (iss.code === "type_mismatch") tag = "TYPE";
    else if (iss.code === "unexpected_parameter") tag = "EXTRA";
    item.appendChild(el("span", "issue-tag", tag));
    item.appendChild(txt(iss.message || JSON.stringify(iss)));
    list.appendChild(item);
  });
  box.appendChild(list);
  return box;
}

/**
 * Interactive Vertical Flow Rail Component for Compose Flow
 * Sequence, progress and current position expressed graphically
 */
export function chainStepper(activeStep = 1, onStepClick) {
  const railWrap = el("div", "compose-flow-rail-container");

  const steps = [
    { num: 1, railNum: 1, key: "deck", title: t("stage1Title") || "1. 软件包与 Deck", sub: t("stage1Sub") || "Deck 解析与工件落地" },
    { num: 2, railNum: 2, key: "schema", title: t("stage2Title") || "2. 参数模式草稿", sub: t("stage2Sub") || "模式编辑与版本登记" },
    { num: 3, railNum: 3, key: "problem", title: t("stage3Title") || "3. 评测问题", sub: t("stage3Sub") || "契约定义与能力绑定" },
    { num: 4, railNum: 4, key: "study", title: t("stage4Title") || "4. 实验研究", sub: t("stage4Sub") || "研究容器与实验队列" },
    { num: 5, railNum: 5, key: "candidate", title: t("stage5Title") || "5. 候选解设计", sub: t("stage5Sub") || "候选解设计与冒烟试算" }
  ];

  const list = el("div", "flow-rail-steps");

  steps.forEach((s) => {
    const isCur = s.num === activeStep;
    const isDone = s.num < activeStep;
    const stateClass = isCur ? "active" : (isDone ? "completed" : "pending");

    const item = el("div", `flow-rail-step ${stateClass}`, {
      role: "button",
      tabIndex: 0,
      "aria-current": isCur ? "step" : undefined,
      "aria-label": `${s.title}: ${isDone ? (t("stepStatusDone") || "Done") : (isCur ? (t("stepStatusActive") || "Active") : (t("stepStatusPending") || "Pending"))}`
    });

    // Step Node Icon / Number
    const nodeIcon = el("div", "flow-step-node");
    if (isDone) {
      nodeIcon.appendChild(el("span", "node-check", "✓"));
    } else {
      nodeIcon.appendChild(el("span", "node-num", String(s.railNum)));
    }
    item.appendChild(nodeIcon);

    // Step Body
    const body = el("div", "flow-step-body");
    const headRow = el("div", "flow-step-head");
    headRow.appendChild(el("span", "flow-step-title", s.title));

    const statusPillNode = el("span", `flow-step-pill ${stateClass}`);
    statusPillNode.textContent = isDone ? (t("stepStatusDone") || "Done") : (isCur ? (t("stepStatusActive") || "Active") : (t("stepStatusPending") || "Pending"));
    headRow.appendChild(statusPillNode);
    body.appendChild(headRow);

    body.appendChild(el("div", "flow-step-sub", s.sub));

    item.appendChild(body);

    const clickHandler = () => {
      if (onStepClick) onStepClick(s.num);
    };

    item.onclick = clickHandler;
    item.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        clickHandler();
      }
    };

    list.appendChild(item);
  });

  railWrap.appendChild(list);
  return railWrap;
}

/**
 * Localized Write Mode and Health badge renderers
 * Single source of truth for app shell and in-page views
 */
export function getWriteModeText(writes) {
  return writes ? (t("modeWritable") || "WRITABLE") : (t("modeReadOnly") || "READ-ONLY");
}

export function getWriteModeTip(writes) {
  return writes ? (t("modeWritableTip") || "Writes enabled") : (t("modeReadOnlyTip") || "Read-only mode");
}

export function getHealthText(healthOk) {
  return healthOk ? (t("healthy") || "Healthy") : (t("unhealthy") || "Unhealthy");
}

export function renderModeBadge(writes) {
  const className = "mode-tag " + (writes ? "writable" : "readonly");
  return el("span", className, {
    title: getWriteModeTip(writes)
  }, txt(getWriteModeText(writes)));
}

export function renderHealthIndicator(healthOk) {
  const wrap = el("div", "side-rail-health-indicator");
  const dotClass = "state-dot " + (healthOk ? "tone-ok" : "tone-bad");
  wrap.appendChild(el("span", dotClass, { "aria-hidden": "true" }));
  wrap.appendChild(el("span", "side-rail-health-text", txt(getHealthText(healthOk))));
  return wrap;
}
