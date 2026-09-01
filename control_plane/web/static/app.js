/**
 * Main Application Orchestrator (Zero-build Static ES Module)
 * Swiss Technical Typography Architecture & Responsive Shell Wiring
 */

import { t, getLang, setLang, fmtClockTime } from "./i18n.js";
import { state } from "./state.js";
import { fetchJSON, IS_MOCK } from "./api.js";
import { parseRoute, updateDocTitle, markNav, navigate } from "./router.js";
import { el, txt, emptyPanel, errorBlock, chip, getWriteModeText, getWriteModeTip, getHealthText } from "./ui.js";

// View Imports
import { renderOverviewView } from "./views/overview.js";
import { renderPackagesView } from "./views/packages.js";
import { renderSchemasView } from "./views/schemas.js";
import { renderProblemsView } from "./views/problems.js";
import { renderStudiesView } from "./views/studies.js";
import { renderCompose } from "./views/compose.js";
import { renderAlgorithmsView } from "./views/algorithms.js";
import { renderCapacityView } from "./views/capacity.js";
import { renderShapesView } from "./views/shapes.js";

let refreshSeq = 0;
let refreshTimer = null;

async function prefetchEntityLists() {
  try {
    const [schemasRes, problemsRes, studiesRes, evalsRes, pkgsRes] = await Promise.all([
      fetchJSON("/api/schemas"),
      fetchJSON("/api/problems"),
      fetchJSON("/api/studies"),
      fetchJSON("/api/evaluations"),
      fetchJSON("/api/packages")
    ]);

    if (schemasRes && schemasRes.ok && schemasRes.data) {
      const items = schemasRes.data.items || schemasRes.data.schemas || (Array.isArray(schemasRes.data) ? schemasRes.data : []);
      state.schemasList = items.map(s => {
        const pCount = s.parameter_count !== undefined ? s.parameter_count : (s.parameters_count !== undefined ? s.parameters_count : ((s.parameters || []).length || 0));
        const extractsList = s.extract_names || (s.extracts || []).map(e => e.name || e) || [];
        return {
          revision: s.revision || s.schema_revision || (typeof s === "string" ? s : ""),
          kind: s.kind || "parameter-schema",
          parameter_count: pCount,
          parameters_count: pCount,
          registered_at: s.registered_at || null,
          extracts_count: extractsList.length,
          extract_names: extractsList,
          source_package: s.source_package || null
        };
      });
    }

    if (problemsRes && problemsRes.ok && problemsRes.data) {
      state.problemsList = problemsRes.data.items || problemsRes.data.problems || (Array.isArray(problemsRes.data) ? problemsRes.data : []);
    }

    if (studiesRes && studiesRes.ok && studiesRes.data) {
      state.studiesList = studiesRes.data.items || studiesRes.data.studies || (Array.isArray(studiesRes.data) ? studiesRes.data : []);
    }

    if (evalsRes && evalsRes.ok && evalsRes.data) {
      state.evaluationsList = evalsRes.data.items || evalsRes.data.evaluations || (Array.isArray(evalsRes.data) ? evalsRes.data : []);
    }

    if (pkgsRes && pkgsRes.ok && pkgsRes.data) {
      state.packagesList = pkgsRes.data.items || pkgsRes.data.packages || (Array.isArray(pkgsRes.data) ? pkgsRes.data : []);
    }
  } catch (_) {}
}

async function refreshHealth() {
  const r = await fetchJSON("/api/health");
  const ok = !!(r && r.ok && r.data && r.data.status === "ok");
  state.healthOk = ok;
  state.health = (r && r.data) || null;
  state.writes = !!(ok && r.data && r.data.writes_enabled === true);
  updateTopBarHealth();
}

export function updateTopBarHealth() {
  const healthDot = document.getElementById("healthdot");
  const healthTxt = document.getElementById("healthtxt");
  const modeTag = document.getElementById("modeTag");
  const mobileHealthDot = document.getElementById("mobileHealthDot");

  if (healthDot) healthDot.className = "hdot " + (state.healthOk ? "ok" : "bad");
  if (mobileHealthDot) mobileHealthDot.className = "hdot " + (state.healthOk ? "ok" : "bad");
  if (healthTxt) healthTxt.textContent = getHealthText(state.healthOk);

  if (modeTag) {
    modeTag.textContent = getWriteModeText(state.writes);
    modeTag.title = getWriteModeTip(state.writes);
    modeTag.className = "mode-tag " + (state.writes ? "writable" : "readonly");
  }
}

function updateBanner() {
  const banner = document.getElementById("banner");
  const bannerText = document.getElementById("bannertext");
  const content = document.getElementById("content");

  if (!banner || !content) return;

  if (!state.unreachable) {
    banner.classList.remove("show");
    content.classList.remove("stale");
    return;
  }

  banner.classList.add("show");
  content.classList.add("stale");

  if (state.lastRenderAt === null) {
    bannerText.textContent = t("bannerUnreachableNoData");
  } else {
    const n = Math.max(0, Math.round((Date.now() - state.lastRenderAt) / 1000));
    bannerText.textContent = t("bannerUnreachableWithData", { n });
  }
}

export async function refreshRoute() {
  const route = parseRoute();
  state.route = route;

  if (typeof window !== "undefined") {
    const rawQuery = window.location.search || (window.location.hash.includes("?") ? window.location.hash.split("?")[1] : "");
    const urlParams = new URLSearchParams(rawQuery);
    const langParam = urlParams.get("lang");
    if (langParam === "en" || langParam === "zh") {
      if (getLang() !== langParam) {
        setLang(langParam);
        updateStaticTexts();
      }
    }
  }

  markNav(route);
  updateDocTitle(route);

  const content = document.getElementById("content");
  if (!content) return;

  const seq = ++refreshSeq;
  const isStale = () => seq !== refreshSeq;

  let viewNode = null;
  try {
    if (route.name === "overview") {
      viewNode = await renderOverviewView();
    } else if (route.name === "compose") {
      viewNode = await renderCompose({ initialStep: route.step || 1, detailId: route.id, detailType: route.detailType });
    } else if (route.name === "algorithms" || route.name === "algorithm") {
      viewNode = await renderAlgorithmsView({ id: route.id });
    } else if (route.name === "capacity") {
      viewNode = await renderCapacityView();
    } else if (route.name === "shapes") {
      viewNode = await renderShapesView();
    } else {
      viewNode = el("div");
      viewNode.appendChild(emptyPanel(t("unknownRouteTitle") || "页面未找到", t("unknownRouteDesc") || "请求的路由不存在"));
    }

    if (isStale()) return;

    state.unreachable = false;
    state.lastRenderAt = Date.now();
    updateBanner();

    content.textContent = "";
    if (viewNode) content.appendChild(viewNode);

    const refreshInfo = document.getElementById("refreshinfo");
    if (refreshInfo) {
      refreshInfo.textContent = `${t("lastRefreshed") || "最近刷新"} ${fmtClockTime(new Date(state.lastRenderAt))}`;
    }
  } catch (err) {
    if (isStale()) return;
    state.unreachable = true;
    updateBanner();
    content.textContent = "";
    content.appendChild(errorBlock((t("netError") || "网络错误") + ": " + err.message, t("generalErrorHint") || "请检查控制平面连接"));
  }
}

function updateNavLabelText(id, text) {
  const elNode = document.getElementById(id);
  if (!elNode) return;
  const labelSpan = elNode.querySelector(".nav-label");
  if (labelSpan) {
    labelSpan.textContent = text;
  } else {
    elNode.textContent = text;
  }
}

export function updateStaticTexts() {
  if (typeof document !== "undefined" && document.documentElement) {
    document.documentElement.lang = getLang() === "zh" ? "zh-CN" : "en";
  }

  const brandName = document.getElementById("brandName");
  if (brandName) brandName.textContent = t("brandTitle");

  const mobileBrandName = document.getElementById("mobileBrandName");
  if (mobileBrandName) mobileBrandName.textContent = t("brandTitle");

  const brandSubtitle = document.getElementById("brandSubtitle");
  if (brandSubtitle) brandSubtitle.textContent = t("brandSubtitle") || "Evaluation Pipeline Console";

  const brandLink = document.querySelector(".brand");
  if (brandLink) {
    brandLink.setAttribute("aria-label", t("brandAria") || t("brandTitle"));
  }

  // Primary Overview & Unified Workbench Navigation
  updateNavLabelText("nav-overview", t("navOverview"));
  updateNavLabelText("nav-compose", t("navCompose"));
  const navCompose = document.getElementById("nav-compose");
  if (navCompose) {
    navCompose.title = t("navWorkbenchTip") || t("workbenchDesc");
  }

  const tagWorkbench = document.getElementById("tagWorkbench");
  if (tagWorkbench) tagWorkbench.textContent = t("navTagWorkbench") || "Flow";

  // Monitoring Group Labels & Items
  const opsLabel = document.getElementById("navOpsLabel");
  if (opsLabel) opsLabel.textContent = t("navOpsLabel");

  updateNavLabelText("nav-algorithms", t("navAlgorithms"));
  updateNavLabelText("nav-capacity", t("navCapacity"));
  updateNavLabelText("nav-shapes", t("navShapes"));

  const lookup = document.getElementById("lookup");
  if (lookup) {
    lookup.placeholder = t("lookupPlaceholder") || "study:…, run:…, problem:…";
    lookup.setAttribute("aria-label", t("lookupAria") || "Search entities");
  }

  const searchKbd = document.getElementById("searchKbd");
  if (searchKbd) {
    searchKbd.title = t("searchKbdTip") || "Press Enter to jump";
  }

  const helpBtnText = document.getElementById("helpBtnText");
  if (helpBtnText) helpBtnText.textContent = t("helpBtnText") || "说明";
  const helpBtn = document.getElementById("helpBtn");
  if (helpBtn) {
    helpBtn.setAttribute("aria-label", t("helpBtnAria") || "Toggle help");
    helpBtn.title = t("helpBtnTip") || "Status legend and usage help";
  }

  const langBtnText = document.getElementById("langBtnText");
  if (langBtnText) langBtnText.textContent = t("langName") || "EN";
  const langBtn = document.getElementById("langBtn");
  if (langBtn) {
    langBtn.setAttribute("aria-label", t("langBtnAria") || "Switch language");
    langBtn.title = t("langBtnTip") || "Switch language";
  }

  const intervalLabel = document.getElementById("intervalLabel");
  if (intervalLabel) intervalLabel.textContent = t("intervalLabel") || "间隔";
  const interval = document.getElementById("interval");
  if (interval) interval.setAttribute("aria-label", t("intervalAria") || "Polling Interval");
  const intervalPickerLabel = document.querySelector(".interval-picker");
  if (intervalPickerLabel) {
    intervalPickerLabel.title = t("intervalPickerTip") || "Polling refresh interval";
  }

  const mf = document.getElementById("mockfail");
  if (mf) {
    mf.textContent = (typeof window !== "undefined" && window.__mockFail) ? (t("restoreService") || "恢复服务") : (t("simulateOutage") || "模拟断网");
  }

  const mobileNavToggle = document.getElementById("mobileNavToggle");
  if (mobileNavToggle) {
    mobileNavToggle.setAttribute("aria-label", t("mobileNavToggleAria") || "Toggle Navigation");
  }

  // Always sync sidebar & mobile health and write-mode badge
  updateTopBarHealth();

  const refreshInfo = document.getElementById("refreshinfo");
  if (refreshInfo) {
    if (state.lastRenderAt) {
      refreshInfo.textContent = (t("lastRefreshed") || "最近刷新") + " " + fmtClockTime(new Date(state.lastRenderAt));
    } else {
      refreshInfo.textContent = t("lastRefreshedEmpty") || "最近刷新 —";
    }
  }

  // Update mobile header route title
  const currentRoute = state.route || parseRoute();
  const mobileRouteTitle = document.getElementById("mobileRouteTitle");
  if (mobileRouteTitle) {
    const routeTitles = {
      overview: t("navOverview"),
      compose: t("navCompose"),
      algorithms: t("navAlgorithms"),
      algorithm: t("navAlgorithms"),
      capacity: t("navCapacity"),
      shapes: t("navShapes")
    };
    mobileRouteTitle.textContent = routeTitles[currentRoute.name] || t("brandTitle");
  }

  const bannerText = document.getElementById("bannertext");
  if (bannerText) {
    bannerText.textContent = t("bannerUnreachableNoData");
  }

  renderHelpDrawer();
  updateBanner();
}

function renderHelpDrawer() {
  const drawer = document.getElementById("helpDrawer");
  if (!drawer) return;
  drawer.textContent = "";
  if (!state.helpEnabled) {
    drawer.style.display = "none";
    return;
  }
  drawer.style.display = "block";

  const wrap = el("div", "help-drawer-inner");
  wrap.appendChild(el("h3", "help-drawer-title", txt(t("statusLegendTitle") || "状态语义图例与说明")));

  const grid = el("div", "legend-grid");
  [
    { st: "queued", tone: "warn", desc: t("legendQueued") || "排队中" },
    { st: "running", tone: "info", desc: t("legendRunning") || "执行中" },
    { st: "recovering", tone: "warn", desc: t("legendRecovering") || "恢复中" },
    { st: "qualified", tone: "ok", desc: t("legendQualified") || "成功达标" },
    { st: "failed", tone: "bad", desc: t("legendFailed") || "失败/未决" },
    { st: "cancelled", tone: "dim", desc: t("legendCancelled") || "已取消" }
  ].forEach(item => {
    const row = el("div", "legend-item");
    row.appendChild(chip(item.tone, item.st));
    row.appendChild(el("span", "legend-desc", txt(item.desc)));
    grid.appendChild(row);
  });
  wrap.appendChild(grid);
  drawer.appendChild(wrap);
}

function startPollingTimer() {
  if (refreshTimer !== null) clearInterval(refreshTimer);
  const sec = parseInt(document.getElementById("interval")?.value || "10", 10) || 10;
  refreshTimer = setInterval(() => {
    if (!document.hidden) {
      refreshHealth();
      refreshRoute();
    }
  }, sec * 1000);
}

function initSearchLookup() {
  const input = document.getElementById("lookup");
  if (!input) return;

  function doLookup() {
    const v = input.value.trim();
    if (!v) return;
    if (v.startsWith("run:") || v.startsWith("algorithm:") || v.startsWith("algo:")) {
      navigate(`#/algorithm/${encodeURIComponent(v)}`);
    } else if (v.startsWith("problem:")) {
      navigate(`#/problem/${encodeURIComponent(v)}`);
    } else if (v.startsWith("schema:") || v.startsWith("sha256:")) {
      navigate(`#/schema/${encodeURIComponent(v)}`);
    } else {
      navigate(`#/study/${encodeURIComponent(v)}`);
    }
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doLookup();
  });
}

function initEventListeners() {
  window.addEventListener("hashchange", () => {
    refreshRoute();
  });

  const langBtn = document.getElementById("langBtn");
  if (langBtn) {
    langBtn.addEventListener("click", () => {
      const nextLang = getLang() === "zh" ? "en" : "zh";
      setLang(nextLang);
      if (typeof window !== "undefined" && window.location) {
        const url = new URL(window.location.href);
        if (url.searchParams.has("lang")) {
          url.searchParams.set("lang", nextLang);
          window.history.replaceState(null, "", url.toString());
        }
      }
      updateStaticTexts();
      refreshRoute();
    });
  }

  const helpBtn = document.getElementById("helpBtn");
  if (helpBtn) {
    helpBtn.addEventListener("click", () => {
      state.helpEnabled = !state.helpEnabled;
      helpBtn.setAttribute("aria-pressed", state.helpEnabled ? "true" : "false");
      renderHelpDrawer();
    });
  }

  const interval = document.getElementById("interval");
  if (interval) {
    interval.addEventListener("change", startPollingTimer);
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      refreshHealth();
      refreshRoute();
    }
  });

  const mockFailBtn = document.getElementById("mockfail");
  if (mockFailBtn) {
    if (IS_MOCK) mockFailBtn.hidden = false;
    mockFailBtn.addEventListener("click", () => {
      window.__mockFail = !window.__mockFail;
      mockFailBtn.setAttribute("aria-pressed", window.__mockFail ? "true" : "false");
      mockFailBtn.textContent = window.__mockFail ? (t("restoreService") || "恢复服务") : (t("simulateOutage") || "模拟断网");
      refreshHealth();
      refreshRoute();
    });
  }

  // Mobile Drawer & Overlay Management
  const mobileToggle = document.getElementById("mobileNavToggle");
  const sidebar = document.getElementById("appSidebar");
  const backdrop = document.getElementById("sidebarBackdrop");
  let lastFocusedElement = null;

  function closeMobileSidebar() {
    if (sidebar) sidebar.classList.remove("open");
    if (backdrop) {
      backdrop.classList.remove("open");
      backdrop.setAttribute("aria-hidden", "true");
    }
    if (typeof document !== "undefined" && document.body) {
      document.body.classList.remove("drawer-open");
    }
    if (mobileToggle) {
      mobileToggle.classList.remove("active");
      mobileToggle.setAttribute("aria-expanded", "false");
    }
    if (lastFocusedElement && typeof lastFocusedElement.focus === "function") {
      try { lastFocusedElement.focus(); } catch (_) {}
    }
  }

  function openMobileSidebar() {
    if (typeof document !== "undefined") {
      lastFocusedElement = document.activeElement;
    }
    if (sidebar) sidebar.classList.add("open");
    if (backdrop) {
      backdrop.classList.add("open");
      backdrop.setAttribute("aria-hidden", "false");
    }
    if (typeof document !== "undefined" && document.body) {
      document.body.classList.add("drawer-open");
    }
    if (mobileToggle) {
      mobileToggle.classList.add("active");
      mobileToggle.setAttribute("aria-expanded", "true");
    }
    // Focus search input or active nav item inside drawer
    setTimeout(() => {
      const focusTarget = sidebar?.querySelector("input#lookup, a.nav-item.cur, a.nav-item");
      if (focusTarget && typeof focusTarget.focus === "function") {
        try { focusTarget.focus(); } catch (_) {}
      }
    }, 50);
  }

  function toggleMobileSidebar() {
    const isOpen = sidebar?.classList.contains("open");
    if (isOpen) {
      closeMobileSidebar();
    } else {
      openMobileSidebar();
    }
  }

  if (mobileToggle) {
    mobileToggle.addEventListener("click", toggleMobileSidebar);
  }

  if (backdrop) {
    backdrop.addEventListener("click", closeMobileSidebar);
  }

  // Keyboard navigation & Esc key handling for drawer
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && sidebar?.classList.contains("open")) {
      closeMobileSidebar();
    }
  });

  // Focus trapping within open drawer
  if (sidebar) {
    sidebar.addEventListener("keydown", (e) => {
      if (!sidebar.classList.contains("open")) return;
      if (e.key === "Tab") {
        const focusable = sidebar.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])');
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    });
  }

  // Close sidebar on link click when in mobile drawer mode
  if (sidebar) {
    const navAnchors = sidebar.querySelectorAll("a.nav-item, a.brand");
    navAnchors.forEach(a => {
      a.addEventListener("click", () => {
        if (window.innerWidth <= 880) {
          closeMobileSidebar();
        }
      });
    });
  }

  initSearchLookup();
}

// Global Banner Update Interval
setInterval(updateBanner, 1000);

// Initialize Application
async function initApp() {
  initEventListeners();
  updateStaticTexts();
  await prefetchEntityLists();
  await refreshHealth();
  await refreshRoute();
  startPollingTimer();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initApp);
} else {
  initApp();
}
