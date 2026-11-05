// paperpush website: pick a venue, upload the manuscript, fill in the venue's
// .sub through a form, and check it.
//
// The .sub text (state.text) is the single source of truth: every widget
// writes into it through Subfile.setField and reads back from Subfile.parse.
// The checks are shown in four blocks (see paperpush/web/app.py:check_group):
// the form checks LiveChecks re-runs on every edit, the ones only `validate`
// can do because they read the uploaded files, the ones only an LLM can
// judge, and the guideline rules paperpush has no check for. /plan lists the
// last three before anything is checked, so they start grey.
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // A tab opened from Settings > New (?new) keeps its workspace and drafts
  // in sessionStorage, so it starts empty and never overwrites another tab's.
  const FRESH = new URLSearchParams(location.search).has("new");
  const areaFor = (key) => (FRESH && /^(workspace|draft:)/.test(key) ? sessionStorage : localStorage);
  const store = {
    get(key) {
      try { return areaFor(key).getItem(`paperpush:${key}`); } catch { return null; }
    },
    set(key, value) {
      try { areaFor(key).setItem(`paperpush:${key}`, value); } catch { /* storage unavailable */ }
    },
    del(key) {
      try { areaFor(key).removeItem(`paperpush:${key}`); } catch { /* storage unavailable */ }
    },
  };

  // The visitor's own LLM API key: in sessionStorage (this tab only) unless
  // they ask to remember it, then localStorage. Sent only with the autofill
  // and AI-validate requests, in headers (see paperpush/web/app.py:visitor_llm).
  const storageArea = (name) => { try { return window[name]; } catch { return null; } };
  const llmKey = {
    load() {
      for (const name of ["sessionStorage", "localStorage"]) {
        try {
          const area = storageArea(name);
          const key = area && area.getItem("paperpush:llm-key");
          if (key) return { key, provider: area.getItem("paperpush:llm-provider") || "", remember: name === "localStorage" };
        } catch { /* storage unavailable */ }
      }
      return { key: "", provider: "", remember: false };
    },
    save(key, provider, remember) {
      for (const name of ["sessionStorage", "localStorage"]) {
        try {
          const area = storageArea(name);
          area.removeItem("paperpush:llm-key");
          area.removeItem("paperpush:llm-provider");
        } catch { /* storage unavailable */ }
      }
      if (!key) return;
      try {
        const area = storageArea(remember ? "localStorage" : "sessionStorage");
        area.setItem("paperpush:llm-key", key);
        area.setItem("paperpush:llm-provider", provider);
      } catch { /* storage unavailable: the key lives only in the page */ }
    },
  };
  const PROVIDER_LABELS = { anthropic: "Anthropic", openai: "OpenAI", google: "Gemini" };
  const guessProvider = (key) => (key.startsWith("sk-ant-") ? "anthropic" : key.startsWith("AIza") ? "google" : key.startsWith("sk-") ? "openai" : "");

  const state = {
    llm: { key: "", provider: "", remember: false },
    info: null,
    venues: [],
    venue: null, // venue detail from /api/venues/<slug>
    fields: new Map(), // id -> field definition
    widgets: new Map(), // id -> {set, refresh?, card}
    text: "",
    ws: null, // workspace id
    files: [], // [{name, size}]
    plan: null, // /plan response
    liveRules: {},
    requirements: null,
    live: { checks: [], byField: {} },
    results: null, // /validate response
    resultsStale: false,
    // A file was removed or replaced since the last validation: the checks
    // that read files (the validate and AI blocks) go back to grey. Adding a
    // file leaves them as they were (out of date): they still describe the
    // files that are still there.
    filesChanged: false,
    initialSubmission: false, // "initial submission (any format)" ticked: errors become warnings
    touched: false, // the author has changed something: colour the live checks
    showRequired: false, // flag empty required fields (after the first Validate)
    review: new Set(), // field ids autofill asked the author to confirm
    pendingPaths: null, // zip paths waiting to be matched to the venue's upload fields
    importSkipped: null, // zip members not unpacked, for the import summary
    busy: false,
    // Set when the CLI opened this page with --web: {path, token, ...}. The
    // page then edits that .sub file on the author's machine (saved back as
    // they go) and file paths are real paths, not upload names.
    local: null,
    // Set while editing a shared form (#/f/<id>/<key>): {id, key, aes, token,
    // version, base}. `base` is the text as last saved to (or read from) the
    // server; the page's edits are state.text minus that.
    shared: null,
  };

  // ------------------------------------------------------------------ utils

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, "");
      else node.setAttribute(k, v);
    }
    for (const c of children.flat()) if (c != null && c !== false) node.append(c);
    return node;
  }

  async function api(path, opts = {}) {
    if (state.local) opts = { ...opts, headers: { ...(opts.headers || {}), "X-Paperpush-Token": state.local.token } };
    const res = await fetch(path, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
      const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      err.status = res.status;
      throw err;
    }
    return res;
  }
  const getJSON = async (path) => (await api(path)).json();
  const postJSON = async (path, body, headers = {}) =>
    (await api(path, { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) })).json();

  // POST `body` with `stream: true` and read the NDJSON answer: call
  // `onProgress` for each {"progress"} line and resolve with the {"result"}.
  async function postStream(path, body, headers = {}, onProgress = () => {}) {
    const res = await api(path, { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify({ ...body, stream: true }) });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result = null;
    const handle = (line) => {
      if (!line.trim()) return;
      const event = JSON.parse(line);
      if (event.progress) onProgress(event.progress);
      else if (event.error) throw new Error(event.error);
      else if ("result" in event) result = event.result;
    };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      lines.forEach(handle);
    }
    handle(buffer + decoder.decode());
    if (result === null) throw new Error("The server stopped before validation finished. Try again.");
    return result;
  }

  function llmHeaders() {
    if (!state.llm.key) return {};
    const headers = { "X-LLM-Key": state.llm.key };
    if (state.llm.provider) headers["X-LLM-Provider"] = state.llm.provider;
    return headers;
  }

  const KEY_TOOLTIP = "Please provide an API key to enable AI features";

  // Which LLM the visitor's key selects, or why AI cannot run.
  function aiAvailability() {
    if (!state.info || !state.info.ai) return { label: null, reason: "AI features are switched off on this server. Running paperpush yourself? Use `paperpush autofill` and `paperpush validate --ai`." };
    if (!state.llm.key && envAi()) return { label: `the ${PROVIDER_LABELS[state.info.ai_provider] || state.info.ai_provider} key set in your environment`, reason: "" };
    if (!state.llm.key) return { label: null, reason: KEY_TOOLTIP };
    const provider = state.llm.provider || guessProvider(state.llm.key);
    if (!provider) return { label: null, reason: "Choose which provider your API key is for." };
    return { label: `your ${PROVIDER_LABELS[provider]} key`, reason: "" };
  }

  function renderKeyPanel() {
    $("#key-panel").classList.toggle("has-key", Boolean(state.llm.key));
  }

  // Run locally (--web), the server may use an API key from the environment.
  const envAi = () => Boolean(state.local && state.info && state.info.ai_provider);

  function setAiTooltip(wrap, reason) {
    const message = !state.llm.key && !envAi() ? KEY_TOOLTIP : reason;
    wrap.dataset.tooltip = message;
    wrap.setAttribute("aria-label", message || "AI feature");
  }

  function wireKeyPanel() {
    state.llm = llmKey.load();
    const input = $("#key-input");
    const provider = $("#key-provider");
    const remember = $("#key-remember");
    input.value = state.llm.key;
    provider.value = state.llm.provider;
    remember.checked = state.llm.remember;
    const update = () => {
      state.llm = { key: input.value.trim(), provider: provider.value, remember: remember.checked };
      llmKey.save(state.llm.key, state.llm.provider, state.llm.remember);
      renderKeyPanel();
      if (state.venue) renderManuscript();
      renderAiAvailability();
    };
    input.addEventListener("input", update);
    provider.addEventListener("change", update);
    remember.addEventListener("change", update);
    for (const wrap of [$("#autofill-wrap"), $("#validate-ai-wrap")]) {
      wrap.addEventListener("click", () => { if (wrap.dataset.tooltip) wrap.focus(); });
    }
  }

  let toastTimer = null;
  function toast(message, kind = "") {
    const t = $("#toast");
    t.textContent = message;
    t.className = `toast ${kind}`;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.hidden = true), 5000);
  }

  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  // How a file is named in the pickers: its upload name, or (run locally with
  // --web) the path exactly as the .sub gives it.
  const baseName = (path) => (state.local ? path.trim() : path.trim().replace(/\\/g, "/").split("/").pop());
  const extOf = (name) => (name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
  const humanize = (key) => key.replace(/_/g, " ").replace(/\bmm\b/, "(mm)").replace(/\bpt\b/, "(pt)").replace(/\bmb\b/, "(MB)").replace(/\bdpi\b/, "dpi");
  const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

  function download(blob, filename) {
    const a = el("a", { href: URL.createObjectURL(blob), download: filename });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  function values() {
    return Subfile.parse(state.text).values;
  }

  // --------------------------------------------------------------- settings

  // ?palette=alt (from `paperpush validate --web --colorblind`) turns the
  // colour-blind-friendly colours on and remembers the choice; read once at
  // load, so the settings checkbox can still turn them off.
  function paletteFromUrl() {
    const asked = new URLSearchParams(location.search).get("palette");
    if (asked === "alt" || asked === "default") store.set("palette", asked);
  }

  function applySettings() {
    const alt = store.get("palette") === "alt";
    document.documentElement.dataset.palette = alt ? "alt" : "default";
    $("#opt-colorblind").checked = alt;
  }

  function wireSettings() {
    const btn = $("#settings-btn");
    const menu = $("#settings-menu");
    const close = () => { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); };
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
      btn.setAttribute("aria-expanded", String(!menu.hidden));
    });
    document.addEventListener("click", (e) => { if (!menu.contains(e.target)) close(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    $("#opt-colorblind").addEventListener("change", (e) => { store.set("palette", e.target.checked ? "alt" : "default"); applySettings(); });
    $("#new-btn").addEventListener("click", () => {
      close();
      window.open(`${location.pathname}?new=${Date.now().toString(36)}`, "_blank", "noopener");
    });
    $("#open-sub").addEventListener("change", async (e) => {
      const files = Array.from(e.target.files);
      e.target.value = "";
      close();
      importFiles(files);
    });
    $("#reset-btn").addEventListener("click", async () => {
      close();
      if (state.ws) { try { await api(`/api/workspaces/${state.ws}`, { method: "DELETE" }); } catch { /* already gone */ } }
      store.del("workspace");
      if (state.venue) store.del(`draft:${state.venue.slug}`);
      location.hash = "";
      location.reload();
    });
  }

  // ---------------------------------------------------------- venue picker

  const TYPE_LABELS = { preprint: "Preprint servers", journal: "Journals", conference: "Conferences" };
  let comboActive = -1;

  function venueMatches(q) {
    q = q.trim().toLowerCase();
    return state.venues.filter((v) => !q || [v.slug, v.name, v.full_name, v.description].some((s) => (s || "").toLowerCase().includes(q)));
  }

  function renderCombo(show = true) {
    const input = $("#venue-search");
    const list = $("#venue-options");
    const matches = venueMatches(input.value);
    list.replaceChildren();
    let index = 0;
    for (const type of Object.keys(TYPE_LABELS)) {
      const group = matches.filter((v) => v.venue_type === type);
      if (!group.length) continue;
      list.append(el("li", { class: "group", role: "presentation", text: TYPE_LABELS[type] }));
      for (const v of group) {
        const li = el(
          "li",
          { role: "option", id: `venue-opt-${index}`, "data-slug": v.slug, "data-index": index, class: index === comboActive ? "active" : null },
          el("span", { class: "vname", text: v.name }),
          el("span", { class: "vfull", text: v.full_name && v.full_name !== v.name ? v.full_name : v.description || "" }),
          v.supports_submit ? null : el("span", { class: "chip plain", title: "paperpush checks the manuscript but cannot fill this venue's portal yet", text: "validate only" }),
        );
        li.addEventListener("mousedown", (e) => { e.preventDefault(); chooseVenue(v.slug); });
        list.append(li);
        index++;
      }
    }
    if (!index) list.append(el("li", { class: "none", text: `No venue matches "${input.value.trim()}".` }));
    list.hidden = !show;
    input.setAttribute("aria-expanded", String(show));
    input.setAttribute("aria-activedescendant", comboActive >= 0 ? `venue-opt-${comboActive}` : "");
  }

  function wireCombo() {
    const input = $("#venue-search");
    const list = $("#venue-options");
    input.addEventListener("focus", () => { input.select(); comboActive = -1; renderCombo(); });
    input.addEventListener("input", () => { comboActive = 0; renderCombo(); });
    input.addEventListener("blur", () => {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      if (state.venue) input.value = state.venue.name;
    });
    input.addEventListener("keydown", (e) => {
      const count = $$("li[data-slug]", list).length;
      if (e.key === "ArrowDown") { e.preventDefault(); comboActive = Math.min(count - 1, comboActive + 1); renderCombo(); scrollActive(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); comboActive = Math.max(0, comboActive - 1); renderCombo(); scrollActive(); }
      else if (e.key === "Enter") {
        e.preventDefault();
        const li = $(`li[data-index="${Math.max(comboActive, 0)}"]`, list);
        if (li) chooseVenue(li.dataset.slug);
      } else if (e.key === "Escape") { input.blur(); }
    });
    $("#venue-toggle").addEventListener("mousedown", (e) => {
      e.preventDefault();
      if (document.activeElement === input && !list.hidden) input.blur();
      else { input.focus(); input.value = ""; renderCombo(); }
    });
  }

  function scrollActive() {
    const li = $(`#venue-opt-${comboActive}`);
    if (li) li.scrollIntoView({ block: "nearest" });
  }

  function chooseVenue(slug) {
    $("#venue-search").blur();
    if (state.venue && state.venue.slug === slug) return;
    location.hash = `#/v/${encodeURIComponent(slug)}`;
  }

  // ----------------------------------------------------------- open a venue

  async function openVenue(slug, text = null) {
    let venue;
    try {
      venue = await getJSON(`/api/venues/${encodeURIComponent(slug)}`);
    } catch (err) {
      toast(err.status === 404 ? `paperpush does not support "${slug}" yet.` : err.message, "error");
      location.hash = "";
      return;
    }
    venue.fields.forEach(renameReviewerField);
    Object.assign(state, {
      venue,
      fields: new Map(venue.fields.map((f) => [f.id, f])),
      plan: null,
      liveRules: {},
      requirements: venue.manuscript_requirements,
      results: null,
      resultsStale: false,
      filesChanged: false,
      initialSubmission: false,
      touched: false,
      showRequired: false,
      review: new Set(),
    });
    renderReportButton();
    const draft = store.get(`draft:${slug}`);
    const restored = text == null && draft && draft !== venue.template;
    state.text = text ?? (draft || venue.template);
    state.touched = Boolean(restored || text);

    hideNav(); // rebuilt with the form
    window.PaperpushViewer?.setFilter("all");
    $("#field-search").value = "";
    $("#venue-search").value = venue.name;
    $("#workspace").hidden = false;
    $("#restored").hidden = !restored;
    $("#import-result").hidden = true;
    document.title = `${venue.name} · paperpush`;
    renderVenueHead();

    await ensureWorkspace();
    renderManuscript();
    buildForm();
    renderRequirements();
    $("#sub-text").value = state.text;
    syncWidgets();
    runLive();
    renderChecks();
    renderAiAvailability();
    renderInitialSubmission();
    refreshPlan();
    applyPendingMatches();
  }

  function renderVenueHead() {
    const v = state.venue;
    $$("[data-venue-name]").forEach((n) => (n.textContent = v.name));
    $("#venue-name").textContent = v.full_name || v.name;
    $("#venue-sub").textContent = v.description || "";
    const chips = [];
    chips.push(el("span", { class: "chip plain", text: TYPE_LABELS[v.venue_type]?.replace(/s$/, "").replace(/ servers?$/, " server") || v.venue_type }));
    if (v.anonymous) chips.push(el("span", { class: "chip warn", title: "Nothing in the files may identify the authors", text: "Double-blind" }));
    if (!v.supports_submit) chips.push(el("span", { class: "chip plain", title: "paperpush checks the manuscript against the guidelines but cannot fill this portal yet", text: "Validate only" }));
    if (v.login_required) chips.push(el("span", { class: "chip plain", text: "Portal login needed to submit" }));
    $("#venue-chips").replaceChildren(...chips);
    // Validate-only venues have no runner to fill the portal: gray the button out.
    const portalWrap = $("#portal-wrap");
    $("#portal-btn").disabled = !v.supports_submit;
    portalWrap.dataset.tooltip = v.supports_submit ? "" : "Not supported for this venue";
    portalWrap.tabIndex = v.supports_submit ? -1 : 0;
    if (v.supports_submit) portalWrap.removeAttribute("aria-label");
    else portalWrap.setAttribute("aria-label", "Not supported for this venue");
    const links = [];
    if (v.submission_guide) links.push(el("a", { href: v.submission_guide, target: "_blank", rel: "noopener", text: "Author guidelines ↗" }));
    if (v.submission_url) links.push(el("a", { href: v.submission_url, target: "_blank", rel: "noopener", text: "Submission portal ↗" }));
    if (v.site_url) links.push(el("a", { href: v.site_url, target: "_blank", rel: "noopener", text: "Website ↗" }));
    $("#venue-links").replaceChildren(...links);
  }

  // -------------------------------------------------------------- workspace

  async function ensureWorkspace() {
    if (state.local) {
      state.ws = "local";
      try { state.files = (await getJSON("/api/workspaces/local")).files; } catch { /* keep the list we have */ }
      return;
    }
    const saved = store.get("workspace");
    if (saved) {
      try {
        const ws = await getJSON(`/api/workspaces/${encodeURIComponent(saved)}`);
        state.ws = saved;
        state.files = ws.files;
        return;
      } catch { /* expired: make a new one */ }
    }
    const ws = await postJSON("/api/workspaces", {});
    state.ws = ws.id;
    state.files = [];
    store.set("workspace", ws.id);
  }

  async function uploadFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return [];
    const body = new FormData();
    for (const f of files) body.append("files", f, f.name);
    toast(`Uploading ${plural(files.length, "file")}…`);
    try {
      const res = await (await api(`/api/workspaces/${state.ws}/files`, { method: "POST", body })).json();
      const listed = new Set(res.files.map((f) => f.name));
      state.files = [...res.files, ...state.files.filter((f) => !listed.has(f.name))];
      refreshFileWidgets();
      noteReplacedUploads(res.saved);
      toast(`Uploaded ${res.saved.join(", ")}`);
      return res.saved;
    } catch (err) {
      if (err.status === 404) {
        store.del("workspace");
        await ensureWorkspace();
        toast("Your previous upload space expired. Please upload again.", "error");
      } else toast(err.message, "error");
      return [];
    }
  }

  // ---------------------------------------------- open a .sub or a .zip

  // A .sub opens its venue's form. A zip (a folder of manuscript files, or a
  // bundle downloaded here) is unpacked into the workspace; any .sub inside
  // opens, and each file goes into the upload field its name points to
  // (/match: keyword matching, no AI), for the fields still empty.
  const isImportable = (name) => /\.(sub|zip|txt)$/i.test(name);

  async function importFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const bad = files.filter((f) => !isImportable(f.name));
    if (bad.length) { toast(`${bad.map((f) => f.name).join(", ")}: open a .sub or a .zip here. Upload other files from the form.`, "error"); return; }
    // A .txt is read here, as before: a .sub saved under another extension.
    const txt = files.filter((f) => /\.txt$/i.test(f.name));
    const rest = files.filter((f) => !/\.txt$/i.test(f.name));
    let subs = await Promise.all(txt.map(async (f) => ({ name: f.name, text: await f.text() })));
    let res = null;
    if (rest.length) {
      if (!state.ws) await ensureWorkspace();
      const body = new FormData();
      for (const f of rest) body.append("files", f, f.name);
      const zips = rest.filter((f) => /\.zip$/i.test(f.name)).length;
      if (zips) toast(`Unpacking ${plural(zips, "zip")}…`);
      try {
        res = await (await api(`/api/workspaces/${state.ws}/import`, { method: "POST", body })).json();
      } catch (err) {
        if (err.status === 404) {
          store.del("workspace");
          state.ws = null;
          await ensureWorkspace();
          toast("Your previous upload space expired. Please try again.", "error");
        } else toast(err.message, "error");
        return;
      }
      subs = subs.concat(res.subs);
      state.files = res.files;
      if (res.paths.length) state.pendingPaths = (state.pendingPaths || []).concat(res.paths);
      state.importSkipped = res.skipped;
    }
    // Several .sub files: the one for the open venue, else the first.
    const parsed = subs.map((s) => ({ ...s, venue: Subfile.parse(s.text).venue }));
    const chosen = parsed.find((s) => state.venue && s.venue === state.venue.slug) || parsed.find((s) => s.venue) || null;
    if (parsed.length && !chosen) toast(`${parsed.map((s) => s.name).join(", ")}: no '@venue:' line, so not a .sub file.`, "error");
    else if (parsed.length > 1) toast(`Opened ${chosen.name}; ignored ${parsed.filter((s) => s !== chosen).map((s) => s.name).join(", ")}.`);
    if (chosen) {
      const sameVenue = state.venue && state.venue.slug === chosen.venue;
      openSubFile(chosen.text);
      // A new venue applies the matches once its form is built (openVenue).
      if (sameVenue) { refreshFileWidgets(); applyPendingMatches(); }
      return;
    }
    if (state.venue) { refreshFileWidgets(); applyPendingMatches(); }
    else if (res) toast(`Uploaded ${plural(res.saved.length, "file")}. Choose a venue and they go into its form.`);
  }

  async function applyPendingMatches() {
    const paths = state.pendingPaths;
    if (!paths || !paths.length || !state.venue) return;
    state.pendingPaths = null;
    let res;
    try {
      res = await postJSON(`/api/venues/${encodeURIComponent(state.venue.slug)}/match`, { paths });
    } catch (err) { toast(err.message, "error"); return; }
    const vals = values();
    let text = state.text;
    const placed = [];
    const kept = [];
    for (const [fid, value] of Object.entries(res.assignments)) {
      const field = state.fields.get(fid);
      if (!field) continue;
      if ((vals[fid] || "").trim()) { kept.push(field); continue; }
      text = Subfile.setField(text, field, value);
      placed.push(field);
    }
    setText(text);
    syncWidgets();
    refreshFileWidgets();
    renderImportResult(res, new Set(placed.map((f) => f.id)), kept);
    if (placed.length) toast(`Placed files in ${plural(placed.length, "field")}. Check them below.`);
  }

  function renderImportResult(res, placedIds, kept) {
    const out = $("#import-result");
    const byField = new Map();
    for (const m of res.matched) {
      if (!placedIds.has(m.field)) continue;
      if (!byField.has(m.field)) byField.set(m.field, { label: m.label, names: [] });
      byField.get(m.field).names.push(m.name);
    }
    const notPlaced = res.unmatched.concat(res.matched.filter((m) => !placedIds.has(m.field)).map((m) => ({ name: m.name, reason: `${m.label} was already filled in` })));
    const skipped = state.importSkipped || [];
    state.importSkipped = null;
    out.replaceChildren(...[
      el("strong", { text: byField.size ? "Your files, placed by their names:" : "No file could be placed by its name." }),
      byField.size ? el("ul", {}, [...byField.entries()].map(([fid, g]) =>
        el("li", {}, el("button", { type: "button", class: "link", onclick: () => focusField(fid), text: g.label }), ": ", el("span", { class: "mono", text: g.names.join(", ") })))) : null,
      kept.length ? el("p", { class: "hint", text: `Left as they were (already filled in): ${kept.map((f) => f.label).join(", ")}.` }) : null,
      notPlaced.length || skipped.length ? el("details", {},
        el("summary", { text: `${plural(notPlaced.length + skipped.length, "file")} not placed` }),
        el("ul", {},
          notPlaced.map((m) => el("li", {}, el("span", { class: "mono", text: m.name }), ` — ${m.reason}`)),
          skipped.map((m) => el("li", {}, el("span", { class: "mono", text: m.name }), ` — not unpacked: ${m.reason}`))),
        el("p", { class: "hint", text: "They are uploaded; pick them in the right field below." })) : null,
      el("button", { type: "button", class: "link small", onclick: () => (out.hidden = true), text: "Dismiss" }),
    ].filter(Boolean));
    out.hidden = false;
  }

  function wireImport() {
    const input = $("#import-input");
    input.addEventListener("change", () => { const files = Array.from(input.files); input.value = ""; importFiles(files); });
    // Drop a .sub or .zip anywhere but on an upload box.
    const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes("Files");
    window.addEventListener("dragover", (e) => { if (hasFiles(e) && !e.target.closest?.(".upload-box")) { e.preventDefault(); document.body.classList.add("drop-import"); } });
    window.addEventListener("dragleave", (e) => { if (!e.relatedTarget) document.body.classList.remove("drop-import"); });
    window.addEventListener("drop", (e) => {
      document.body.classList.remove("drop-import");
      if (!hasFiles(e) || e.target.closest?.(".upload-box")) return;
      e.preventDefault();
      importFiles(e.dataTransfer.files);
    });
  }

  // ------------------------------------------------------------- the text

  // Every change goes through here.
  function setText(next, { fromEditor = false } = {}) {
    if (next === state.text) return;
    const before = Subfile.parse(state.text).values;
    state.text = next;
    state.touched = true;
    if (state.results) state.resultsStale = true;
    if (state.results && fileRemoved(before, Subfile.parse(next).values)) state.filesChanged = true;
    if (state.local) saveLocal();
    else if (state.shared) schedulePush();
    else store.set(`draft:${state.venue.slug}`, next);
    if (!fromEditor) $("#sub-text").value = next;
    else syncWidgets();
    const after = Subfile.parse(next).values;
    // The manuscript box and file pickers follow the text too.
    if (state.venue.manuscript_field && before[state.venue.manuscript_field] !== after[state.venue.manuscript_field]) renderManuscript();
    // arXiv's category dropdown lists only the chosen archive's categories.
    if (before.primary_archive !== after.primary_archive) state.widgets.get("primary_category")?.set(after.primary_category ?? "");
    runLive();
    renderChecks();
    schedulePlan();
  }

  // The paths a file or filelist value names (a filelist line is 'path | type | ...').
  const filePaths = (value) => String(value ?? "").split("\n").map((l) => l.split("|")[0].trim()).filter(Boolean);
  const fileFields = () => (state.venue ? state.venue.fields.filter((f) => f.type === "file" || f.type === "filelist") : []);

  // Did a file field lose a path (removed, or replaced by another file)?
  function fileRemoved(before, after) {
    return fileFields().some((f) => {
      const kept = new Set(filePaths(after[f.id]));
      return filePaths(before[f.id]).some((p) => !kept.has(p));
    });
  }

  // An upload under a name the form already uses replaces that file's
  // contents without changing the text, so setText never sees it.
  function noteReplacedUploads(saved) {
    if (!state.results || !saved.length) return;
    const vals = values();
    const used = new Set(fileFields().flatMap((f) => filePaths(vals[f.id])));
    if (!saved.some((n) => used.has(n))) return;
    state.resultsStale = true;
    state.filesChanged = true;
    renderFieldIssues();
    renderChecks();
  }

  // Write the text back to the local .sub file (--web), a moment after the last edit.
  const saveLocal = debounce(async () => {
    const text = state.text;
    try {
      await api("/api/local", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }), keepalive: true });
      $("#local-status").textContent = `Saved ${new Date().toLocaleTimeString()}.`;
      $("#local-status").className = "small muted";
    } catch (err) {
      $("#local-status").textContent = `Not saved: ${err.message}`;
      $("#local-status").className = "small err";
    }
  }, 400);

  function onWidgetChange(field, value) {
    state.review.delete(field.id);
    state.widgets.get(field.id)?.card?.classList.remove("review");
    setText(Subfile.setField(state.text, field, value));
  }

  // Push the text's values into every widget the author isn't typing in.
  function syncWidgets() {
    const vals = values();
    for (const [id, w] of state.widgets) {
      if (w.card && w.card.contains(document.activeElement) && document.activeElement.matches("input[type=text], input:not([type]), textarea")) continue;
      w.set(vals[id] ?? "");
    }
  }

  function refreshFileWidgets() {
    for (const w of state.widgets.values()) w.refresh?.();
    syncWidgets();
    renderManuscript();
  }

  // ------------------------------------------------------------ manuscript

  // The autofill button under the upload boxes: it needs the manuscript.
  function renderManuscript() {
    const fid = state.venue.manuscript_field;
    const current = fid ? (values()[fid] || "").trim() : "";
    const file = current ? fileInfo(current) : null;
    renderStatus();
    const af = $("#autofill-btn");
    const hint = $("#autofill-hint");
    const { label: ai, reason } = aiAvailability();
    af.disabled = !current || !file || !ai || state.busy;
    setAiTooltip($("#autofill-wrap"), reason);
    hint.textContent = !ai
      ? reason
      : !current
        ? "Choose the manuscript first."
        : !file
          ? "The manuscript file was not found. Choose it again."
          : `Reads your uploads (${ai}) and fills the title, abstract, authors, keywords, statements, and the rest. You review every value.`;
  }

  function wireManuscript() {
    $("#autofill-btn").addEventListener("click", runAutofill);
  }

  async function runAutofill() {
    const btn = $("#autofill-btn");
    const out = $("#autofill-result");
    state.busy = true;
    btn.disabled = true;
    btn.replaceChildren(el("span", { class: "spinner" }), " Reading your files…");
    try {
      const res = await postJSON(`/api/workspaces/${state.ws}/autofill`, { text: state.text }, llmHeaders());
      setText(res.text);
      syncWidgets();
      state.review = new Set(res.needs_review.map((o) => o.id));
      for (const [id, w] of state.widgets) w.card?.classList.toggle("review", state.review.has(id));
      renderStatus();
      out.hidden = false;
      const reviewList = res.needs_review.length
        ? el("ul", {}, res.needs_review.map((o) => el("li", {}, el("button", { type: "button", class: "link", onclick: () => focusField(o.id), text: o.label }), o.note ? ` — ${o.note}` : "")))
        : null;
      const left = res.left_for_the_author.length
        ? el("p", { class: "hint", text: `Left for you: ${res.left_for_the_author.map((u) => state.fields.get(u.id)?.label || u.id).join(", ")}.` })
        : null;
      out.replaceChildren(
        el("p", { text: `Filled ${plural(res.filled.length + res.needs_review.length, "field")}. ${res.needs_review.length ? `Please check the ${plural(res.needs_review.length, "one")} highlighted below:` : "Please read them over."}` }),
        reviewList,
        left,
      );
      toast("Autofill done. Review the highlighted fields.");
    } catch (err) {
      toast(`Autofill failed: ${err.message}`, "error");
    } finally {
      state.busy = false;
      btn.replaceChildren(el("span", { "aria-hidden": "true", text: "✨" }), " Fill in the rest from my file uploads");
      renderManuscript();
    }
  }

  // ------------------------------------------------------------ requirements

  const REQ_SECTIONS = [
    ["manuscript", "Manuscript"], ["title_page", "Title page"], ["abstract", "Abstract"], ["keywords", "Keywords"],
    ["sections", "Sections"], ["statements", "Statements and declarations"], ["figures", "Figures"], ["tables", "Tables"],
    ["supplementary", "Supplementary material"], ["references", "References"], ["cover_letter", "Cover letter"], ["upload", "Upload"],
  ];
  const REQ_SKIP = new Set(["notes", "template_layout", "main_text_end_headings"]);

  function reqValue(v) {
    if (Array.isArray(v)) return v.map((x) => (typeof x === "string" ? x.replace(/_/g, " ") : JSON.stringify(x))).join(", ");
    if (v === true) return "yes";
    if (v === false) return "no";
    if (v && typeof v === "object") return Object.entries(v).map(([k, x]) => `${humanize(k)}: ${reqValue(x)}`).join("; ");
    return String(v);
  }

  function renderRequirements() {
    const reqs = state.requirements;
    const body = $("#req-body");
    const panel = $("#req-panel");
    panel.hidden = !reqs;
    if (!reqs) return;
    body.replaceChildren();
    let ruleCount = 0;
    const general = reqs.notes || [];
    if (general.length) {
      ruleCount += general.length;
      body.append(el("div", { class: "req-section" }, el("h4", { text: "General" }), el("ul", {}, general.map((n) => el("li", { text: n })))));
    }
    for (const [key, title] of REQ_SECTIONS) {
      const section = reqs[key];
      if (!section || typeof section !== "object") continue;
      const entries = Object.entries(section).filter(([k, v]) => !REQ_SKIP.has(k) && v != null && !(Array.isArray(v) && !v.length));
      const notes = section.notes || [];
      if (!entries.length && !notes.length) continue;
      ruleCount += entries.length + notes.length;
      body.append(
        el("div", { class: "req-section" },
          el("h4", { text: title }),
          entries.length ? el("dl", {}, entries.flatMap(([k, v]) => [el("dt", { text: humanize(k) }), el("dd", { text: reqValue(v) })])) : null,
          notes.length ? el("ul", {}, notes.map((n) => el("li", { text: n }))) : null,
        ),
      );
    }
    const typeField = reqs.article_type_field || state.venue.manuscript_requirements?.article_type_field;
    const types = state.venue.manuscript_requirements?.article_types;
    if (typeField && types && Object.keys(types).length) {
      const chosen = values()[typeField];
      body.prepend(el("p", { class: "hint", text: chosen ? `Showing the rules for "${chosen}".` : `Some limits depend on the article type; choose it in "${state.fields.get(typeField)?.label || typeField}" to see them.` }));
    }
    const sources = reqs.source_urls || [];
    if (sources.length) {
      body.append(el("p", { class: "hint" }, `Taken from the venue's guidelines${reqs.retrieved ? ` (read ${reqs.retrieved})` : ""}: `, ...sources.flatMap((u, i) => [i ? ", " : "", el("a", { href: u, target: "_blank", rel: "noopener", text: new URL(u).pathname.replace(/\/$/, "").split("/").pop() || new URL(u).host })])));
    }
    $("#req-summary").textContent = plural(ruleCount, "rule");
  }

  // ------------------------------------------------------------------ form

  const GROUPS = [
    ["paper", "Paper Metadata", "Copied from the manuscript."],
    ["authors", "Authors", ""],
    ["reviewers", "Reviewers", "People you suggest as reviewers and people you ask the editor to exclude. paperpush never fills these in for you."],
    ["decisions", "Disclosures", "Policies and confirmations only you can answer. paperpush never fills these in for you."],
  ];

  // Reviewer fields go in their own group. A venue that takes suggested and
  // excluded reviewers in one field (a stance column) shows it as two tables.
  const isReviewerField = (f) => /reviewer|referee/i.test(f.id);
  const stanceIndex = (f) => (f.fields || []).findIndex((c) => c.replace(/\?$/, "") === "stance");
  // The page says "reviewers", never "referees" (the portal's own "Add
  // Referee" button keeps its name), and names a combined field for both tables.
  function renameReviewerField(f) {
    if (!isReviewerField(f)) return;
    if (stanceIndex(f) >= 0) f.label = "Suggested and excluded reviewers";
    const swap = (t) => t.replace(/(?<!Add )\b(r)eferee/gi, (_, r) => (r === "R" ? "R" : "r") + "eviewer");
    f.label = swap(f.label);
    if (f.help) f.help = swap(f.help);
  }

  function groupOf(f) {
    if (f.type === "authorlist") return "authors";
    if (f.type === "file" || f.type === "filelist") return "files";
    if (isReviewerField(f)) return "reviewers";
    if (f.role === "never") return "decisions";
    return "paper";
  }

  // The upload fields, the manuscript first, in the File uploads panel.
  function uploadFields() {
    const mf = state.venue.manuscript_field;
    const files = state.venue.fields.filter((f) => f.type === "file" || f.type === "filelist");
    return [...files.filter((f) => f.id === mf), ...files.filter((f) => f.id !== mf)];
  }

  function buildForm() {
    const root = $("#field-groups");
    root.replaceChildren();
    state.widgets.clear();
    const uploads = uploadFields();
    $("#upload-fields").replaceChildren(...uploads.map(fieldCard));
    const fields = state.venue.fields.filter((f) => !uploads.includes(f));
    for (const [key, title, hint] of GROUPS) {
      const group = fields.filter((f) => groupOf(f) === key);
      if (!group.length) continue;
      root.append(el("section", { class: "field-group", id: `group-${key}`, "data-group": key },
        el("div", { class: "group-head" }, el("h2", { text: title }), el("span", { class: "count muted small", "data-count": key })),
        hint ? el("p", { class: "hint group-hint", text: hint }) : null,
        group.map(fieldCard)));
    }
    buildNav(fields, uploads);
  }

  // The side menu: the manuscript, the guidelines, every field by group
  // (each with a status dot), then the checks. viewer.js highlights the
  // entry on screen and scrolls to the one clicked.
  function buildNav(fields, uploads = []) {
    const v = state.venue;
    $("#nav-venue").textContent = v.name;
    $("#nav-file").textContent = v.subfile_name;
    const item = (target, label, { required = false, key = "", cls = "" } = {}) =>
      el("a", { class: `toc-item ${cls}`, href: `#${target}`, "data-target": target, "data-key": key || null },
        el("span", { class: "dot", "aria-hidden": "true" }),
        el("span", { class: "toc-label", text: label }),
        required ? el("span", { class: "req", title: "Required", text: "*" }) : null);
    const top = [
      ...(uploads.length ? [el("div", { class: "toc-group", "data-group": "uploads", text: "File uploads" })] : []),
      ...uploads.map((f) => item(`card-${f.id}`, f.label, { required: f.required, key: f.id, cls: "st-empty" })),
      item("req-panel", "Author guidelines", { key: "guidelines", cls: "st-info" }),
    ];
    const groups = [];
    for (const [key, title] of GROUPS) {
      const group = fields.filter((f) => groupOf(f) === key);
      if (!group.length) continue;
      groups.push(el("div", { class: "toc-group", "data-group": key, text: title }));
      for (const f of group) groups.push(item(`card-${f.id}`, f.label, { required: f.required, key: f.id, cls: "st-empty" }));
    }
    $("#toc").replaceChildren(
      ...top,
      ...groups,
      el("div", { class: "toc-group", text: "Before you submit" }),
      item("checks-panel", "Checks", { key: "checks", cls: "st-pending" }),
    );
    $("#sidenav").hidden = false;
    $("#layout").classList.add("with-nav");
    window.PaperpushViewer?.refresh();
  }

  function limitsText(f) {
    const bits = [];
    const g = state.liveRules[f.id] || {};
    if (f.word_count) bits.push(`at most ${f.word_count} words`);
    else if (g.max_words) bits.push(`at most ${g.max_words} words (guidelines)`);
    if (g.min_words) bits.push(`at least ${g.min_words} words (guidelines)`);
    if (f.character_count) bits.push(`at most ${f.character_count} characters`);
    else if (g.max_characters) bits.push(`at most ${g.max_characters} characters (guidelines)`);
    if (f.min_character_count) bits.push(`at least ${f.min_character_count} characters`);
    if (f.min_count != null || f.max_count != null) {
      const what = f.type === "authorlist" ? "authors" : f.type === "filelist" ? "files" : "items";
      bits.push(f.max_count != null ? (f.min_count ? `${f.min_count}–${f.max_count} ${what}` : `at most ${f.max_count} ${what}`) : `at least ${f.min_count} ${what}`);
    } else if (g.min_keywords != null || g.max_keywords != null) {
      bits.push(g.max_keywords != null ? (g.min_keywords ? `${g.min_keywords}–${g.max_keywords} keywords (guidelines)` : `at most ${g.max_keywords} keywords (guidelines)`) : `at least ${g.min_keywords} keywords (guidelines)`);
    }
    if (g.structured_headings) bits.push(`structured: ${g.structured_headings.join(", ")}`);
    if (f.require_url) bits.push("http(s) links");
    if (f.accept) bits.push(f.accept.join(" "));
    if (f.max_file_size_mb) bits.push(`up to ${f.max_file_size_mb} MB each`);
    if (f.min_value != null || f.max_value != null) bits.push(`${f.min_value ?? ""}–${f.max_value ?? ""}`);
    return bits.join(" · ");
  }

  const TYPE_BADGES = { text: "text", textarea: "long text", choice: "choice", multichoice: "multiple choice", boolean: "yes / no", file: "file", filelist: "files", authorlist: "authors", int: "number" };

  function fieldCard(f) {
    const id = `fld-${f.id}`;
    const widget = makeWidget(f, id);
    const card = el(
      "section",
      { class: "field card st-empty", id: `card-${f.id}`, "data-field": f.id },
      el("header", { class: "card-head" },
        el("div", { class: "titles" },
          el("label", { class: "field-label", for: id, text: f.label }),
          el("code", { class: "key", text: f.id })),
        el("div", { class: "badges" },
          el("span", { class: "badge type", text: TYPE_BADGES[f.type] || f.type }),
          f.required ? el("span", { class: "badge req", text: "required" })
            : f.required_if ? el("span", { class: "badge cond", text: `required if ${state.fields.get(f.required_if)?.label || f.required_if}` })
              : el("span", { class: "badge opt", text: "optional" }),
          el("span", { class: "badge review-badge", text: "check this", title: "Autofill filled this in; please read it over" }),
          el("button", { type: "button", class: "copy", title: "Copy the value", text: "Copy" })),
      ),
      f.help ? helpBlock(f.help) : null,
      widget.node,
      el("ul", { class: "field-issues", "aria-live": "polite" }),
      el("footer", { class: "card-foot" }, widget.counter ? widget.counter.node : null, el("span", { class: "limits" })),
    );
    widget.card = card;
    widget.field = f;
    state.widgets.set(f.id, widget);
    updateLimits(f.id);
    return card;
  }

  function updateLimits(fid) {
    const w = state.widgets.get(fid);
    if (!w) return;
    const text = limitsText(w.field);
    $(".limits", w.card).replaceChildren(...(text ? text.split(" · ") : []).map((t) => el("span", { class: "fnote lim", text: t })));
  }

  function helpBlock(text) {
    if (text.length <= 220) return el("p", { class: "hint desc", text });
    const short = el("p", { class: "hint desc" }, text.slice(0, 200).replace(/\s+\S*$/, "") + "… ");
    const more = el("button", { type: "button", class: "link small", text: "more" });
    more.addEventListener("click", () => short.replaceWith(el("p", { class: "hint desc", text })));
    short.append(more);
    return short;
  }

  function makeWidget(f, id) {
    if (f.hierarchical) return hierarchicalWidget(f, id);
    if (isReviewerField(f) && stanceIndex(f) >= 0) return stanceWidget(f, id);
    if (f.fields && f.fields.length > 1 && (f.type === "authorlist" || f.type === "textarea")) return tableWidget(f, id);
    switch (f.type) {
      case "boolean": return booleanWidget(f, id);
      case "choice": return f.options && f.options.length ? choiceWidget(f, id) : textWidget(f, id);
      case "multichoice":
        if (!f.options || !f.options.length) return tagWidget(f, id);
        return isCoded(f) ? codeMultiWidget(f, id) : multiWidget(f, id);
      case "file": return fileWidget(f, id);
      case "filelist": return fileListWidget(f, id);
      case "int": return textWidget(f, id, "number");
      case "text":
        if (f.options && f.options.length) return codeChoiceWidget(f, id);
        return /keyword/i.test(f.id) ? tagWidget(f, id) : textWidget(f, id);
      case "textarea":
      case "authorlist": return textareaWidget(f, id);
      default: return textWidget(f, id);
    }
  }

  // A running count against the field's own limit, or the guidelines'.
  function counterFor(f) {
    const node = el("span", { class: "counter fnote" });
    const update = (value) => {
      const g = state.liveRules[f.id] || {};
      const maxWords = f.word_count || g.max_words;
      const maxChars = f.character_count || g.max_characters;
      const maxKeywords = g.max_keywords;
      const parts = [];
      let over = false;
      if (maxWords) { const w = LiveChecks.words(value); parts.push(`${w}/${maxWords} words`); over ||= w > maxWords; }
      if (maxChars) { const c = value.trim().length; parts.push(`${c}/${maxChars} characters`); over ||= c > maxChars; }
      if (maxKeywords) { const k = value.split(/[,;\n]/).filter((s) => s.trim()).length; parts.push(`${k}/${maxKeywords} keywords`); over ||= k > maxKeywords; }
      node.textContent = parts.join(" · ");
      node.hidden = !parts.length;
      node.classList.toggle("over", over);
    };
    return { node, update };
  }

  function textWidget(f, id, type = "text") {
    const input = el("input", { id, type, class: "input" });
    if (f.type === "int") {
      if (f.min_value != null) input.min = f.min_value;
      if (f.max_value != null) input.max = f.max_value;
    }
    const counter = counterFor(f);
    input.addEventListener("input", () => { counter.update(input.value); onWidgetChange(f, input.value.replace(/\n/g, " ")); });
    input.addEventListener("blur", () => markTouched(f.id));
    return { node: input, counter, set(v) { input.value = v; counter.update(v); } };
  }

  function autosize(ta) {
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight + 2, 480)}px`;
  }

  function textareaWidget(f, id) {
    const ta = el("textarea", { id, class: "input", rows: 3 });
    const counter = counterFor(f);
    ta.addEventListener("input", () => { autosize(ta); counter.update(ta.value); onWidgetChange(f, ta.value); });
    ta.addEventListener("blur", () => markTouched(f.id));
    return { node: ta, counter, set(v) { ta.value = v; counter.update(v); requestAnimationFrame(() => autosize(ta)); } };
  }

  function booleanWidget(f, id) {
    if (f.confirm) {
      const box = el("input", { id, type: "checkbox" });
      box.addEventListener("change", () => onWidgetChange(f, box.checked ? "yes" : "no"));
      return { node: el("label", { class: "confirm" }, box, el("span", { text: "Yes, I confirm" })), set(v) { box.checked = /^(yes|y|true|1|on)$/i.test(v.trim()); } };
    }
    const name = `${id}-radio`;
    const opts = [["yes", "Yes"], ["no", "No"]].map(([value, label]) => {
      const r = el("input", { type: "radio", name, value, id: value === "yes" ? id : null });
      r.addEventListener("change", () => onWidgetChange(f, value));
      return [r, el("label", {}, r, el("span", { text: label }))];
    });
    const clear = el("button", { type: "button", class: "link small", text: "clear" });
    clear.addEventListener("click", () => { opts.forEach(([r]) => (r.checked = false)); onWidgetChange(f, ""); });
    return {
      node: el("div", { class: "yesno", role: "radiogroup" }, opts.map(([, l]) => l), clear),
      set(v) {
        const t = v.trim().toLowerCase();
        const val = /^(yes|y|true|1|on)$/.test(t) ? "yes" : /^(no|n|false|0|off)$/.test(t) ? "no" : "";
        opts.forEach(([r]) => (r.checked = r.value === val));
      },
    };
  }

  // A text field backed by a vocabulary of "code (Label)" entries (arXiv's
  // primary_category): a dropdown that shows the label but stores the bare code,
  // grouped by archive and narrowed to the chosen primary_archive when set.
  const CODED = /^(\S+) \((.+)\)$/;
  const isCoded = (f) => f.options.every((o) => CODED.test(o.trim()));
  const codeOf = (v) => { const m = v.trim().match(CODED); return m ? m[1] : v.trim(); };
  function codedEntries(f) {
    return f.options.map((o) => {
      const code = codeOf(o);
      return { code, text: o, group: code.split(".")[0] };
    });
  }
  function groupedOptions(entries) {
    const groups = new Map();
    entries.forEach((e) => { if (!groups.has(e.group)) groups.set(e.group, []); groups.get(e.group).push(e); });
    return [...groups].map(([g, list]) => el("optgroup", { label: g }, list.map((e) => el("option", { value: e.code, text: e.text }))));
  }

  function codeChoiceWidget(f, id) {
    const entries = codedEntries(f);
    const select = el("select", { id, class: "input" });
    const fill = (current) => {
      const archive = (values().primary_archive || "").trim();
      const shown = archive && entries.some((e) => e.group === archive) ? entries.filter((e) => e.group === archive) : entries;
      select.replaceChildren(el("option", { value: "", text: "Choose…" }));
      if (current && !entries.some((e) => e.code === current)) select.append(el("option", { value: current, text: `${current} (not an option)` }));
      else if (current && !shown.some((e) => e.code === current)) select.append(el("option", { value: current, text: `${current} (not in ${archive})` }));
      select.append(...groupedOptions(shown));
      select.value = current;
    };
    select.addEventListener("change", () => onWidgetChange(f, select.value));
    select.addEventListener("blur", () => markTouched(f.id));
    fill("");
    return { node: select, set(v) { fill(v.trim()); } };
  }

  // A multichoice over "code (Label)" options (arXiv's crosslist_categories):
  // pick from a grouped dropdown to add a chip; the .sub stores the bare codes.
  // arXiv pairs each cross-list category with the archive at the same position
  // in crosslist_archives, so that field is rewritten to match (each code's
  // prefix: "q-bio.GN" -> "q-bio").
  function codeMultiWidget(f, id) {
    const entries = codedEntries(f);
    const byCode = new Map(entries.map((e) => [e.code, e]));
    const pairedId = f.id === "crosslist_categories" ? "crosslist_archives" : null;
    let selected = [];
    const chips = el("div", { class: "chips" });
    const select = el("select", { id, class: "input" });
    const counter = counterFor(f);
    const emit = () => {
      const v = selected.join(", ");
      counter.update(v);
      const paired = pairedId && state.venue.fields.find((x) => x.id === pairedId);
      if (paired) {
        state.review.delete(f.id);
        state.widgets.get(f.id)?.card?.classList.remove("review");
        const archives = selected.map((c) => c.split(".")[0]).join(", ");
        setText(Subfile.setField(Subfile.setField(state.text, f, v), paired, archives));
        state.widgets.get(pairedId)?.set(archives);
      } else onWidgetChange(f, v);
    };
    const render = () => {
      chips.replaceChildren(...selected.map((c) => {
        const known = byCode.has(c) || f.options.includes(c);
        return el("span", { class: `chip ${known ? "" : "warn"}`, title: byCode.get(c)?.text || "not an option" }, byCode.get(c)?.text || c,
          el("button", { type: "button", "aria-label": `Remove ${c}`, onclick: () => { selected = selected.filter((x) => x !== c); render(); emit(); } }, "×"));
      }));
      select.replaceChildren(el("option", { value: "", text: selected.length ? "Add another category…" : "Add a category…" }),
        ...groupedOptions(entries.filter((e) => !selected.includes(e.code))));
      select.value = "";
    };
    select.addEventListener("change", () => {
      if (!select.value) return;
      selected = [...selected, select.value];
      render();
      emit();
    });
    select.addEventListener("blur", () => markTouched(f.id));
    render();
    return {
      node: el("div", { class: "multi" }, chips, select),
      counter,
      set(v) { selected = [...new Set(v.split(",").map(codeOf).filter(Boolean))]; counter.update(v); render(); },
    };
  }

  function choiceWidget(f, id) {
    if (f.options_recommended) {
      const listId = `${id}-list`;
      const input = el("input", { id, class: "input", list: listId, placeholder: "Pick from the list or type your own" });
      const dl = el("datalist", { id: listId }, f.options.map((o) => el("option", { value: o })));
      input.addEventListener("input", () => onWidgetChange(f, input.value));
      return { node: el("div", {}, input, dl), set(v) { input.value = v; } };
    }
    const select = el("select", { id, class: "input" });
    const fill = (current) => {
      select.replaceChildren(el("option", { value: "", text: "Choose…" }));
      if (current && !f.options.includes(current)) select.append(el("option", { value: current, text: `${current} (not an option)` }));
      f.options.forEach((o) => select.append(el("option", { value: o, text: o })));
      select.value = current;
    };
    select.addEventListener("change", () => {
      onWidgetChange(f, select.value);
      // The article type picks which guideline limits apply.
      if (f.id === state.venue.manuscript_requirements?.article_type_field) refreshPlan();
    });
    fill("");
    return { node: select, set(v) { fill(v.trim()); } };
  }

  function multiWidget(f, id) {
    let selected = [];
    const filter = el("input", { type: "search", class: "input", placeholder: `Filter ${f.options.length} options`, "aria-label": "Filter options" });
    const chips = el("div", { class: "chips" });
    const list = el("div", { class: "checklist", id });
    const counter = counterFor(f);
    const emit = () => { counter.update(selected.join(", ")); onWidgetChange(f, selected.join(", ")); };
    const renderChips = () => {
      chips.replaceChildren(...selected.map((s) =>
        el("span", { class: `chip ${f.options.includes(s) ? "" : "warn"}` }, s,
          el("button", { type: "button", "aria-label": `Remove ${s}`, onclick: () => { selected = selected.filter((x) => x !== s); renderList(); renderChips(); emit(); } }, "×"))));
    };
    const renderList = () => {
      const q = filter.value.trim().toLowerCase();
      list.replaceChildren(...f.options.filter((o) => !q || o.toLowerCase().includes(q)).slice(0, 400).map((o) => {
        const box = el("input", { type: "checkbox", checked: selected.includes(o) });
        box.addEventListener("change", () => {
          selected = box.checked ? [...selected, o] : selected.filter((x) => x !== o);
          renderChips();
          emit();
        });
        return el("label", {}, box, el("span", { text: o }));
      }));
    };
    filter.addEventListener("input", renderList);
    const extras = [];
    if (f.options_recommended) {
      const custom = el("input", { class: "input", placeholder: "Add your own and press Enter", style: "margin-top:6px" });
      custom.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        const added = splitItems(custom.value).filter((v) => !selected.includes(v));
        if (added.length) { selected = [...selected, ...new Set(added)]; renderChips(); renderList(); emit(); }
        custom.value = "";
      });
      extras.push(custom);
    }
    return {
      node: el("div", { class: "multi" }, chips, f.options.length > 8 ? filter : null, list, ...extras),
      counter,
      set(v) { selected = v.split(",").map((s) => s.trim()).filter(Boolean); counter.update(v); renderChips(); renderList(); },
    };
  }

  // Typed-in entries split on commas and semicolons, so a pasted list
  // ("a, b; c") becomes one entry each.
  function splitItems(text) {
    return text.split(/[,;\n]/).map((s) => s.trim()).filter(Boolean);
  }

  // A free list (keywords, categories): each entry is its own chip. Type and
  // press Enter (or leave the box) to add; several at once with commas or
  // semicolons. Backspace in the empty box takes the last one back to edit,
  // double-clicking a chip edits it. The .sub keeps the list joined with the
  // separator the venue's help asks for.
  function tagWidget(f, id) {
    const sep = /semicolon/i.test(f.help || "") ? ";" : ", ";
    let items = [];
    const input = el("input", { id, class: "tag-input", type: "text", autocomplete: "off", placeholder: "Type and press Enter" });
    const box = el("div", { class: "tags input" });
    const counter = counterFor(f);
    const emit = () => { const v = items.join(sep); counter.update(v); onWidgetChange(f, v); };
    const render = () => {
      box.replaceChildren(
        ...items.map((t, i) => {
          const chip = el("span", { class: "chip tag", title: "Double-click to edit" }, el("span", { text: t }),
            el("button", { type: "button", "aria-label": `Remove ${t}`, onclick: (e) => { e.stopPropagation(); items.splice(i, 1); render(); emit(); input.focus(); } }, "×"));
          chip.addEventListener("dblclick", () => { commit(); items.splice(i, 1); input.value = t; render(); emit(); input.focus(); });
          return chip;
        }),
        input,
      );
      input.placeholder = items.length ? "" : "Type and press Enter";
      input.size = Math.max(input.value.length + 1, items.length ? 4 : 20);
    };
    const commit = () => {
      const added = splitItems(input.value).filter((t) => !items.includes(t));
      input.value = "";
      if (!added.length) { render(); return false; }
      items = [...items, ...new Set(added)];
      render();
      emit();
      return true;
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      else if (e.key === "Backspace" && !input.value && items.length) {
        e.preventDefault();
        input.value = items.pop();
        render();
        emit();
      }
    });
    input.addEventListener("input", () => { input.size = Math.max(input.value.length + 1, items.length ? 4 : 20); });
    input.addEventListener("blur", () => { commit(); markTouched(f.id); });
    box.addEventListener("click", (e) => { if (e.target === box) input.focus(); });
    render();
    return {
      node: box,
      counter,
      set(v) { items = splitItems(v); counter.update(v); render(); },
    };
  }

  // Upload boxes. Click one to choose files (in a local session, paperpush
  // opens the Finder itself so the .sub gets the file's real path; on the
  // website the browser's picker uploads a copy), or drop files onto it.
  const acceptOf = (f) => f.accept || (f.id === state.venue.manuscript_field && state.venue.manuscript_formats?.length ? state.venue.manuscript_formats : null);
  const acceptOk = (f, name) => { const acc = acceptOf(f); return !acc || acc.some((ext) => name.toLowerCase().endsWith(ext.toLowerCase())); };
  // The listed file a path names. A local session lists files relative to
  // where paperpush runs, so an absolute path in the .sub matches by its tail.
  const fileInfo = (path) => {
    const p = path.trim();
    return state.files.find((x) => x.name === p || baseName(x.name) === baseName(p))
      || (state.local && p.startsWith("/") ? state.files.find((x) => !x.name.startsWith("/") && p.endsWith(`/${x.name}`)) : null)
      || null;
  };

  async function pickNative(f, multiple) {
    try {
      const res = await postJSON("/api/local/pick", { multiple, prompt: `Choose ${multiple ? "files" : "a file"} for “${f.label}”` });
      for (const file of res.files) if (!state.files.some((x) => x.name === file.name)) state.files.push(file);
      return res.files.map((x) => x.name);
    } catch (err) {
      toast(`Could not open the file picker: ${err.message}`, "error");
      return [];
    }
  }

  // A chosen file: its path, size, and anything wrong with it.
  function chosenFile(f, path, onRemove) {
    const info = fileInfo(path);
    return el("span", { class: "chosen" },
      el("span", { class: "mono path", text: path }),
      info ? el("span", { class: "muted small", text: formatSize(info.size) }) : el("span", { class: "chip warn", text: state.local ? "not found" : "not uploaded" }),
      acceptOk(f, path) ? null : el("span", { class: "chip warn", text: `${extOf(path) || "no extension"} is not accepted` }),
      onRemove ? el("button", { type: "button", class: "icon-btn", "aria-label": `Remove ${path}`, title: "Remove", onclick: (e) => { e.stopPropagation(); onRemove(); } }, "×") : null);
  }

  // `shown` is the chosen file's path (a single-file box shows it inside).
  function uploadBox(f, id, { multiple, shown, onPicked, onRemove }) {
    const acc = acceptOf(f);
    const input = el("input", { type: "file", hidden: true, multiple, accept: acc ? acc.join(",") : null });
    input.addEventListener("change", async () => {
      const files = Array.from(input.files);
      input.value = "";
      const saved = await uploadFiles(files);
      if (saved.length) onPicked(saved);
    });
    const prompt = el("span", { class: "prompt" },
      el("strong", { text: shown ? "Change" : multiple ? "Choose files" : "Choose a file" }), ` or drop ${multiple ? "them" : "it"} here`);
    const box = el("div", { class: `upload-box${shown ? " has-file" : ""}`, id, tabindex: "0", role: "button", "aria-label": `${shown ? "Change" : "Choose"} ${f.label}` },
      shown ? chosenFile(f, shown, onRemove) : null, prompt);
    const open = async () => {
      if (state.info?.native_picker && state.local) { const names = await pickNative(f, multiple); if (names.length) onPicked(names); }
      else input.click();
    };
    box.addEventListener("click", open);
    box.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    box.addEventListener("dragover", (e) => { e.preventDefault(); box.classList.add("drag"); });
    box.addEventListener("dragleave", () => box.classList.remove("drag"));
    box.addEventListener("drop", async (e) => {
      e.preventDefault();
      box.classList.remove("drag");
      const files = Array.from(e.dataTransfer.files).slice(0, multiple ? undefined : 1);
      const saved = await uploadFiles(files);
      if (saved.length) onPicked(saved);
    });
    return el("div", { class: "upload-wrap" }, box, input);
  }

  function fileWidget(f, id) {
    const wrap = el("div", { class: "file-pick" });
    let value = "";
    const render = () => {
      wrap.replaceChildren(uploadBox(f, id, {
        multiple: false,
        shown: value,
        onPicked: (names) => { value = names[0]; render(); onWidgetChange(f, value); },
        onRemove: () => { value = ""; render(); onWidgetChange(f, ""); },
      }));
    };
    render();
    return { node: wrap, set(v) { value = v.trim(); render(); }, refresh: render };
  }

  // Extra columns after 'path' in a filelist, read from its help text
  // ("One file per line as 'path | type | linktext'").
  function fileListColumns(f) {
    const m = (f.help || "").match(/'path((?:\s*\|\s*[A-Za-z_ ]+?)+)'/);
    if (!m) return [];
    return m[1].split("|").map((s) => s.trim()).filter(Boolean);
  }

  function fileListWidget(f, id) {
    const cols = fileListColumns(f);
    let rows = []; // [[path, col1, col2, ...]]
    const wrap = el("div", { class: "rows", id });
    const serialize = () =>
      rows.filter((r) => r[0]).map((r) => {
        const cells = [r[0], ...cols.map((_, i) => r[i + 1] || "")];
        while (cells.length > 1 && !cells[cells.length - 1]) cells.pop();
        return cells.join(" | ");
      }).join("\n");
    const emit = () => onWidgetChange(f, serialize());
    const render = () => {
      wrap.replaceChildren();
      rows.forEach((r, ri) => {
        const cells = cols.map((c, ci) => {
          let input;
          if (c === "type" && f.type_options) {
            input = el("select", { class: "input" }, el("option", { value: "", text: "type…" }), f.type_options.map((o) => el("option", { value: o, text: o })));
            input.value = r[ci + 1] || "";
            input.addEventListener("change", () => { r[ci + 1] = input.value; emit(); });
          } else {
            input = el("input", { class: "input", placeholder: c.replace(/_/g, " "), value: r[ci + 1] || "" });
            input.addEventListener("input", () => { r[ci + 1] = input.value.replace(/\|/g, "/"); emit(); });
          }
          return input;
        });
        wrap.append(el("div", { class: "row file-row" }, chosenFile(f, r[0], () => { rows.splice(ri, 1); render(); emit(); }), ...cells));
      });
      wrap.append(uploadBox(f, `${id}-add`, {
        multiple: true,
        shown: "",
        onPicked: (names) => { names.forEach((n) => { if (!rows.some((r) => r[0] === n)) rows.push([n]); }); render(); emit(); },
      }));
    };
    render();
    return {
      node: wrap,
      set(v) {
        rows = v.split("\n").filter((l) => l.trim()).map((l) => {
          const parts = l.split("|").map((s) => s.trim());
          return [baseName(parts[0]), ...parts.slice(1)];
        });
        render();
      },
      refresh: render,
    };
  }

  const BOOL_COLUMNS = new Set(["corresponding", "first_author", "contributed_equally", "submitter"]);

  // Authors, reviewers, funders: a table that reads like the saved report
  // (columns as wide as their longest word, long values wrapping). Every cell
  // is an always-visible text box that wraps the same way. Each cell is a textarea laid over an
  // invisible copy of its text, so the copy sets the column widths and row
  // heights and the box always matches them.
  // `opts.fields` overrides the columns, `opts.onChange` replaces saving the
  // value (the caller reads `rows()` instead), `opts.addLabel` names the button.
  function tableWidget(f, id, opts = {}) {
    const cols = (opts.fields || f.fields).map((c) => ({ name: c.replace(/\?$/, ""), optional: c.endsWith("?") }));
    let rows = [];
    const wrap = el("div", { class: "tedit", id });
    const filled = () => rows.filter((r) => r.some((c, i) => c && !(BOOL_COLUMNS.has(cols[i]?.name) && c === "no")))
      .map((r) => cols.map((_, i) => r[i] || ""));
    const serialize = () => filled().map((r) => r.join(" | ")).join("\n");
    const emit = () => (opts.onChange ? opts.onChange() : onWidgetChange(f, serialize()));
    const label = (c) => c.name.replace(/_/g, " ");
    const cell = (r, ri, c, ci) => {
      if (BOOL_COLUMNS.has(c.name)) {
        const on = /^yes$/i.test(r[ci] || "");
        const pill = el("span", { class: `pill ${on ? "yes" : "no"}`, text: on ? "yes" : "no" });
        const box = el("input", { type: "checkbox", "aria-label": `${label(c)}, row ${ri + 1}`, checked: on });
        box.addEventListener("change", () => {
          r[ci] = box.checked ? "yes" : c.optional ? "" : "no";
          pill.className = `pill ${box.checked ? "yes" : "no"}`;
          pill.textContent = box.checked ? "yes" : "no";
          emit();
        });
        return el("td", { class: "tbool" }, pill, box);
      }
      const value = r[ci] || "";
      const sizer = el("span", { class: "sizer", "aria-hidden": "true", text: value || label(c) });
      const ta = el("textarea", { rows: 1, spellcheck: c.name !== "email", "aria-label": `${label(c)}, row ${ri + 1}`, placeholder: label(c) });
      ta.value = value;
      ta.addEventListener("input", () => {
        const v = ta.value.replace(/\|/g, "/").replace(/\n/g, " ");
        if (v !== ta.value) ta.value = v;
        r[ci] = v;
        sizer.textContent = v || label(c);
        emit();
      });
      // A cell holds one line of the .sub; Enter moves down a row instead.
      ta.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        const next = wrap.querySelectorAll("tbody tr")[ri + (e.shiftKey ? -1 : 1)];
        next?.children[ci + 1]?.querySelector("textarea")?.focus();
      });
      return el("td", {}, el("div", { class: "tcell" }, sizer, ta));
    };
    const render = () => {
      const head = el("thead", {}, el("tr", {},
        el("th", { class: "num", text: "#" }),
        cols.map((c) => el("th", { text: label(c) + (c.optional ? "" : "\u00a0*") })),
        el("th", { class: "tdel" })));
      const body = el("tbody", {}, rows.map((r, ri) =>
        el("tr", {},
          el("td", { class: "num", text: String(ri + 1) }),
          cols.map((c, ci) => cell(r, ri, c, ci)),
          el("td", { class: "tdel" },
            el("button", { type: "button", class: "icon-btn", title: "Remove", "aria-label": `Remove row ${ri + 1}`, onclick: () => { rows.splice(ri, 1); render(); emit(); } }, "×")))));
      const add = el("button", { type: "button", class: "btn btn-small", onclick: () => {
        rows.push(cols.map((c) => (BOOL_COLUMNS.has(c.name) && !c.optional ? "no" : "")));
        render();
        wrap.querySelector("tbody tr:last-child textarea")?.focus();
      } }, opts.addLabel || (f.type === "authorlist" ? "+ Add author" : "+ Add row"));
      wrap.replaceChildren(el("div", { class: "tscroll" }, el("table", {}, head, body)), add);
    };
    // Mark the field touched once focus leaves the table.
    wrap.addEventListener("focusout", (e) => {
      if (e.relatedTarget && wrap.contains(e.relatedTarget)) return;
      setTimeout(() => { if (!wrap.contains(document.activeElement)) markTouched(f.id); }, 0);
    });
    render();
    return {
      node: wrap,
      rows: filled,
      set(v) {
        rows = v.split("\n").filter((l) => l.trim()).map((l) => {
          const parts = l.split("|").map((s) => s.trim());
          // Keep any columns beyond the known ones in the last cell so nothing is lost.
          if (parts.length > cols.length) parts.splice(cols.length - 1, parts.length, parts.slice(cols.length - 1).join(" | "));
          return parts;
        });
        render();
      },
    };
  }

  // One reviewers field with a stance column, shown as a "Suggested reviewers"
  // table and an "Excluded reviewers" table. Saved back as one field, each
  // line carrying the stance the venue expects ("opposed" for excluded).
  function stanceWidget(f, id) {
    const si = stanceIndex(f);
    const fields = f.fields.filter((_, i) => i !== si);
    const suggested = /\brecommended\b/i.test(f.help || "") ? "recommended" : "suggested";
    const withStance = (r, stance) => [...r.slice(0, si), stance, ...r.slice(si)];
    const emit = () => onWidgetChange(f, [
      ...sug.rows().map((r) => withStance(r, suggested)),
      ...exc.rows().map((r) => withStance(r, "opposed")),
    ].map((r) => r.join(" | ").replace(/(\s*\|\s*)+$/, "")).join("\n"));
    const sug = tableWidget(f, id, { fields, onChange: emit, addLabel: "+ Add suggested reviewer" });
    const exc = tableWidget(f, `${id}-excluded`, { fields, onChange: emit, addLabel: "+ Add excluded reviewer" });
    return {
      node: el("div", { class: "stance-tables" },
        el("h3", { class: "subhead", text: "Suggested reviewers" }), sug.node,
        el("h3", { class: "subhead", text: "Excluded reviewers" }), exc.node),
      set(v) {
        const lines = { sug: [], exc: [] };
        for (const l of v.split("\n").filter((x) => x.trim())) {
          const parts = l.split("|").map((x) => x.trim());
          const stance = parts[si] || "";
          const rest = [...parts.slice(0, si), ...parts.slice(si + 1)].join(" | ");
          (/^(opposed|excluded?)$/i.test(stance) ? lines.exc : lines.sug).push(rest);
        }
        sug.set(lines.sug.join("\n"));
        exc.set(lines.exc.join("\n"));
      },
    };
  }

  function hierarchicalWidget(f, id) {
    const ta = textareaWidget(f, id);
    const picker = el("div", { class: "cascade" });
    let path = [];
    const add = el("button", { type: "button", class: "btn btn-small", disabled: true, text: "Add category" });
    const render = async () => {
      picker.replaceChildren();
      for (let depth = 0; depth <= path.length; depth++) {
        const prefix = path.slice(0, depth);
        let res;
        try {
          const q = prefix.length ? `?path=${encodeURIComponent(prefix.join(" | "))}` : "";
          res = await getJSON(`/api/venues/${state.venue.slug}/fields/${f.id}/options${q}`);
        } catch { break; }
        if (!res.options || !res.options.length) break;
        const select = el("select", { class: "input" }, el("option", { value: "", text: depth ? "(stop here)" : "Browse categories…" }), res.options.map((o) => el("option", { value: o, text: o })));
        select.value = path[depth] || "";
        select.addEventListener("change", () => { path = [...prefix, ...(select.value ? [select.value] : [])]; render(); });
        picker.append(select);
      }
      add.disabled = path.length < 2;
      picker.append(add);
    };
    add.addEventListener("click", () => {
      const current = $(`#${CSS.escape(id)}`).value.trim();
      const next = (current ? current + "\n" : "") + path.join(" | ");
      ta.set(next);
      onWidgetChange(f, next);
      path = [];
      render();
    });
    render();
    return { node: el("div", {}, ta.node, picker), counter: ta.counter, set: ta.set };
  }

  // ---------------------------------------------------------------- checks

  const touchedFields = new Set();
  function markTouched(fid) {
    if (touchedFields.has(fid)) return;
    touchedFields.add(fid);
    renderFieldIssues();
  }

  function runLive() {
    const live = LiveChecks.run(state.venue.fields, values(), state.liveRules, { venue: state.venue.slug, serverIssues: state.plan?.schema_issues });
    state.live = state.initialSubmission ? asInitialSubmission(live) : live;
    renderFieldIssues();
  }

  // ------------------------------------------- initial submission (any format)

  // The venue's rule that the initial submission may be in any format, for the
  // selected article type ("" when it has none).
  function initialRule() {
    if (!state.venue) return "";
    return (state.plan ? state.plan.initial_submission_rule : state.venue.initial_submission_rule) || "";
  }

  // The live checks with every error turned into a warning, as
  // `paperpush validate --initial-submission` does on the server.
  function asInitialSubmission(live) {
    const soften = (i) => (i.level === "error" ? { ...i, level: "warning" } : i);
    const byField = {};
    for (const [fid, issues] of Object.entries(live.byField)) byField[fid] = issues.map(soften);
    const checks = live.checks.map((c) => ({ ...c, status: c.status === "failed" ? "warning" : c.status, issues: (c.issues || []).map(soften) }));
    return { ...live, checks, byField };
  }

  function renderInitialSubmission() {
    const box = $("#initial-submission");
    const wrap = $("#initial-wrap");
    const rule = initialRule();
    if (!rule && state.initialSubmission) setInitialSubmission(false);
    box.disabled = !rule;
    box.checked = state.initialSubmission;
    const message = rule
      ? `Show every error as a warning. ${rule}`
      : `${state.venue ? state.venue.name : "This venue"}'s guidelines do not let the initial submission be in any format.`;
    wrap.dataset.tooltip = message;
    wrap.setAttribute("aria-label", message);
  }

  function setInitialSubmission(on) {
    if (state.initialSubmission === on) return;
    state.initialSubmission = on;
    // Results from the server were validated the other way: ask for a re-run.
    if (state.results) state.resultsStale = true;
    runLive();
    renderChecks();
  }

  // Issues under each field: the live ones as they happen (an empty required
  // field only once the author has left it or pressed Validate), plus the
  // file checks from the last validation.
  function renderFieldIssues() {
    const server = {};
    if (state.results && !state.resultsStale) {
      for (const c of state.results.checks) {
        if (c.group === "live" || !["failed", "warning"].includes(c.status)) continue;
        for (const i of c.issues) if (i.field) (server[i.field] ||= []).push(i);
      }
    }
    for (const [fid, w] of state.widgets) {
      const live = (state.live.byField[fid] || []).filter((i) => i.check !== "form.required" || state.showRequired || touchedFields.has(fid));
      const all = [...live, ...(server[fid] || [])];
      const list = $(".field-issues", w.card);
      list.replaceChildren(...all.map((i) => el("li", { class: i.level === "error" ? "error" : "warning", text: i.message })));
      w.card.querySelectorAll(".input").forEach((n) => n.classList.toggle("invalid", live.some((i) => i.level === "error")));
      w.issues = all;
    }
    renderStatus();
  }

  // One status per field, shown as the card's coloured edge and the menu
  // dot: red for an error, orange for a warning, yellow for a value autofill
  // wants read over, green for filled, hollow grey for empty.
  const STATUS_CLASSES = ["st-ok", "st-warn", "st-issue", "st-review", "st-empty", "st-pending"];
  function fieldStatus(value, issues, review) {
    if (issues.some((i) => i.level === "error")) return "st-issue";
    if (issues.length) return "st-warn";
    if (!String(value || "").trim()) return "st-empty";
    return review ? "st-review" : "st-ok";
  }
  function setStatus(node, status) {
    if (!node) return;
    node.classList.remove(...STATUS_CLASSES);
    node.classList.add(status);
  }

  function renderStatus() {
    if (!state.venue || !$("#toc").children.length) return;
    const vals = values();
    let withIssues = 0;
    for (const [fid, w] of state.widgets) {
      const value = vals[fid] ?? "";
      const status = fieldStatus(value, w.issues || [], state.review.has(fid));
      if (status === "st-issue" || status === "st-warn") withIssues++;
      setStatus(w.card, status);
      setStatus($(`.toc-item[data-key="${CSS.escape(fid)}"]`), status);
      w.card.toggleAttribute("data-filled", Boolean(String(value).trim()));
      w.card.dataset.value = value;
      w.card.dataset.search = `${w.field.label} ${fid} ${value}`.toLowerCase();
      $(".copy", w.card).hidden = !String(value).trim();
    }
    // The totals.
    const all = state.venue.fields;
    const req = all.filter((f) => f.required);
    const reqFilled = req.filter((f) => String(vals[f.id] ?? "").trim()).length;
    $("#stat-fields").textContent = all.length;
    $("#stat-req").textContent = `${reqFilled}/${req.length}`;
    $("#stat-req-tile").className = `stat ${reqFilled === req.length ? "good" : "partial"}`;
    $("#stat-issues").textContent = withIssues;
    $("#stat-issues-tile").className = `stat ${withIssues ? "bad" : "good"}`;
    const pct = req.length ? Math.round((100 * reqFilled) / req.length) : 100;
    $("#nav-progress span").style.width = `${pct}%`;
    $("#nav-progress").classList.toggle("done", reqFilled === req.length);
    $("#nav-progress-label").textContent = `${reqFilled} of ${req.length} required fields filled`;
    for (const [key] of GROUPS) {
      const count = $(`[data-count="${key}"]`);
      if (!count) continue;
      const cards = $$(`#group-${key} .card[data-field]`);
      const filled = cards.filter((c) => c.hasAttribute("data-filled")).length;
      count.textContent = `${filled} of ${cards.length} filled`;
    }
    renderCheckStatus();
    window.PaperpushViewer?.apply();
  }

  // The checks' tally: the last stat tile, the menu entry, and the row of
  // tiles above the check groups.
  function renderCheckStatus() {
    const tile = $("#stat-checks-tile");
    const item = $(".toc-item[data-key=checks]");
    const r = state.results;
    const statsRow = $("#check-stats");
    if (!r) {
      $("#stat-checks").textContent = "–";
      $("#stat-checks-label").textContent = "checks passed (not run yet)";
      tile.className = "stat jump";
      setStatus(item, "st-pending");
      statsRow.hidden = true;
      return;
    }
    // Once a file is removed or replaced, the last validation's tally no
    // longer describes the files: show none until Validate runs again.
    const c = state.filesChanged ? { passed: 0, failed: 0, warning: 0, needs_ai: 0 } : r.counts;
    const ran = c.passed + c.failed + c.warning;
    $("#stat-checks").textContent = `${c.passed}/${ran}`;
    $("#stat-checks-label").textContent = state.resultsStale ? "checks passed (out of date)" : "checks passed";
    const cls = state.resultsStale ? "stale" : c.failed ? "bad" : c.warning ? "partial" : "good";
    tile.className = `stat jump ${cls}`;
    setStatus(item, state.resultsStale ? "st-pending" : c.failed ? "st-issue" : c.warning ? "st-warn" : "st-ok");
    const tiles = [
      ["good", c.passed, "passed"],
      [c.failed ? "bad" : "", c.failed, "failed"],
      [c.warning ? "partial" : "", c.warning, "warnings"],
      [c.needs_ai ? "ai" : "", c.needs_ai || 0, "need AI"],
    ];
    statsRow.replaceChildren(...tiles.map(([k, n, label]) => el("div", { class: `stat ${k}` }, el("b", { text: String(n) }), el("span", { text: label }))));
    statsRow.hidden = false;
    statsRow.classList.toggle("stale", state.resultsStale);
  }

  const BLOCKS = [
    ["live", "Checked as you type", "The form itself: required fields, options, counts, and the word, character, and keyword limits."],
    ["validate", "Checked by Validate", "These read your uploaded files: formats and sizes, page and word counts, sections, statements, figures, layout, links, references, and whether the form matches the manuscript."],
    ["ai", "Checked only with AI", "Judgements that need someone to read the files: lettering in raster figures, free-text guideline rules, whether every field says what the manuscript says. Run them with “Validate, extended with AI”."],
    ["manual", "Not checked by paperpush", "Rules in the guidelines paperpush has no check for yet. Check these yourself."],
  ];
  const MARKS = { passed: "✔", failed: "✖", warning: "▲", unchecked: "?", needs_ai: "?", not_implemented: "–", not_applicable: "·", pending: "○" };
  const STATUS_WORDS = { unchecked: "could not run", needs_ai: "needs AI", not_implemented: "check by hand" };

  // The checks of one block, each with the status to show (grey "pending"
  // until there is a result for it).
  function blockChecks(group) {
    if (group === "live") {
      return state.live.checks.map((c) => ({ ...c, group, applicable: c.status !== "not_applicable", status: !state.touched && c.status !== "not_applicable" ? "pending" : c.status }));
    }
    // Results that read files no longer describe the files in the form.
    const ignored = state.filesChanged && (group === "validate" || group === "ai");
    const results = new Map(ignored ? [] : (state.results?.checks || []).map((c) => [c.id, c]));
    const plan = state.plan ? state.plan.checks : [];
    const ids = new Set();
    const out = [];
    for (const p of plan) {
      if (p.group !== group) continue;
      ids.add(p.id);
      const r = results.get(p.id);
      if (r) out.push({ ...r, group });
      else out.push({ ...p, status: p.applicable ? (group === "manual" ? "not_implemented" : "pending") : "not_applicable" });
    }
    // Checks a validation added that the plan did not foresee (AI tasks queued once files were read).
    if (!ignored) for (const r of state.results?.checks || []) if (r.group === group && !ids.has(r.id)) out.push(r);
    return out;
  }

  function checkItem(c) {
    const status = c.status;
    const cls = { passed: "st-passed", failed: "st-failed", warning: "st-warning", unchecked: "st-unchecked", needs_ai: "st-needs-ai", not_applicable: "st-na" }[status] || "";
    const tag = c.source === "ai" ? el("span", { class: "tag", text: "AI" }) : null;
    const word = STATUS_WORDS[status] ? el("span", { class: "cat", text: STATUS_WORDS[status] }) : null;
    const head = [el("span", { class: "mk", "aria-hidden": "true", text: MARKS[status] || "○" }), el("span", { class: "ttl", text: c.title }), tag, word, el("span", { class: "cat", text: c.category })];
    const issues = c.issues || [];
    const detail = c.detail;
    const hasBody = issues.length || detail;
    if (!hasBody) return el("div", { class: `check ${cls}`, "data-check": c.id }, el("div", { class: "check-row" }, ...head, el("span", { class: "sr-only", text: status })));
    const body = el("div", { class: "body" },
      detail ? el("p", { class: "detail", text: detail }) : null,
      issues.length ? el("ul", {}, issues.map((i) => el("li", {},
        i.field && state.fields.has(i.field) ? el("button", { type: "button", class: "link", onclick: () => focusField(i.field), text: state.fields.get(i.field).label }) : null,
        i.field && state.fields.has(i.field) ? ": " : "",
        i.message))) : null,
    );
    return el("details", { class: `check ${cls}`, "data-check": c.id, open: status === "failed" || status === "unchecked" || (status === "warning" && issues.length <= 6) ? true : null },
      el("summary", {}, ...head), body);
  }

  function renderChecks() {
    const root = $("#check-groups");
    root.replaceChildren();
    const rule = initialRule();
    if (rule) {
      root.append(el("p", { class: "initial-notice" }, el("b", { text: "Format for initial submission: " }), rule,
        state.initialSubmission ? " Errors are shown as warnings (initial submission is ticked)." : " Tick “initial submission (any format)” to show errors as warnings."));
    }
    if ($("#toc").children.length) renderCheckStatus();
    if (state.results && state.resultsStale) {
      root.append(el("p", { class: "hint", text: state.filesChanged
        ? "You have removed or replaced a file since the last validation, so the checks that read files are grey again. Validate again to refresh them."
        : "You have changed the form since the last validation. Validate again to refresh these results." }));
    } else if (state.results) {
      const ok = state.results.ok;
      const errors = state.results.checks.filter((c) => c.status === "failed").length;
      const downgraded = state.results.downgraded || 0;
      if (downgraded) root.append(el("p", { class: "verdict partial", text: `▲ Ready as an initial submission: ${plural(downgraded, "error")} shown as warnings. Fix them before the revision.` }));
      else root.append(el("p", { class: `verdict ${ok ? "ok" : "bad"}`, text: ok ? "✔ No blocking errors. Read any warnings, then submit." : `✖ Not ready: ${plural(errors, "check")} failed. Fix them before submitting.` }));
    }
    for (const [group, title, hint] of BLOCKS) {
      const checks = blockChecks(group);
      if (!checks.length && group !== "live") {
        if (!state.plan) root.append(el("div", { class: "check-group" }, el("h3", { text: title }), el("p", { class: "hint", text: "Working out which checks apply…" })));
        continue;
      }
      const shown = checks.filter((c) => c.status !== "not_applicable");
      const na = checks.filter((c) => c.status === "not_applicable");
      const done = shown.filter((c) => ["passed", "failed", "warning"].includes(c.status));
      const counts = done.length ? `${done.filter((c) => c.status === "passed").length} of ${shown.length} passed` : plural(shown.length, "check");
      const worst = shown.some((c) => c.status === "failed") ? "st-failed"
        : shown.some((c) => c.status === "warning") ? "st-warning"
          : shown.length && done.length === shown.length ? "st-passed" : "st-pending";
      root.append(el("div", { class: `check-group ${worst}`, "data-group": group },
        el("h3", {}, title, el("span", { class: "count", text: counts })),
        el("p", { class: "hint", text: hint }),
        el("div", { class: "check-list" }, shown.map(checkItem)),
        na.length ? el("details", { class: "na" },
          el("summary", { text: `${plural(na.length, "check")} not applicable${state.files.length ? "" : " (some will apply once you upload files)"}` }),
          el("ul", {}, na.map((c) => el("li", {}, c.title, c.detail ? el("span", { class: "muted", text: ` — ${c.detail}` }) : null)))) : null,
      ));
    }
  }

  function renderAiAvailability() {
    const { label: ai, reason } = aiAvailability();
    const btn = $("#validate-ai-btn");
    btn.disabled = !ai || state.busy;
    btn.title = ai ? `Also runs the AI checks (${ai})` : reason;
    setAiTooltip($("#validate-ai-wrap"), reason);
  }

  let planSeq = 0;
  async function refreshPlan() {
    if (!state.venue || !state.ws) return;
    const seq = ++planSeq;
    try {
      const plan = await postJSON(`/api/workspaces/${state.ws}/plan`, { text: state.text });
      if (seq !== planSeq || plan.venue !== state.venue.slug) return;
      const rulesChanged = JSON.stringify(plan.live_rules) !== JSON.stringify(state.liveRules);
      const schemaChanged = JSON.stringify(plan.schema_issues || []) !== JSON.stringify(state.plan?.schema_issues || []);
      state.plan = plan;
      state.liveRules = plan.live_rules || {};
      if (plan.requirements) state.requirements = plan.requirements;
      renderInitialSubmission();
      if (rulesChanged) {
        for (const fid of state.widgets.keys()) updateLimits(fid);
        syncWidgets(); // redraws the counters against the new limits
      }
      if (rulesChanged || schemaChanged) runLive();
      renderRequirements();
      renderChecks();
    } catch (err) {
      if (err.status === 404) { store.del("workspace"); await ensureWorkspace(); }
    }
  }
  const schedulePlan = debounce(refreshPlan, 900);

  async function runValidate(withAi) {
    const status = $("#validate-status");
    const buttons = [$("#validate-btn"), $("#validate-ai-btn")];
    buttons.forEach((b) => (b.disabled = true));
    status.className = "small";
    const bar = el("progress", { class: "validate-progress", max: "1", value: "0", "aria-label": "Validation progress" });
    const label = el("span", { class: "validate-progress-label", text: withAi ? "Validating, with the AI checks (this can take a minute)…" : "Validating…" });
    status.replaceChildren(bar, label);
    const onProgress = ({ done, total, label: stage }) => {
      bar.max = total || 1;
      bar.value = done;
      const pct = total ? Math.round((100 * done) / total) : 0;
      label.textContent = stage === "done" ? "Finishing…" : `${pct}% · ${stage}${withAi && stage === "AI checks" ? " (this can take a minute)" : ""}…`;
    };
    state.showRequired = true;
    try {
      const res = await postStream(`/api/workspaces/${state.ws}/validate`, { text: state.text, ai: withAi, initial_submission: state.initialSubmission }, withAi ? llmHeaders() : {}, onProgress);
      state.results = res;
      state.resultsStale = false;
      state.filesChanged = false;
      state.touched = true;
      const c = res.counts;
      const notes = [];
      if (!res.network_checks) notes.push("link and DOI checks are switched off on this server");
      if (res.ai_error) notes.push(`AI checks failed: ${res.ai_error}`);
      status.textContent = `${c.passed} passed, ${c.failed} failed, ${c.warning} with warnings${c.needs_ai ? `, ${c.needs_ai} need AI` : ""}.${notes.length ? ` (${notes.join("; ")})` : ""}`;
      if (res.ai_error) status.className = "small err";
    } catch (err) {
      status.className = "small err";
      status.textContent = err.message;
    } finally {
      buttons[0].disabled = false;
      renderReportButton();
        renderAiAvailability();
      runLive();
      renderChecks();
      $("#checks-panel").open = true;
      $("#checks-panel").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function focusField(fid) {
    const w = state.widgets.get(fid);
    if (!w) return;
    w.card.scrollIntoView({ behavior: "smooth", block: "center" });
    const target = w.card.querySelector("input, textarea, select");
    if (target) setTimeout(() => target.focus({ preventScroll: true }), 300);
  }

  // ------------------------------------------------------- bottom actions

  function downloadSub() {
    download(new Blob([state.text], { type: "text/plain" }), state.venue.subfile_name);
  }

  // The last validation as a text report, like `paperpush validate -o report.txt`.
  function downloadReport() {
    if (!state.results?.report_text) { toast("Validate first.", "error"); return; }
    download(new Blob([state.results.report_text], { type: "text/plain" }), `${state.venue.subfile_name.replace(/\.sub(\.txt)?$/i, "")}_validation.txt`);
    if (state.resultsStale) toast("The form changed since this validation. Validate again for an up-to-date report.");
  }

  function renderReportButton() {
    const btn = $("#download-report");
    btn.disabled = !state.results?.report_text;
    btn.title = btn.disabled ? "Validate first" : "The last validation's report, as a text file";
  }

  async function downloadBundle() {
    try {
      if (!state.ws) await ensureWorkspace();
      const res = await api(`/api/workspaces/${state.ws}/bundle`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: state.text }) });
      download(await res.blob(), `${state.venue.subfile_name.replace(/\.sub(\.txt)?$/i, "")}-submission.zip`);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // A standalone copy of the page as it is now, to send to a PI or
  // collaborator: the form's values, the requirements, and the check
  // results, with the styles inlined. Of the scripts only viewer.js comes
  // along, so the side menu, filters, search, and copy buttons still work.

  async function savePage() {
    let css = "";
    let viewer = "";
    try { css = await (await api("/static/style.css")).text(); } catch { /* unstyled copy */ }
    try { viewer = await (await api("/static/viewer.js")).text(); } catch { /* a static copy */ }
    const doc = document.documentElement.cloneNode(true);
    const live = $$("input, textarea, select");
    const copies = $$("input, textarea, select", doc);
    live.forEach((node, i) => {
      const copy = copies[i];
      if (!copy) return;
      if (node.type === "checkbox" || node.type === "radio") { if (node.checked) copy.setAttribute("checked", ""); else copy.removeAttribute("checked"); }
      else if (node.tagName === "TEXTAREA") copy.textContent = node.value;
      else if (node.tagName === "SELECT") $$("option", copy).forEach((o, j) => (j === node.selectedIndex ? o.setAttribute("selected", "") : o.removeAttribute("selected")));
      else copy.setAttribute("value", node.value);
      if (!node.hasAttribute("data-keep")) copy.setAttribute("disabled", "");
    });
    $$("script, dialog, #toast, .settings, #add-venue-link, .topbar-key, .final-actions, .check-actions, .autofill, #restored, #shared-note, #venue-options", doc).forEach((n) => n.remove());
    $("#sub-text-panel", doc)?.remove();
    $$("details", doc).forEach((d) => d.setAttribute("open", ""));
    const link = $("link[rel=stylesheet]", doc);
    if (link) link.replaceWith(Object.assign(document.createElement("style"), { textContent: css }));
    let editLink = null;
    try { editLink = await shareLink(); } catch { /* no CompressionStream: no link */ }
    const stamp = el("p", { class: "hint", text: `Saved from paperpush ${state.info?.version || ""} on ${new Date().toLocaleString()}. ` });
    if (editLink) stamp.append(el("a", { href: editLink, target: "_blank", rel: "noopener", text: "Open an editable copy on the paperpush website" }), ".");
    else stamp.append(`To keep working on it, open ${state.venue.subfile_name} in paperpush.`);
    $("main", doc)?.prepend(stamp);
    if (viewer) $("body", doc).append(Object.assign(document.createElement("script"), { textContent: viewer }));
    const html = `<!doctype html>\n${doc.outerHTML}`;
    download(new Blob([html], { type: "text/html" }), `${state.venue.slug}-paperpush.html`);
  }

  // ----------------------------------------------------------- share link

  // Where a share link points. A page served by `paperpush ... --web` is on
  // this computer only, so its links go to the public website instead.
  const PUBLIC_SITE = "https://paperpush.onrender.com/";

  // A link that opens this form, filled in and editable, on the website:
  // the .sub rides in the URL's #fragment, so nothing is stored on a server
  // and the recipient needs no install. Uploaded files are not in it.
  async function shareLink({ website = false } = {}) {
    const base = website || state.local ? PUBLIC_SITE : `${location.origin}${location.pathname}`;
    return `${base}#/v/${encodeURIComponent(state.venue.slug)}/s/${await Subfile.toShareCode(state.text)}`;
  }

  // Whether this server keeps shared forms: the hosted site does; a
  // `paperpush web` page on this computer does not.
  const canShareHere = () => Boolean(state.info?.shared) && !state.local;

  // Share where this server cannot keep a shared form: open the form, filled
  // in, on the public website (the .sub rides in the link's #fragment, which
  // is never sent to a server). The author presses Share there to edit it
  // with others.
  async function openOnWebsite() {
    const tab = window.open("", "_blank"); // open now, while the click still counts
    let link;
    try {
      link = await shareLink({ website: true });
    } catch (err) {
      tab?.close();
      toast(`Could not make a link: ${err.message}`, "error");
      return;
    }
    if (!tab) { toast("Your browser blocked the new tab. Allow pop-ups for this page and press Share again.", "error"); return; }
    tab.opener = null;
    tab.location.href = link;
    const files = (state.files || []).length ? " Your uploaded files stay here: upload them there to validate." : "";
    toast(`Opened this form on the paperpush website. Press Share there to edit it with others.${files}`);
  }

  // ---------------------------------------------------------- shared forms

  // "Share for editing": one copy of the form on the server that everyone
  // with the link edits (web/shared.py). The page encrypts it with the key
  // in the link (shared.js), saves each edit a moment after it is made,
  // checks for others' edits every few seconds, and merges field by field
  // (Subfile.mergeFields). When two people change the same field, the one
  // who saves second chooses which value to keep.

  const POLL_MS = 4000;
  const sharedPath = (id) => `/api/shared/${encodeURIComponent(id)}`;
  const sharedLink = () => `${location.origin}${location.pathname}#/f/${state.shared.id}/${state.shared.key}`;
  let pushTimer = null;
  let pollTimer = null;
  let pushing = false;
  let resolving = false;

  function setSharedStatus(text, kind = "muted") {
    const node = $("#shared-status");
    node.textContent = text;
    node.className = `small ${kind}`;
  }

  function renderSharedNote() {
    const sh = state.shared;
    $("#shared-note").hidden = !sh;
    $("#share-btn").hidden = Boolean(sh);
    $("#share-btn").title = !canShareHere()
      ? "Open this form, filled in, on the paperpush website, where you can share it for editing. Your uploaded files stay here."
      : "Put this form online so others can edit it with you. It is encrypted with a key in the link, so the server cannot read it.";
    $("#venue-search").disabled = Boolean(sh || state.local);
    $("#venue-toggle").hidden = Boolean(sh || state.local);
    for (const sel of ["#reset-btn", "#discard-draft"]) { const n = $(sel); if (n) n.hidden = Boolean(sh || state.local); }
    if (!sh) return;
    const info = state.info.shared || {};
    const days = Math.round(info.ttl_days || 90);
    $("#shared-expiry").textContent = info.durable
      ? ` It is deleted after ${days} days without edits.`
      : " This server keeps shared forms only until it restarts, so do not rely on the link for long.";
  }

  async function startSharing() {
    if (!state.venue || state.shared) return;
    const btn = $("#share-btn");
    btn.disabled = true;
    try {
      const key = SharedCrypto.newKey();
      const { aes, token } = await SharedCrypto.derive(key);
      const text = state.text;
      const res = await postJSON("/api/shared", { ciphertext: await SharedCrypto.encrypt(text, aes), token });
      state.shared = { id: res.id, key, aes, token, version: res.version, base: text };
      history.replaceState(null, "", `${location.pathname}${location.search}#/f/${res.id}/${key}`);
      renderSharedNote();
      startPolling();
      setSharedStatus(`Saved ${new Date().toLocaleTimeString()}.`);
      await copySharedLink();
    } catch (err) {
      toast(`Could not share the form: ${err.message}`, "error");
    } finally {
      btn.disabled = false;
    }
  }

  async function copySharedLink() {
    if (!state.shared) return;
    const link = sharedLink();
    const files = (state.files || []).length ? " Uploaded files are not shared: each person uploads their own to validate." : "";
    try {
      await navigator.clipboard.writeText(link);
      toast(`Link copied. Everyone who opens it edits this same form.${files}`);
    } catch {
      window.prompt("Copy this link:", link);
    }
  }

  // Open #/f/<id>/<key>: fetch, decrypt, and show the shared form.
  async function openShared(id, key) {
    let keys, res, text;
    try {
      keys = await SharedCrypto.derive(key);
      res = await getJSON(sharedPath(id));
    } catch (err) {
      toast(err.status === 404 ? "This shared form no longer exists: it was deleted, or expired after going unedited." : `Could not open the shared form: ${err.message}`, "error");
      location.hash = "";
      return;
    }
    try {
      text = await SharedCrypto.decrypt(res.ciphertext, keys.aes);
    } catch {
      toast("This link's key does not open the shared form. It may have been cut off: ask for the link again.", "error");
      location.hash = "";
      return;
    }
    const { venue } = Subfile.parse(text);
    stopSharing();
    state.shared = { id, key, aes: keys.aes, token: keys.token, version: res.version, base: text };
    state.venue = null; // open it afresh, even if this venue is already showing
    await openVenue(venue, text);
    if (state.venue?.slug !== venue) { stopSharing(); return; }
    renderSharedNote();
    setSharedStatus(`Up to date as of ${new Date().toLocaleTimeString()}.`);
    startPolling();
  }

  // Leave the shared form (the page moves to another venue, or it was deleted).
  function stopSharing() {
    clearTimeout(pushTimer);
    clearInterval(pollTimer);
    pushTimer = pollTimer = null;
    state.shared = null;
    if (state.info) renderSharedNote();
  }

  function schedulePush() {
    clearTimeout(pushTimer);
    if (state.shared && state.text === state.shared.base && !pushing) return; // nothing new to save
    setSharedStatus("Saving…");
    pushTimer = setTimeout(push, 700);
  }

  async function push() {
    const sh = state.shared;
    if (!sh || resolving) return;
    if (pushing) { schedulePush(); return; }
    const text = state.text;
    if (text === sh.base) { setSharedStatus(`Saved ${new Date().toLocaleTimeString()}.`); return; }
    pushing = true;
    try {
      const ciphertext = await SharedCrypto.encrypt(text, sh.aes);
      const res = await fetch(sharedPath(sh.id), {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-Paperpush-Share-Token": sh.token },
        body: JSON.stringify({ ciphertext, base_version: sh.version }),
        keepalive: ciphertext.length < 60000,
      });
      if (state.shared !== sh) return;
      if (res.ok) {
        const body = await res.json();
        sh.version = body.version;
        sh.base = text;
        setSharedStatus(`Saved ${new Date().toLocaleTimeString()}.`);
      } else if (res.status === 409) {
        const { current } = await res.json();
        await receive(current.version, await SharedCrypto.decrypt(current.ciphertext, sh.aes));
      } else if (res.status === 404) {
        sharedGone();
        return;
      } else {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
        setSharedStatus(`Not saved: ${detail}`, "err");
        return;
      }
    } catch (err) {
      setSharedStatus(`Not saved (${err.message}). Retrying…`, "err");
      pushTimer = setTimeout(push, POLL_MS);
      return;
    } finally {
      pushing = false;
    }
    if (state.shared === sh && state.text !== sh.base) schedulePush();
  }

  function startPolling() {
    clearInterval(pollTimer);
    pollTimer = setInterval(poll, POLL_MS);
  }

  async function poll() {
    const sh = state.shared;
    if (!sh || pushing || resolving || document.visibilityState !== "visible") return;
    let res;
    try {
      res = await getJSON(`${sharedPath(sh.id)}?known=${sh.version}`);
    } catch (err) {
      if (err.status === 404) sharedGone();
      return;
    }
    if (state.shared !== sh || res.version === sh.version || !res.ciphertext || pushing) return;
    let theirs;
    try { theirs = await SharedCrypto.decrypt(res.ciphertext, sh.aes); } catch { return; }
    await receive(res.version, theirs);
  }

  // Someone else saved `theirs` as `version`: take it in, keeping this
  // page's unsaved edits, and ask about any field both sides changed.
  async function receive(version, theirs) {
    const sh = state.shared;
    const mine = state.text;
    const { text, conflicts } = Subfile.mergeFields(sh.base, mine, theirs, state.fields);
    sh.base = theirs;
    sh.version = version;
    if (conflicts.length) {
      resolving = true;
      let chosen;
      try { chosen = await askConflicts(conflicts); } finally { resolving = false; }
      let resolved = text;
      for (const c of conflicts) if (chosen[c.id] === "theirs") resolved = Subfile.setField(resolved, state.fields.get(c.id) || { id: c.id, type: "text" }, c.theirs);
      applyRemote(resolved);
    } else {
      applyRemote(text);
      if (text === theirs) setSharedStatus(`Updated with a collaborator's changes at ${new Date().toLocaleTimeString()}.`);
    }
    if (state.text !== sh.base) schedulePush();
  }

  function applyRemote(text) {
    if (text === state.text) return;
    setText(text);
    syncWidgets();
  }

  function sharedGone() {
    const slug = state.venue?.slug;
    stopSharing();
    if (slug) {
      store.set(`draft:${slug}`, state.text);
      history.replaceState(null, "", `${location.pathname}${location.search}#/v/${encodeURIComponent(slug)}`);
    }
    toast("This shared form was deleted. What you see is now your own copy, saved in this browser.", "error");
  }

  async function stopSharingForAll() {
    const sh = state.shared;
    if (!sh) return;
    if (!window.confirm("Delete the shared form for everyone? Each person keeps the copy they have open, but the link stops working.")) return;
    try {
      await api(sharedPath(sh.id), { method: "DELETE", headers: { "X-Paperpush-Share-Token": sh.token } });
    } catch (err) {
      if (err.status !== 404) { toast(`Could not delete it: ${err.message}`, "error"); return; }
    }
    sharedGone();
    toast("Deleted the shared form. Your copy is saved in this browser.");
  }

  // One question per field both sides changed; resolves to {fieldId: "mine"|"theirs"}.
  function askConflicts(conflicts) {
    const dialog = $("#conflict-dialog");
    const preview = (v) => el("pre", { class: "conflict-value", text: v === "" ? "(empty)" : v.length > 600 ? `${v.slice(0, 600)}…` : v });
    const rows = conflicts.map((c) => {
      const label = state.fields.get(c.id)?.label || c.id;
      const name = `conflict-${c.id}`;
      return el("fieldset", { class: "conflict" },
        el("legend", { text: label }),
        el("label", { class: "conflict-choice" }, el("input", { type: "radio", name, value: "theirs" }), el("span", { text: "Theirs (just saved by someone else)" }), preview(c.theirs)),
        el("label", { class: "conflict-choice" }, el("input", { type: "radio", name, value: "mine", checked: true }), el("span", { text: "Mine" }), preview(c.mine)));
    });
    $("#conflict-body").replaceChildren(...rows);
    return new Promise((resolve) => {
      const done = () => {
        const out = {};
        for (const c of conflicts) out[c.id] = $(`input[name="conflict-${CSS.escape(c.id)}"]:checked`, dialog)?.value || "mine";
        dialog.removeEventListener("close", done);
        resolve(out);
      };
      dialog.addEventListener("close", done);
      dialog.showModal();
    });
  }

  function command(text, { wrap = false } = {}) {
    const code = el("code", { text, class: wrap ? "wrap" : "" });
    const copy = el("button", { type: "button", class: "btn btn-small", text: "Copy" });
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(text); copy.textContent = "Copied"; setTimeout(() => (copy.textContent = "Copy"), 1500); } catch { /* clipboard unavailable */ }
    });
    return el("div", { class: "cmd" }, code, copy);
  }

  function openPortalDialog() {
    const v = state.venue;
    const sub = v.subfile_name;
    const body = $("#portal-body");
    const steps = [];
    const submitHint = () => el("p", { class: "hint", text: "paperpush validates again, opens a browser, and fills in the portal. It stops before the final submit button, so you can look everything over and press it yourself." });
    const loginStep = () => el("li", {}, el("strong", { text: `Log in to ${v.name}` }), " once. paperpush keeps the login in your system keychain:", command(`paperpush login ${v.slug}`));
    // The two routes are alternatives, so show them as tabs: one visible at a time.
    const routes = (subPath, where, stepList) => {
      const ai = el("div", { class: "portal-panel", role: "tabpanel" },
        el("p", {}, `Ask Claude Code (or another AI assistant that can run commands)${where}:`),
        command(`Submit ${subPath} to ${v.name} with paperpush.`, { wrap: true }),
        el("p", { class: "hint", text: `The assistant installs paperpush if needed, validates the .sub, and fills in the portal, stopping before the final submit button.${v.login_required ? ` It will ask you to run paperpush login ${v.slug} yourself, since only you can sign in.` : ""}` }));
      const manual = el("div", { class: "portal-panel", role: "tabpanel" }, el("ol", {}, stepList));
      const panels = [ai, manual];
      const tabs = ["With AI", "Yourself"].map((label, i) => el("button", {
        type: "button", class: "portal-tab", role: "tab", text: label,
        onclick: () => select(i),
      }));
      function select(i) {
        tabs.forEach((t, j) => { t.classList.toggle("active", i === j); t.setAttribute("aria-selected", String(i === j)); });
        panels.forEach((p, j) => { p.hidden = i !== j; });
      }
      select(0);
      return [el("div", { class: "portal-tabs", role: "tablist" }, tabs), ...panels];
    };
    if (state.local) {
      if (v.login_required) steps.push(loginStep());
      steps.push(el("li", {}, el("strong", { text: "Submit" }), " from the same directory:", command(`paperpush submit ${state.local.path}`), submitHint()));
      body.replaceChildren(...routes(state.local.path, "", steps));
      $("#portal-dialog").showModal();
      return;
    }
    const download = el("div", { class: "portal-download" },
      el("p", {}, el("strong", { text: "First, download your submission." }), ` The bundle holds ${sub} and the files you uploaded; unzip it into a folder. (Already have the files? The .sub alone is enough if it sits next to them.)`),
      el("div", { class: "cmd" },
        el("button", { type: "button", class: "btn primary btn-small", onclick: downloadBundle, text: "Download bundle (.zip)" }),
        el("button", { type: "button", class: "btn btn-small", onclick: downloadSub, text: `Download ${sub} only` })),
    );
    steps.push(el("li", {}, el("strong", { text: "Install paperpush" }), " (Python 3.10 or later), and the browser it drives:", command("pip install paperpush"), command("playwright install chromium")));
    if (v.login_required) steps.push(loginStep());
    steps.push(el("li", {}, el("strong", { text: "Submit" }), " from inside the unzipped folder:", command(`paperpush submit ${sub}`), submitHint()));
    body.replaceChildren(download, ...routes(sub, ", started in the unzipped folder", steps));
    $("#portal-dialog").showModal();
  }

  // ------------------------------------------------------------- routing

  async function route() {
    const f = location.hash.match(/^#\/f\/([A-Za-z0-9_-]+)\/([A-Za-z0-9_-]+)/);
    if (f && !state.local) {
      if (state.shared?.id !== f[1]) await openShared(f[1], f[2]);
      return;
    }
    if (state.shared) stopSharing();
    const m = location.hash.match(/^#\/v\/([^/]+)(?:\/s\/([A-Za-z0-9_-]+))?/);
    if (m) {
      const slug = decodeURIComponent(m[1]);
      if (m[2] && !state.local) {
        // A share link: open its form, then drop the code from the URL so a
        // reload keeps the recipient's edits (saved as this venue's draft).
        history.replaceState(null, "", `${location.pathname}${location.search}#/v/${encodeURIComponent(slug)}`);
        let shared;
        try {
          shared = await Subfile.fromShareCode(m[2]);
        } catch {
          toast("That share link is damaged or cut off, so it could not be opened. Ask for it again.", "error");
        }
        if (shared != null) {
          const hadDraft = (store.get(`draft:${slug}`) || "") !== "" && store.get(`draft:${slug}`) !== shared;
          if (state.venue && state.venue.slug === slug) { setText(shared); syncWidgets(); }
          else await openVenue(slug, shared);
          if (state.venue?.slug === slug) {
            store.set(`draft:${slug}`, shared);
            toast(hadDraft ? "Opened the shared form. It replaced the form you had saved for this venue in this browser." : "Opened the shared form. Your edits are saved in this browser.");
          }
          pendingText = null;
          return;
        }
      }
      if (!state.venue || state.venue.slug !== slug) await openVenue(slug, pendingText);
      pendingText = null;
    } else {
      state.venue = null;
      $("#workspace").hidden = true;
      hideNav();
      $("#venue-search").value = "";
      document.title = "paperpush";
    }
  }

  function hideNav() {
    $("#toc").replaceChildren();
    $("#sidenav").hidden = true;
    $("#layout").classList.remove("with-nav");
  }

  let pendingText = null;
  function openSubFile(text) {
    const { venue } = Subfile.parse(text);
    if (!venue) { toast("That file has no '@venue:' line, so it is not a .sub file.", "error"); return; }
    if (state.venue && state.venue.slug === venue) {
      setText(text);
      syncWidgets();
      return;
    }
    pendingText = text;
    location.hash = `#/v/${encodeURIComponent(venue)}`;
  }

  function wire() {
    wireSettings();
    wireCombo();
    wireManuscript();
    wireImport();
    $("#sub-text").addEventListener("input", debounce((e) => setText(e.target.value, { fromEditor: true }), 300));
    $("#discard-draft").addEventListener("click", () => {
      store.del(`draft:${state.venue.slug}`);
      $("#restored").hidden = true;
      setText(state.venue.template);
      syncWidgets();
      state.touched = false;
      renderChecks();
    });
    $("#validate-btn").addEventListener("click", () => runValidate(false));
    $("#validate-ai-btn").addEventListener("click", () => runValidate(true));
    $("#initial-submission").addEventListener("change", (e) => setInitialSubmission(e.target.checked));
    $("#download-sub").addEventListener("click", downloadSub);
    $("#download-zip").addEventListener("click", downloadBundle);
    $("#download-report").addEventListener("click", downloadReport);
    $("#save-page").addEventListener("click", savePage);
    $("#share-btn").addEventListener("click", () => (canShareHere() ? startSharing() : openOnWebsite()));
    $("#shared-copy").addEventListener("click", copySharedLink);
    $("#shared-stop").addEventListener("click", stopSharingForAll);
    document.addEventListener("visibilitychange", () => { if (state.shared && document.visibilityState === "visible") poll(); });
    $("#portal-btn").addEventListener("click", openPortalDialog);
    $("#portal-wrap").addEventListener("click", (e) => { if (e.currentTarget.dataset.tooltip) e.currentTarget.focus(); });
    $("#form").addEventListener("submit", (e) => e.preventDefault());
    window.addEventListener("hashchange", route);
  }

  async function init() {
    paletteFromUrl();
    applySettings();
    wire();
    wireKeyPanel();
    try {
      [state.info, state.venues] = await Promise.all([getJSON("/api/info"), getJSON("/api/venues")]);
    } catch (err) {
      $("#venue-hint").textContent = `Could not load the venues: ${err.message}`;
      return;
    }
    $("#version").textContent = `v${state.info.version}`;
    $("#key-panel").hidden = !state.info.ai;
    renderKeyPanel();
    renderSharedNote();
    const input = $("#venue-search");
    input.disabled = false;
    $("#venue-hint").textContent = `${state.venues.length} venues. Search by name, or open the list.`;
    if (await openLocal()) return;
    await route();
    if (!state.venue) input.focus();
  }

  // Opened by `paperpush subfile|autofill|validate --web`: load the .sub file
  // from disk, lock the page to it, then run the step the command asked for.
  async function openLocal() {
    const token = new URLSearchParams(location.search).get("local");
    if (!token || !state.info.local) return false;
    state.local = { token };
    let local;
    try {
      local = await getJSON("/api/local");
    } catch (err) {
      state.local = null;
      toast(`Could not open the local .sub file: ${err.message}`, "error");
      return false;
    }
    Object.assign(state.local, local);
    // Keep the local server up while this page is open (a preview server
    // started by `paperpush subfile`/`autofill` stops once nothing pings it).
    setInterval(() => { if (document.visibilityState === "visible") api("/api/local/ping").catch(() => {}); }, 60000);
    renderSharedNote();
    state.ws = "local";
    state.files = local.files;
    $("#venue-search").disabled = true;
    $("#venue-toggle").hidden = true;
    $("#venue-hint").textContent = "";
    $("#local-path").textContent = local.path;
    $("#local-note").hidden = false;
    for (const sel of ["#reset-btn", "#discard-draft"]) { const n = $(sel); if (n) n.hidden = true; }
    const openRow = $("#open-sub")?.closest(".menu-row");
    if (openRow) openRow.hidden = true;
    pendingText = local.text;
    if (location.hash !== `#/v/${encodeURIComponent(local.venue)}`) history.replaceState(null, "", `${location.pathname}${location.search}#/v/${encodeURIComponent(local.venue)}`);
    await route();
    document.title = `${local.path} · paperpush`;
    if (local.action === "autofill") {
      if ($("#autofill-btn").disabled) toast(`Cannot autofill yet: ${$("#autofill-hint").textContent}`, "error");
      else { $("#manuscript-panel").scrollIntoView({ block: "start" }); await runAutofill(); }
    } else if (/^validate(-ai)?(\+initial)?$/.test(local.action || "")) {
      const [action, initial] = local.action.split("+");
      if (initial) {
        await refreshPlan(); // the article type can change the rule
        if (initialRule()) setInitialSubmission(true);
        else toast(`Validating normally: ${state.venue.name}'s guidelines do not let the initial submission be in any format.`, "error");
        renderInitialSubmission();
      }
      const withAi = action === "validate-ai" && Boolean(aiAvailability().label);
      if (action === "validate-ai" && !withAi) toast(`Validating without the AI checks: ${aiAvailability().reason}`, "error");
      await runValidate(withAi);
    }
    return true;
  }

  init();
})();
