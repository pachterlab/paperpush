// The page's reading aids: the side menu, the filter and search bar, the
// stat tiles, the description toggle, and the copy buttons. They read only
// the DOM (each field card's class and data-* attributes), so the same file
// runs on the live page, where app.js keeps those attributes current, and is
// inlined into a page saved with "Save this page" to send to a PI or
// collaborator, where nothing else runs. It also holds the light/dark toggle
// and gets the page ready for printing (Save as PDF), for the same reason.
//
// A field card is `.card[data-field]` with one status class (st-ok,
// st-warn, st-issue, st-review, st-empty), `data-filled` when it has a
// value, `data-search` (lowercased label, id, and value), and `data-value`.
// A menu entry is `.toc-item[data-target]` naming the card's element id.
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  let filter = "all";
  let observer = null;

  function matches(card, q) {
    const st = card.className;
    const ok =
      filter === "all" ||
      (filter === "filled" && card.hasAttribute("data-filled")) ||
      (filter === "empty" && !card.hasAttribute("data-filled")) ||
      (filter === "issue" && /\bst-(issue|warn)\b/.test(st)) ||
      (filter === "review" && /\bst-review\b/.test(st));
    return ok && (!q || (card.dataset.search || "").includes(q));
  }

  function apply() {
    const box = $("#field-search");
    const q = box ? box.value.trim().toLowerCase() : "";
    let shown = 0;
    for (const card of $$(".card[data-field]")) {
      const show = matches(card, q);
      if (show) shown++;
      card.classList.toggle("is-hidden", !show);
      const item = $(`.toc-item[data-target="${card.id}"]`);
      if (item) item.classList.toggle("is-hidden", !show);
    }
    // A group with nothing left to show disappears, in the form and the menu.
    for (const group of $$(".field-group")) {
      const any = $$(".card[data-field]", group).some((c) => !c.classList.contains("is-hidden"));
      group.classList.toggle("is-hidden", !any);
      const head = $(`.toc-group[data-group="${group.dataset.group}"]`);
      if (head) head.classList.toggle("is-hidden", !any);
    }
    const none = $("#filter-none");
    if (none) none.hidden = shown > 0 || !$$(".card[data-field]").length;
    $$("[data-filter]").forEach((b) => b.classList.toggle("on", b.dataset.filter === filter));
  }

  function setFilter(next) {
    filter = next;
    apply();
  }

  // Highlight the menu entry of the card at the top of the screen.
  function observe() {
    if (observer) observer.disconnect();
    if (!("IntersectionObserver" in window)) return;
    observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          $$(".toc-item").forEach((t) => t.classList.toggle("active", t.dataset.target === e.target.id));
          const active = $(".toc-item.active");
          const nav = $("#sidenav");
          if (active && nav && nav.scrollHeight > nav.clientHeight) {
            const r = active.getBoundingClientRect();
            const n = nav.getBoundingClientRect();
            if (r.top < n.top + 40 || r.bottom > n.bottom - 40) active.scrollIntoView({ block: "nearest" });
          }
        }
      },
      { rootMargin: "-130px 0px -70% 0px" },
    );
    $$(".toc-item[data-target]").forEach((t) => {
      const target = document.getElementById(t.dataset.target);
      if (target) observer.observe(target);
    });
  }

  async function copy(button) {
    const card = button.closest("[data-value]");
    const value = card ? card.dataset.value : "";
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      const t = document.createElement("textarea");
      t.value = value;
      document.body.append(t);
      t.select();
      document.execCommand("copy");
      t.remove();
    }
    button.textContent = "Copied";
    button.classList.add("done");
    setTimeout(() => { button.textContent = "Copy"; button.classList.remove("done"); }, 1200);
  }

  // Delegated, so it keeps working when app.js rebuilds the form.
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-filter], .toc-item, .jump, #desc-toggle, .copy");
    if (!t) return;
    if (t.matches("[data-filter]")) {
      setFilter(filter === t.dataset.filter && t.classList.contains("stat") ? "all" : t.dataset.filter);
    } else if (t.matches(".toc-item, .jump")) {
      // Not a plain #anchor jump: on the live page the hash is the router.
      e.preventDefault();
      const target = document.getElementById(t.dataset.target);
      if (target?.tagName === "DETAILS") target.open = true;
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    } else if (t.id === "desc-toggle") {
      const hide = document.body.classList.toggle("hide-desc");
      t.classList.toggle("on", !hide);
      t.setAttribute("aria-pressed", String(!hide));
    } else if (t.matches(".copy")) {
      copy(t);
    }
  });
  document.addEventListener("input", (e) => { if (e.target.id === "field-search") apply(); });
  document.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches(".stat[role=button]")) { e.preventDefault(); e.target.click(); }
  });

  // The sticky menu and toolbar sit under the top bar, which wraps onto two
  // lines on a phone: track its real height.
  function trackTopbar() {
    const bar = $(".topbar");
    if (!bar) return;
    const set = () => document.documentElement.style.setProperty("--topbar-h", `${bar.offsetHeight}px`);
    set();
    if ("ResizeObserver" in window) new ResizeObserver(set).observe(bar);
  }

  // ---- light and dark. index.html's <head> sets the first theme before the
  // page paints; the toggle remembers the author's choice on this device.
  // Without a choice, the page follows the system setting as it changes.
  const THEME_KEY = "paperpush:theme";
  const chosenTheme = () => { try { return localStorage.getItem(THEME_KEY); } catch { return null; } };
  const systemDark = () => Boolean(window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches);

  function setTheme(theme, remember = false) {
    document.documentElement.dataset.theme = theme;
    if (remember) { try { localStorage.setItem(THEME_KEY, theme); } catch { /* storage unavailable */ } }
    const button = $("#theme-toggle");
    if (button) {
      const label = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
      button.title = label;
      button.setAttribute("aria-label", label);
    }
  }

  function wireTheme() {
    const html = document.documentElement;
    const stored = chosenTheme();
    // A saved page keeps the theme it was saved in, unless this device has a choice.
    setTheme(stored === "light" || stored === "dark" ? stored : html.dataset.theme || (systemDark() ? "dark" : "light"));
    if (window.matchMedia) {
      matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", (e) => {
        if (!chosenTheme()) setTheme(e.matches ? "dark" : "light");
      });
    }
  }
  document.addEventListener("click", (e) => {
    if (e.target.closest("#theme-toggle")) setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
  });

  // ---- printing (Save as PDF, or the browser's own Print): light colours,
  // every collapsed check open, and each text box as tall as its text so
  // nothing is cut off. All undone afterwards.
  let printUndo = null;
  window.addEventListener("beforeprint", () => {
    if (printUndo) return;
    const html = document.documentElement;
    const theme = html.dataset.theme;
    // The guidelines panel and the lists of checks that do not apply stay as
    // the author left them: opened, they run to pages.
    const closed = $$("details:not([open])").filter((d) => d.id !== "req-panel" && !d.classList.contains("na"));
    const heights = $$("textarea").map((t) => [t, t.style.height]);
    html.dataset.theme = "light";
    closed.forEach((d) => (d.open = true));
    for (const [t] of heights) { t.style.height = "auto"; t.style.height = `${t.scrollHeight + 2}px`; }
    printUndo = () => {
      html.dataset.theme = theme;
      closed.forEach((d) => (d.open = false));
      for (const [t, h] of heights) t.style.height = h;
    };
  });
  window.addEventListener("afterprint", () => { if (printUndo) { printUndo(); printUndo = null; } });

  window.PaperpushViewer = { apply, observe, setFilter, setTheme, print: () => window.print(), refresh() { observe(); apply(); } };
  const start = () => { trackTopbar(); wireTheme(); window.PaperpushViewer.refresh(); };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
