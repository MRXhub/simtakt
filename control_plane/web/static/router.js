/**
 * Hash Router & Navigation
 */

import { t } from "./i18n.js";
import { state } from "./state.js";

export function parseRoute() {
  const hashStr = location.hash || "#/";
  const [hash, query] = hashStr.split("?");
  const queryParams = new URLSearchParams(query || "");

  if (hash === "#" || hash === "#/" || hash === "#/overview") return { name: "overview", query: queryParams };

  // Consolidated Workbench Stages & Deep Links
  if (hash === "#/packages") return { name: "compose", step: 1, query: queryParams };
  if (hash === "#/schemas") return { name: "compose", step: 2, query: queryParams };
  if (hash.startsWith("#/schema/")) {
    const id = decodeURIComponent(hash.slice("#/schema/".length));
    return { name: "compose", step: 2, id, detailType: "schema", query: queryParams };
  }
  if (hash === "#/problems") return { name: "compose", step: 3, query: queryParams };
  if (hash.startsWith("#/problem/")) {
    const id = decodeURIComponent(hash.slice("#/problem/".length));
    return { name: "compose", step: 3, id, detailType: "problem", query: queryParams };
  }
  if (hash === "#/studies") return { name: "compose", step: 4, query: queryParams };
  if (hash.startsWith("#/study/")) {
    const id = decodeURIComponent(hash.slice("#/study/".length));
    return { name: "compose", step: 4, id, detailType: "study", query: queryParams };
  }
  if (hash === "#/submit" || hash === "#/compose" || hash === "#/workbench") {
    const stepParam = parseInt(queryParams.get("step") || "5", 10);
    const step = (stepParam >= 1 && stepParam <= 5) ? stepParam : 5;
    return { name: "compose", step, query: queryParams };
  }

  // Monitoring
  if (hash === "#/algorithms") return { name: "algorithms", query: queryParams };
  if (hash.startsWith("#/algorithm/")) return { name: "algorithm", id: decodeURIComponent(hash.slice("#/algorithm/".length)), query: queryParams };
  if (hash === "#/capacity") return { name: "capacity", query: queryParams };
  if (hash === "#/shapes") return { name: "shapes", query: queryParams };

  return { name: "unknown", raw: hashStr, query: queryParams };
}

export function navigate(path) {
  if (path.startsWith("#")) location.hash = path;
  else location.hash = `#/${path.replace(/^\//, "")}`;
}

export function updateDocTitle(route) {
  const titles = {
    overview: t("overviewTitle"),
    compose: t("workbenchTitle"),
    algorithms: t("algorithmsTitle"),
    algorithm: t("algorithmTitle", { id: route.id || "" }),
    capacity: t("capacityTitle"),
    shapes: t("shapesTitle")
  };
  const mainTitle = titles[route.name] || t("unknownRouteTitle");
  document.title = `${mainTitle} · ${t("brandTitle")}`;
}

export function markNav(route) {
  const navMap = {
    overview: "nav-overview",
    compose: "nav-compose",
    algorithms: "nav-algorithms",
    algorithm: "nav-algorithms",
    capacity: "nav-capacity",
    shapes: "nav-shapes"
  };

  const navTitles = {
    overview: t("navOverview"),
    compose: t("navCompose"),
    algorithms: t("navAlgorithms"),
    algorithm: t("navAlgorithms"),
    capacity: t("navCapacity"),
    shapes: t("navShapes")
  };

  const navElements = [
    "nav-overview", "nav-compose", "nav-algorithms", "nav-capacity", "nav-shapes"
  ];

  const activeId = navMap[route.name] || "nav-compose";
  navElements.forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      if (id === activeId) el.classList.add("cur");
      else el.classList.remove("cur");
    }
  });

  const mobileRouteTitle = document.getElementById("mobileRouteTitle");
  if (mobileRouteTitle) {
    mobileRouteTitle.textContent = navTitles[route.name] || t("brandTitle");
  }
}
