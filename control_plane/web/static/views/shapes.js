/**
 * Shapes View · Swiss Technical Typography
 * Displays task shape profiles and physical resource execution statistics.
 */

import { t, fmtBytes, fmtClockTime } from "../i18n.js";
import { state } from "../state.js";
import { el, txt, pageHead, emptyPanel, metricRail, chip, tip, monoHash } from "../ui.js";
import { fetchJSON } from "../api.js";

export async function renderShapesView() {
  const container = el("div", "shapes-view");

  const r = await fetchJSON("/api/shapes");
  const data = (r && r.ok && r.data) ? r.data : (state.shapes || {});
  state.shapes = data;

  const shapes = data.shapes || [];

  // Page Header
  const head = pageHead(t("shapesTitle") || "任务画像", t("shapesDesc") || "任务类别的实测资源画像 · 样本不足 5 的行标记为低置信");
  container.appendChild(head);

  // Field Guide (Collapsible Swiss Details Well)
  const guide = el("details", "guide-details");
  guide.appendChild(el("summary", "", el("span", "guide-icon", "ℹ "), txt(t("shapesGuideSummary") || "指标列定义与缩写说明")));
  guide.appendChild(el("div", "details-content", txt(t("shapesGuide") || "字段说明：rev=画像版本；CPU=核心分配；样本=执行样本数；wall/cpu/busy=物理墙上/CPU累计/繁忙耗时均值±标准差(秒)；RSS=物理内存驻留集；预算=硬性超时限制")));
  container.appendChild(guide);

  // Section 1: Asymmetrical Rule-Delimited Metric Rail
  let totalSamples = 0;
  let lowConfCount = 0;
  shapes.forEach(s => {
    const samples = s.sample_count || 0;
    totalSamples += samples;
    if (samples < 5) lowConfCount++;
  });

  const shapesSub = shapes.length === 1
    ? (t("metricTaskClassSingle") || "1 种任务类别")
    : shapes.length > 0
      ? (t("metricTaskClassesPlural", { n: shapes.length }) || `${shapes.length} 种任务类别`)
      : (t("metricZeroRegistered") || "0 项已登记");

  const metricItems = [
    {
      primary: true,
      label: t("statTaskClasses") || "任务类别",
      val: shapes.length,
      tipKey: "tipTaskClassKey",
      sub: shapesSub
    },
    {
      label: t("statTotalSamples") || "总执行样本",
      val: totalSamples,
      tipKey: "tipSamples",
      sub: `${totalSamples} ${t("metricCompletedSamples") || "项已完成样本"}`
    },
    {
      label: t("statLowConfidence") || "低置信画像",
      val: lowConfCount,
      dotTone: lowConfCount > 0 ? "warn" : null,
      tone: lowConfCount > 0 ? "warn" : "neutral",
      tipKey: "tipShapesTable",
      sub: lowConfCount > 0 ? `${lowConfCount} ${t("metricLowConfidenceAudit") || "项样本需扩充"}` : (t("metricAllHighConfidence") || "全部置信度充足")
    }
  ];

  const rail = metricRail(metricItems);
  container.appendChild(rail);

  // Section 2: Rule-Delimited Shapes Catalog Section
  const shapesSection = el("section", "rule-section shapes-catalog-section");
  const sHead = el("div", "section-filter-bar");
  const sTitleWrap = el("div", "section-title-wrap");
  sTitleWrap.appendChild(el("h2", "section-title", txt(t("shapesCatalogTitle") || "任务类别资源画像目录")));
  sHead.appendChild(sTitleWrap);

  const countSummary = el("span", "count-summary meta mono", txt(t("shapesCountSummary", { count: shapes.length }) || `共 ${shapes.length} 项任务类别`));
  sHead.appendChild(countSummary);
  shapesSection.appendChild(sHead);

  if (shapes.length === 0) {
    shapesSection.appendChild(emptyPanel(t("noShapesTitle") || "暂无画像样本", t("noShapesDesc") || "尚无足够的执行样本形成形状统计。"));
  } else {
    const tableWrap = el("div", "table-dense-wrap");
    const table = el("table", "data-table table-dense");
    const thead = el("thead");
    const trH = el("tr");
    trH.appendChild(el("th", "", txt(t("thTaskClassKey") || "任务类别"), tip(null, "tipTaskClassKey")));
    trH.appendChild(el("th", "", txt(t("thTarget") || "目标节点"), tip(null, "tipTarget")));
    trH.appendChild(el("th", "num", txt(t("thRev") || "版本"), tip(null, "tipRev")));
    trH.appendChild(el("th", "num", txt(t("thCpu") || "CPU"), tip(null, "tipCpu")));
    trH.appendChild(el("th", "num", txt(t("thSamples") || "样本数"), tip(null, "tipSamples")));
    trH.appendChild(el("th", "", txt(t("thSuccessRate") || "成功率"), tip(null, "tipSuccessRate")));
    trH.appendChild(el("th", "num", txt(t("thWallMean") || "物理耗时 (wall)"), tip(null, "tipWallMean")));
    trH.appendChild(el("th", "num", txt(t("thCpuMean") || "CPU 耗时 (cpu)"), tip(null, "tipCpuMean")));
    trH.appendChild(el("th", "num", txt(t("thBusyMean") || "繁忙耗时 (busy)"), tip(null, "tipBusyMean")));
    trH.appendChild(el("th", "num", txt(t("thRssMean") || "内存驻留 (RSS)"), tip(null, "tipRssMean")));
    trH.appendChild(el("th", "", txt(t("thBudget") || "预算限制 (max/cmd)"), tip(null, "tipBudget")));
    thead.appendChild(trH);
    table.appendChild(thead);

    const tbody = el("tbody");
    shapes.forEach(s => {
      const tr = el("tr");
      const isLowConf = (s.sample_count || 0) < 5;

      const tdKey = el("td", "col-task-key");
      const keyStr = s.task_class_key || s.task_shape || "—";
      tdKey.appendChild(el("span", "mono mono-task-key", { style: "font-weight: 600;", title: keyStr }, txt(keyStr)));
      if (isLowConf) {
        const lcChip = el("span", "chip tone-warn", {
          style: "margin-left: 6px; font-size: 9.5px;",
          title: t("lowConfidenceTip") || "样本数 < 5，统计低置信"
        }, txt(t("lowConfidenceTag") || "低置信"));
        tdKey.appendChild(lcChip);
      }
      tr.appendChild(tdKey);

      tr.appendChild(el("td", "mono dim", s.target_id ? monoHash(s.target_id, { len: 14 }) : txt("—")));
      tr.appendChild(el("td", "num mono", txt(s.profile_revision !== undefined ? String(s.profile_revision) : "—")));
      tr.appendChild(el("td", "num mono", txt(s.processors !== undefined ? String(s.processors) : "—")));
      tr.appendChild(el("td", "num mono", txt(s.sample_count !== undefined ? String(s.sample_count) : "0")));

      // Success Rate
      const total = s.sample_count || 0;
      const succ = s.success_count || 0;
      const rate = total > 0 ? `${Math.round((succ / total) * 100)}%` : "—";
      const tdRate = el("td");
      if (total > 0) {
        tdRate.appendChild(chip(succ === total ? "ok" : (succ > 0 ? "warn" : "bad"), `${succ}/${total} (${rate})`));
      } else {
        tdRate.appendChild(el("span", "dim", "—"));
      }
      tr.appendChild(tdRate);

      // Wall mean & stddev
      const wallStr = s.successful_wall_mean_seconds !== null && s.successful_wall_mean_seconds !== undefined
        ? `${s.successful_wall_mean_seconds.toFixed(1)}${s.successful_wall_stddev_seconds ? `±${s.successful_wall_stddev_seconds.toFixed(1)}` : ""}`
        : "—";
      tr.appendChild(el("td", "num mono", txt(wallStr)));

      // CPU mean & stddev
      const cpuStr = s.cpu_mean_seconds !== null && s.cpu_mean_seconds !== undefined
        ? `${s.cpu_mean_seconds.toFixed(1)}${s.cpu_stddev_seconds ? `±${s.cpu_stddev_seconds.toFixed(1)}` : ""}`
        : "—";
      tr.appendChild(el("td", "num mono", txt(cpuStr)));

      // Busy mean & stddev
      const busyStr = s.busy_mean_seconds !== null && s.busy_mean_seconds !== undefined
        ? `${s.busy_mean_seconds.toFixed(1)}${s.busy_stddev_seconds ? `±${s.busy_stddev_seconds.toFixed(1)}` : ""}`
        : "—";
      tr.appendChild(el("td", "num mono", txt(busyStr)));

      // RSS mean
      tr.appendChild(el("td", "num mono", txt(fmtBytes(s.rss_mean_bytes))));

      // Budget
      const b = s.budget || {};
      const bMax = b.max_wall_seconds ? `${b.max_wall_seconds}s` : "∞";
      const bCmd = b.command_timeout_seconds ? `${b.command_timeout_seconds}s` : "—";
      tr.appendChild(el("td", "mono sub", txt(`${bMax} / ${bCmd}`)));

      tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    tableWrap.appendChild(table);
    shapesSection.appendChild(tableWrap);
  }
  container.appendChild(shapesSection);

  // Section 3: Baseline Footer Termination
  const baselineFooter = el("footer", "overview-baseline-footer", { role: "contentinfo" });
  const footerRow = el("div", "baseline-meta-row");
  footerRow.appendChild(el("span", "baseline-meta meta mono", txt(t("shapesBaselineMeta") || "科研评测控制台 · 任务形状与资源画像统计")));
  footerRow.appendChild(el("span", "baseline-telemetry meta mono", txt(`${t("overviewLiveSync") || "实时同步: 正常"} · ${fmtClockTime(new Date())}`)));
  baselineFooter.appendChild(footerRow);
  container.appendChild(baselineFooter);

  return container;
}
