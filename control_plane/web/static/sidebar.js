import { t } from "./i18n.js";

const mobileLayout = window.matchMedia("(max-width: 880px)");

export function updateSidebarLabels() {
  const sidebar = document.getElementById("appSidebar");
  const button = document.getElementById("mobileNavToggle");
  const expanded = mobileLayout.matches
    ? sidebar.classList.contains("open")
    : !document.getElementById("appShell").classList.contains("sidebar-collapsed");
  const label = t(expanded ? "collapseSidebar" : "expandSidebar");
  button.setAttribute("aria-label", label);
  button.setAttribute("aria-expanded", String(expanded));
  button.title = label;
  document.getElementById("sidebarClose").setAttribute("aria-label", t("closeSidebar"));
}

export function initSidebar() {
  const shell = document.getElementById("appShell");
  const sidebar = document.getElementById("appSidebar");
  const canvas = document.querySelector(".app-main-canvas");
  const toggle = document.getElementById("mobileNavToggle");
  const backdrop = document.getElementById("sidebarBackdrop");
  try {
    shell.classList.toggle("sidebar-collapsed", localStorage.getItem("simtakt_sidebar_collapsed") === "true");
  } catch (_) { /* Navigation works even when storage is unavailable. */ }

  function sync() {
    const open = mobileLayout.matches && sidebar.classList.contains("open");
    sidebar.inert = mobileLayout.matches && !open;
    canvas.inert = open;
    backdrop.classList.toggle("open", open);
    backdrop.setAttribute("aria-hidden", String(!open));
    document.body.classList.toggle("drawer-open", open);
    updateSidebarLabels();
  }

  function close(restoreFocus = true) {
    sidebar.classList.remove("open");
    sync();
    if (restoreFocus) toggle.focus();
  }

  toggle.addEventListener("click", () => {
    if (mobileLayout.matches) {
      sidebar.classList.add("open");
      sync();
      document.getElementById("sidebarClose").focus();
    } else {
      const collapsed = shell.classList.toggle("sidebar-collapsed");
      try { localStorage.setItem("simtakt_sidebar_collapsed", String(collapsed)); } catch (_) {}
      sync();
    }
  });
  document.getElementById("sidebarClose").addEventListener("click", () => close());
  backdrop.addEventListener("click", () => close());
  mobileLayout.addEventListener("change", () => close(false));
  sidebar.querySelectorAll("a.nav-item, a.brand").forEach(link => {
    link.addEventListener("click", () => { if (mobileLayout.matches) close(); });
  });
  document.addEventListener("keydown", event => {
    if (!mobileLayout.matches || !sidebar.classList.contains("open")) return;
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    }
    if (event.key === "Tab") {
      const items = [...sidebar.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled])')]
        .filter(node => !node.hidden && node.getClientRects().length);
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    }
  });
  sync();
}
