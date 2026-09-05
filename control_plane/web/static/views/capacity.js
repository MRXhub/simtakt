/**
 * Capacity View · Swiss Technical Typography
 * Displays global queue counters, license pools, and execution targets.
 *
 * Doctrine 2: Asymmetrical rule-delimited metric rail with primary session capacity and subordinate queue telemetry.
 */

import { t, fmtDate, fmtClockTime } from "../i18n.js";
import { state } from "../state.js";
import { el, txt, pageHead, emptyPanel, metricRail, chip, tip, monoHash } from "../ui.js";
import { fetchJSON } from "../api.js";

export async function renderCapacityView() {
  const container = el("div", "capacity-view");

  const r = await fetchJSON("/api/capacity");
  const data = (r && r.ok && r.data) ? r.data : (state.capacity || {});
  state.capacity = data;

  const g = data.global || {};
  const pools = data.license_pools || [];
  const targets = data.targets || [];

  const snapSuffix = data.snapshot && data.snapshot !== "unavailable" ? t("snapshotSuffix", { snap: data.snapshot }) : "";
  const head = pageHead(t("capacityTitle") || "容量", (t("capacityDesc") || "全局队列计数、License 池与执行目标") + snapSuffix);
  container.appendChild(head);

  // Section 1: Asymmetrical Rule-Delimited Metric Rail (Doctrine 2)
  let totalActiveSessions = 0;
  let totalMaxSessions = 0;
  pools.forEach(p => {
    totalActiveSessions += (p.active_count !== undefined ? p.active_count : (p.license_sessions_in_use || 0));
    totalMaxSessions += (p.license_sessions || 0);
  });

  const queuedCount = g.queued || 0;
  const recoveringCount = g.recovering || 0;
  const reconcilingCount = g.reconciling || 0;
  const staleCount = g.stale_reconciling;

  const poolsSub = pools.length === 1
    ? (t("metricActivePoolSingle") || "1 个活跃 License 池")
    : pools.length > 0
      ? (t("metricActivePoolPlural", { n: pools.length }) || `${pools.length} 个活跃 License 池`)
      : (t("metricZeroRegistered") || "0 个已配置");

  const queuedSub = queuedCount > 0
    ? (queuedCount === 1 ? (t("metricAwaitingSlotSingle") || "等待调度槽位") : (t("metricAwaitingSlotsPlural", { n: queuedCount }) || `${queuedCount} 项等待槽位`))
    : (t("metricQueueClear") || "队列无积压");

  const recoveringSub = recoveringCount > 0
    ? (recoveringCount === 1 ? (t("metricSolverRetrySingle") || "1 项重试中") : (t("metricSolverRetriesPlural", { n: recoveringCount }) || `${recoveringCount} 项重试中`))
    : (t("metricZeroRecovering") || "0 项重试中");

  const reconcilingSub = reconcilingCount > 0
    ? (reconcilingCount === 1 ? (t("metricSyncingNodeSingle") || "1 项同步中") : (t("metricSyncingNodesPlural", { n: reconcilingCount }) || `${reconcilingCount} 项同步中`))
    : (t("metricZeroReconciling") || "0 项同步中");

  const metricItems = [
    {
      primary: true,
      label: t("statActiveSessions") || "活跃会话",
      val: `${totalActiveSessions} / ${totalMaxSessions || "—"}`,
      sub: poolsSub
    },
    {
      label: t("statQueued") || "排队中",
      val: queuedCount,
      dotTone: queuedCount > 0 ? "warn" : null,
      tone: queuedCount > 0 ? "warn" : "neutral",
      tipKey: "tipWaiting",
      sub: queuedSub
    },
    {
      label: t("statRecovering") || "重试中",
      val: recoveringCount,
      dotTone: recoveringCount > 0 ? "bad" : null,
      tone: recoveringCount > 0 ? "bad" : "neutral",
      tipKey: "tipRecovering",
      sub: recoveringSub
    },
    {
      label: t("statReconciling") || "节点同步中",
      val: reconcilingCount,
      dotTone: reconcilingCount > 0 ? "warn" : null,
      tone: reconcilingCount > 0 ? "warn" : "neutral",
      tipKey: "tipReconciling",
      sub: reconcilingSub
    }
  ];

  if (staleCount !== undefined) {
    metricItems.push({
      label: t("statStaleReconciling") || "同步超时",
      val: staleCount || 0,
      dotTone: (staleCount || 0) > 0 ? "bad" : null,
      tone: (staleCount || 0) > 0 ? "bad" : "neutral",
      tipKey: "tipStaleReconciling",
      sub: (staleCount || 0) > 0 ? (t("metricAuditRequired") || "需要审计") : (t("metricNoStale") || "无超时")
    });
  }

  const rail = metricRail(metricItems);
  container.appendChild(rail);

  // Section 2: License Pools Rule-Delimited Section
  const poolSection = el("section", "rule-section license-pools-section");
  const poolHead = el("div", "section-filter-bar");
  const poolTitleWrap = el("div", "section-title-wrap");
  poolTitleWrap.appendChild(el("h2", "section-title", txt(t("sectionLicensePools") || "License 资源池")));
  poolHead.appendChild(poolTitleWrap);

  const poolSummary = el("span", "count-summary meta mono", txt(t("poolsCountSummary", { count: pools.length }) || `共 ${pools.length} 个资源池`));
  poolHead.appendChild(poolSummary);
  poolSection.appendChild(poolHead);

  if (pools.length === 0) {
    poolSection.appendChild(emptyPanel(t("noLicensePoolsTitle") || "暂无 License 池", t("noLicensePoolsDesc") || "执行拓扑中尚未配置任何 License 池。"));
  } else {
    const pWrap = el("div", "table-dense-wrap");
    const pTable = el("table", "data-table table-dense");
    const pThead = el("thead");
    const pTrH = el("tr");
    pTrH.appendChild(el("th", "", txt(t("thLicensePoolId") || "资源池标识")));
    pTrH.appendChild(el("th", "", txt(t("thSessionAlloc") || "会话占用")));
    pTrH.appendChild(el("th", "num", txt(t("thInUse") || "使用中")));
    pTrH.appendChild(el("th", "num", txt(t("thReserve") || "预留")));
    pTrH.appendChild(el("th", "", txt(t("thActiveStatus") || "状态")));
    pThead.appendChild(pTrH);
    pTable.appendChild(pThead);

    const pTbody = el("tbody");
    pools.forEach(pool => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", { style: "font-weight: 600;" }, monoHash(pool.license_pool_id, { len: 20 })));

      const total = pool.license_sessions || 0;
      const active = pool.active_count !== undefined ? pool.active_count : 0;
      tr.appendChild(el("td", "mono", txt(`${active} / ${total}`)));
      tr.appendChild(el("td", "mono num", txt(pool.license_sessions_in_use !== null && pool.license_sessions_in_use !== undefined ? String(pool.license_sessions_in_use) : "—")));
      tr.appendChild(el("td", "mono num", txt(pool.license_reserve !== undefined ? String(pool.license_reserve) : "—")));
      const enabled = typeof pool.active === "boolean" ? pool.active : null;
      tr.appendChild(el("td", "", chip(enabled === true ? "ok" : "dim", enabled === null ? "—" : enabled ? t("chipActive") : t("chipDisabled"))));
      pTbody.appendChild(tr);
    });
    pTable.appendChild(pTbody);
    pWrap.appendChild(pTable);
    poolSection.appendChild(pWrap);
  }
  container.appendChild(poolSection);

  // Section 3: Targets Rule-Delimited Section
  const targetSection = el("section", "rule-section targets-section");
  const targetHead = el("div", "section-filter-bar");
  const targetTitleWrap = el("div", "section-title-wrap");
  targetTitleWrap.appendChild(el("h2", "section-title", txt(t("sectionTargets") || "执行目标节点")));
  targetHead.appendChild(targetTitleWrap);

  const targetSummary = el("span", "count-summary meta mono", txt(t("targetsCountSummary", { count: targets.length }) || `共 ${targets.length} 个目标节点`));
  targetHead.appendChild(targetSummary);
  targetSection.appendChild(targetHead);

  if (targets.length === 0) {
    targetSection.appendChild(emptyPanel(t("noTargetsTitle") || "暂无执行目标", t("noTargetsDesc") || "执行拓扑中尚未配置任何目标节点。"));
  } else {
    const tWrap = el("div", "table-dense-wrap");
    const tTable = el("table", "data-table table-dense");
    const tThead = el("thead");
    const tTrH = el("tr");
    tTrH.appendChild(el("th", "", txt(t("thTargetId") || "目标标识"), tip(null, "tipTarget")));
    tTrH.appendChild(el("th", "", txt(t("thHostId") || "主机标识")));
    tTrH.appendChild(el("th", "", txt(t("thRole") || "角色")));
    tTrH.appendChild(el("th", "", txt(t("thSessionsActiveMax") || "会话并发")));
    tTrH.appendChild(el("th", "", txt(t("thActiveStatus") || "状态")));
    tThead.appendChild(tTrH);
    tTable.appendChild(tThead);

    const tTbody = el("tbody");
    targets.forEach(target => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", { style: "font-weight: 600;" }, monoHash(target.target_id, { len: 20 })));
      tr.appendChild(el("td", "mono dim", target.host_id ? monoHash(target.host_id, { len: 16 }) : txt("—")));
      tr.appendChild(el("td", "", chip("info", t(target.role === "formal" ? "nodeRoleFormal" : target.role === "trial" ? "nodeRoleTrial" : "nodeRoleUnknown"))));

      const act = target.active_count !== undefined ? target.active_count : 0;
      const max = target.max_active_sessions !== null && target.max_active_sessions !== undefined ? target.max_active_sessions : "—";
      tr.appendChild(el("td", "mono", txt(`${act} / ${max}`)));
      const enabled = typeof target.active === "boolean" ? target.active : null;
      tr.appendChild(el("td", "", chip(enabled === true ? "ok" : "dim", enabled === null ? "—" : enabled ? t("chipActive") : t("chipDisabled"))));
      tTbody.appendChild(tr);
    });
    tTable.appendChild(tTbody);
    tWrap.appendChild(tTable);
    targetSection.appendChild(tWrap);
  }
  container.appendChild(targetSection);

  // Section 4: Baseline Footer Termination
  const baselineFooter = el("footer", "overview-baseline-footer", { role: "contentinfo" });
  const footerRow = el("div", "baseline-meta-row");
  footerRow.appendChild(el("span", "baseline-meta meta mono", txt(t("capacityBaselineMeta") || "科研评测控制台 · 集群容量与拓扑调度")));
  footerRow.appendChild(el("span", "baseline-telemetry meta mono", txt(`${t("overviewLiveSync") || "实时同步: 正常"} · ${fmtClockTime(new Date())}`)));
  baselineFooter.appendChild(footerRow);
  container.appendChild(baselineFooter);

  return container;
}
