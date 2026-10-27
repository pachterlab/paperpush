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

  const store = {
    get(key) {
      try { return localStorage.getItem(`paperpush:${key}`); } catch { return null; }
    },
    set(key, value) {
      try { localStorage.setItem(`paperpush:${key}`, value); } catch { /* storage unavailable */ }
    },
    del(key) {
      try { localStorage.removeItem(`paperpush:${key}`); } catch { /* storage unavailable */ }
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
    touched: false, // the author has changed something: colour the live checks
    showRequired: false, // flag empty required fields (after the first Validate)
    review: new Set(), // field ids autofill asked the author to confirm
    busy: false,
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
    if (!state.llm.key) return { label: null, reason: KEY_TOOLTIP };
    const provider = state.llm.provider || guessProvider(state.llm.key);
    if (!provider) return { label: null, reason: "Choose which provider your API key is for." };
    return { label: `your ${PROVIDER_LABELS[provider]} key`, reason: "" };
  }

  function renderKeyPanel() {
    $("#key-panel").classList.toggle("has-key", Boolean(state.llm.key));
  }

  function setAiTooltip(wrap, reason) {
    const message = !state.llm.key ? KEY_TOOLTIP : reason;
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
    $("#key-clear").addEventListener("click", () => { input.value = ""; update(); input.focus(); });
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

  const baseName = (path) => path.trim().replace(/\\/g, "/").split("/").pop();
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

  function applySettings() {
    const alt = store.get("palette") === "alt";
    document.documentElement.dataset.palette = alt ? "alt" : "default";
    $("#opt-colorblind").checked = alt;
    const showText = store.get("subtext") === "1";
    $("#opt-subtext").checked = showText;
    $("#sub-text-panel").hidden = !showText;
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
    $("#opt-subtext").addEventListener("change", (e) => { store.set("subtext", e.target.checked ? "1" : "0"); applySettings(); });
    $("#open-sub").addEventListener("change", async (e) => {
      const file = e.target.files[0];
      e.target.value = "";
      close();
      if (file) openSubFile(await file.text());
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
    Object.assign(state, {
      venue,
      fields: new Map(venue.fields.map((f) => [f.id, f])),
      plan: null,
      liveRules: {},
      requirements: venue.manuscript_requirements,
      results: null,
      resultsStale: false,
      touched: false,
      showRequired: false,
      review: new Set(),
    });
    const draft = store.get(`draft:${slug}`);
    const restored = text == null && draft && draft !== venue.template;
    state.text = text ?? (draft || venue.template);
    state.touched = Boolean(restored || text);

    $("#venue-search").value = venue.name;
    $("#workspace").hidden = false;
    $("#restored").hidden = !restored;
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
    refreshPlan();
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
    const links = [];
    if (v.submission_guide) links.push(el("a", { href: v.submission_guide, target: "_blank", rel: "noopener", text: "Author guidelines ↗" }));
    if (v.submission_url) links.push(el("a", { href: v.submission_url, target: "_blank", rel: "noopener", text: "Submission portal ↗" }));
    if (v.site_url) links.push(el("a", { href: v.site_url, target: "_blank", rel: "noopener", text: "Website ↗" }));
    $("#venue-links").replaceChildren(...links);
  }

  // -------------------------------------------------------------- workspace

  async function ensureWorkspace() {
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
      state.files = res.files;
      refreshFileWidgets();
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

  // ------------------------------------------------------------- the text

  // Every change goes through here.
  function setText(next, { fromEditor = false } = {}) {
    if (next === state.text) return;
    const before = Subfile.parse(state.text).values;
    state.text = next;
    state.touched = true;
    if (state.results) state.resultsStale = true;
    store.set(`draft:${state.venue.slug}`, next);
    if (!fromEditor) $("#sub-text").value = next;
    else syncWidgets();
    const after = Subfile.parse(next).values;
    // The manuscript box and file pickers follow the text too.
    if (state.venue.manuscript_field && before[state.venue.manuscript_field] !== after[state.venue.manuscript_field]) renderManuscript();
    runLive();
    renderChecks();
    schedulePlan();
  }

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

  function renderManuscript() {
    const v = state.venue;
    const fid = v.manuscript_field;
    const zone = $("#dropzone");
    const input = $("#ms-input");
    const formats = v.manuscript_formats || [];
    input.accept = formats.join(",");
    $("#ms-formats").textContent = formats.length
      ? `Accepted formats: ${formats.map((f) => f.replace(/^\./, "").toUpperCase()).join(", ")}${state.fields.get(fid)?.max_file_size_mb ? ` · up to ${state.fields.get(fid).max_file_size_mb} MB` : ""}`
      : "Any format the venue accepts.";
    const current = fid ? baseName(values()[fid] || "") : "";
    const file = state.files.find((f) => f.name === current);
    zone.classList.toggle("has-file", Boolean(current));
    $("#ms-empty").hidden = Boolean(current);
    const box = $("#ms-file");
    box.hidden = !current;
    if (current) {
      const wrong = formats.length && !formats.includes(extOf(current));
      box.replaceChildren(
        el("div", { class: "ms-file" },
          el("strong", { class: "mono", text: current }),
          file ? el("span", { class: "muted small", text: formatSize(file.size) }) : el("span", { class: "chip warn", text: "not uploaded" }),
          wrong ? el("span", { class: "chip warn", text: `${extOf(current) || "no extension"} is not accepted` }) : null,
          el("button", { type: "button", class: "btn btn-small", onclick: (e) => { e.stopPropagation(); input.click(); }, text: "Replace" }),
          el("button", { type: "button", class: "btn btn-small", onclick: (e) => { e.stopPropagation(); setManuscript(""); }, text: "Remove" }),
        ),
      );
    }
    const af = $("#autofill-btn");
    const hint = $("#autofill-hint");
    const { label: ai, reason } = aiAvailability();
    af.disabled = !current || !file || !ai || state.busy;
    setAiTooltip($("#autofill-wrap"), reason);
    hint.textContent = !ai
      ? reason
      : !current || !file
        ? "Upload the manuscript first."
        : `Reads the manuscript (${ai}) and fills the title, abstract, authors, keywords, statements, and the rest. You review every value.`;
  }

  async function setManuscript(name) {
    const field = state.fields.get(state.venue.manuscript_field);
    if (field) onWidgetChange(field, name);
    renderManuscript();
  }

  function wireManuscript() {
    const zone = $("#dropzone");
    const input = $("#ms-input");
    const pick = async (files) => {
      const file = files && files[0];
      if (!file) return;
      const formats = state.venue.manuscript_formats || [];
      if (formats.length && !formats.includes(extOf(file.name))) toast(`${file.name}: ${state.venue.name} accepts ${formats.join(", ")} for the manuscript.`, "error");
      const saved = await uploadFiles([file]);
      if (saved.length) setManuscript(saved[0]);
    };
    zone.addEventListener("click", () => { if (!zone.classList.contains("has-file")) input.click(); });
    zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
    zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("drag"); pick(e.dataTransfer.files); });
    input.addEventListener("change", () => { pick(input.files); input.value = ""; });
    $("#autofill-btn").addEventListener("click", runAutofill);
  }

  async function runAutofill() {
    const btn = $("#autofill-btn");
    const out = $("#autofill-result");
    state.busy = true;
    btn.disabled = true;
    btn.replaceChildren(el("span", { class: "spinner" }), " Reading your manuscript…");
    try {
      const res = await postJSON(`/api/workspaces/${state.ws}/autofill`, { text: state.text }, llmHeaders());
      setText(res.text);
      syncWidgets();
      state.review = new Set(res.needs_review.map((o) => o.id));
      for (const [id, w] of state.widgets) w.card?.classList.toggle("review", state.review.has(id));
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
      btn.replaceChildren(el("span", { "aria-hidden": "true", text: "✨" }), " Fill in the rest from my manuscript");
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
    ["paper", "About the paper", "Copied from the manuscript."],
    ["authors", "Authors", ""],
    ["files", "Other files", "Figures, supplements, cover letter. Upload them here."],
    ["decisions", "Your decisions", "Policies and confirmations only you can answer. paperpush never fills these in for you."],
  ];

  function groupOf(f) {
    if (f.type === "authorlist") return "authors";
    if (f.type === "file" || f.type === "filelist") return "files";
    if (f.role === "never") return "decisions";
    return "paper";
  }

  function buildForm() {
    const root = $("#field-groups");
    root.replaceChildren();
    state.widgets.clear();
    const fields = state.venue.fields.filter((f) => f.id !== state.venue.manuscript_field);
    for (const [key, title, hint] of GROUPS) {
      const group = fields.filter((f) => groupOf(f) === key);
      if (!group.length) continue;
      root.append(el("fieldset", { class: "panel" }, el("legend", { text: title }), hint ? el("p", { class: "hint", style: "margin:0 0 12px", text: hint }) : null, group.map(fieldCard)));
    }
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

  function fieldCard(f) {
    const id = `fld-${f.id}`;
    const widget = makeWidget(f, id);
    const card = el(
      "div",
      { class: "field", id: `card-${f.id}`, "data-field": f.id },
      el("div", { class: "field-head" },
        el("label", { class: "field-label", for: id }, f.label, f.required ? el("span", { class: "req", title: "Required", text: "*" }) : null),
        f.required_if ? el("span", { class: "chip plain", text: `required if ${state.fields.get(f.required_if)?.label || f.required_if}` }) : null,
      ),
      f.help ? helpBlock(f.help) : null,
      el("p", { class: "limits" }),
      widget.node,
      el("div", { class: "field-foot" }, widget.counter ? widget.counter.node : null),
      el("ul", { class: "field-issues", "aria-live": "polite" }),
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
    const p = $(".limits", w.card);
    p.textContent = text;
    p.hidden = !text;
  }

  function helpBlock(text) {
    if (text.length <= 220) return el("p", { class: "hint", style: "margin:0 0 6px", text });
    const short = el("p", { class: "hint", style: "margin:0 0 6px" }, text.slice(0, 200).replace(/\s+\S*$/, "") + "… ");
    const more = el("button", { type: "button", class: "link small", text: "more" });
    more.addEventListener("click", () => short.replaceWith(el("p", { class: "hint", style: "margin:0 0 6px", text })));
    short.append(more);
    return short;
  }

  function makeWidget(f, id) {
    if (f.hierarchical) return hierarchicalWidget(f, id);
    if (f.fields && f.fields.length > 1 && (f.type === "authorlist" || f.type === "textarea")) return tableWidget(f, id);
    switch (f.type) {
      case "boolean": return booleanWidget(f, id);
      case "choice": return f.options && f.options.length ? choiceWidget(f, id) : textWidget(f, id);
      case "multichoice": return f.options && f.options.length ? multiWidget(f, id) : textWidget(f, id);
      case "file": return fileWidget(f, id);
      case "filelist": return fileListWidget(f, id);
      case "int": return textWidget(f, id, "number");
      case "textarea":
      case "authorlist": return textareaWidget(f, id);
      default: return textWidget(f, id);
    }
  }

  // A running count against the field's own limit, or the guidelines'.
  function counterFor(f) {
    const node = el("span", { class: "counter" });
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
        const v = custom.value.trim().replace(/,/g, " ");
        if (v && !selected.includes(v)) { selected = [...selected, v]; renderChips(); renderList(); emit(); }
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

  // File pickers choose among the uploaded files, or upload one on the spot.
  function fileSelect(current, accept) {
    const select = el("select", { class: "input" });
    select.append(el("option", { value: "", text: state.files.length ? "Choose an uploaded file…" : "Upload a file →" }));
    const names = state.files.map((f) => f.name);
    const cur = current ? baseName(current) : "";
    if (cur && !names.includes(cur)) select.append(el("option", { value: cur, text: `${cur} (not uploaded)` }));
    const ok = (n) => !accept || accept.some((ext) => n.toLowerCase().endsWith(ext.toLowerCase()));
    for (const n of [...names.filter(ok), ...names.filter((n) => !ok(n))]) select.append(el("option", { value: n, text: ok(n) ? n : `${n} (wrong type)` }));
    select.value = cur;
    return select;
  }

  function uploadButton(accept, multiple, onDone) {
    const input = el("input", { type: "file", hidden: true, multiple, accept: accept ? accept.join(",") : null });
    input.addEventListener("change", async () => {
      const saved = await uploadFiles(input.files);
      input.value = "";
      if (saved.length) onDone(saved);
    });
    return el("label", { class: "btn btn-small" }, "Upload…", input);
  }

  function fileWidget(f, id) {
    const wrap = el("div", { class: "file-pick" });
    let value = "";
    const render = () => {
      const select = fileSelect(value, f.accept);
      select.id = id;
      select.addEventListener("change", () => { value = select.value; onWidgetChange(f, value); });
      wrap.replaceChildren(select, uploadButton(f.accept, false, (saved) => { value = saved[0]; render(); onWidgetChange(f, value); }));
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
        const select = fileSelect(r[0], f.accept);
        select.addEventListener("change", () => { r[0] = select.value; emit(); });
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
        const del = el("button", { type: "button", class: "icon-btn", "aria-label": "Remove row", title: "Remove", onclick: () => { rows.splice(ri, 1); render(); emit(); } }, "×");
        wrap.append(el("div", { class: "row" }, select, ...cells, del));
      });
      wrap.append(el("div", { class: "row-actions" },
        uploadButton(f.accept, true, (saved) => { saved.forEach((n) => rows.push([n])); render(); emit(); }),
        state.files.length ? el("button", { type: "button", class: "btn btn-small", onclick: () => { rows.push([""]); render(); } }, "+ Pick an uploaded file") : null,
      ));
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

  function tableWidget(f, id) {
    const cols = f.fields.map((c) => ({ name: c.replace(/\?$/, ""), optional: c.endsWith("?") }));
    let rows = [];
    const wrap = el("div", { class: "table-edit", id });
    const serialize = () =>
      rows.filter((r) => r.some((c, i) => c && !(BOOL_COLUMNS.has(cols[i]?.name) && c === "no")))
        .map((r) => cols.map((_, i) => r[i] || "").join(" | ")).join("\n");
    const emit = () => onWidgetChange(f, serialize());
    const render = () => {
      const head = el("div", { class: "trow thead", style: `--cols:${cols.length}` },
        cols.map((c) => el("span", { text: c.name.replace(/_/g, " ") + (c.optional ? "" : " *") })), el("span"));
      const body = rows.map((r, ri) =>
        el("div", { class: "trow", style: `--cols:${cols.length}` },
          cols.map((c, ci) => {
            if (BOOL_COLUMNS.has(c.name)) {
              const box = el("input", { type: "checkbox", "aria-label": c.name, checked: /^yes$/i.test(r[ci] || "") });
              box.addEventListener("change", () => { r[ci] = box.checked ? "yes" : c.optional ? "" : "no"; emit(); });
              return el("label", { class: "tcheck" }, box);
            }
            const input = el("input", { class: "input", "aria-label": c.name, placeholder: c.name.replace(/_/g, " "), value: r[ci] || "" });
            input.addEventListener("input", () => { r[ci] = input.value.replace(/\|/g, "/").replace(/\n/g, " "); emit(); });
            return input;
          }),
          el("button", { type: "button", class: "icon-btn", title: "Remove", "aria-label": "Remove row", onclick: () => { rows.splice(ri, 1); render(); emit(); } }, "×"),
        ));
      const add = el("button", { type: "button", class: "btn btn-small", onclick: () => { rows.push(cols.map((c) => (BOOL_COLUMNS.has(c.name) && !c.optional ? "no" : ""))); render(); } },
        f.type === "authorlist" ? "+ Add author" : "+ Add row");
      wrap.replaceChildren(el("div", { class: "tscroll" }, head, ...body), add);
    };
    render();
    return {
      node: wrap,
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
    state.live = LiveChecks.run(state.venue.fields, values(), state.liveRules);
    renderFieldIssues();
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
    }
  }

  const BLOCKS = [
    ["live", "Checked as you type", "The form itself: required fields, options, counts, and the word, character, and keyword limits."],
    ["validate", "Checked by Validate", "These read your uploaded files: formats and sizes, page and word counts, sections, statements, figures, layout, links, references, and whether the form matches the manuscript."],
    ["ai", "Checked only with AI", "Judgements that need someone to read the files: lettering in raster figures, free-text guideline rules, whether every field says what the manuscript says. Run them with “Validate, extended with AI”."],
    ["manual", "Not checked by paperpush", "Rules in the guidelines paperpush has no check for yet. Check these yourself."],
  ];
  const MARKS = { passed: "✔", failed: "✖", warning: "▲", unchecked: "?", needs_ai: "○", not_implemented: "–", not_applicable: "·", pending: "○" };
  const STATUS_WORDS = { unchecked: "could not run", needs_ai: "needs AI", not_implemented: "check by hand" };

  // The checks of one block, each with the status to show (grey "pending"
  // until there is a result for it).
  function blockChecks(group) {
    if (group === "live") {
      return state.live.checks.map((c) => ({ ...c, group, applicable: c.status !== "not_applicable", status: !state.touched && c.status !== "not_applicable" ? "pending" : c.status }));
    }
    const results = new Map((state.results?.checks || []).map((c) => [c.id, c]));
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
    for (const r of state.results?.checks || []) if (r.group === group && !ids.has(r.id)) out.push(r);
    return out;
  }

  function checkItem(c) {
    const status = c.status;
    const cls = { passed: "st-passed", failed: "st-failed", warning: "st-warning", unchecked: "st-unchecked", not_applicable: "st-na" }[status] || "";
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
    if (state.results && state.resultsStale) {
      root.append(el("p", { class: "hint", text: "You have changed the form since the last validation. Validate again to refresh these results." }));
    } else if (state.results) {
      const ok = state.results.ok;
      const errors = state.results.checks.filter((c) => c.status === "failed").length;
      root.append(el("p", { class: `verdict ${ok ? "ok" : "bad"}`, text: ok ? "✔ No blocking errors. Read any warnings, then submit." : `✖ Not ready: ${plural(errors, "check")} failed. Fix them before submitting.` }));
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
      root.append(el("div", { class: "check-group", "data-group": group },
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
      state.plan = plan;
      state.liveRules = plan.live_rules || {};
      if (plan.requirements) state.requirements = plan.requirements;
      if (rulesChanged) {
        for (const fid of state.widgets.keys()) updateLimits(fid);
        syncWidgets(); // redraws the counters against the new limits
        runLive();
      }
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
    status.replaceChildren(el("span", { class: "spinner" }), withAi ? " Validating, with the AI checks (this can take a minute)…" : " Validating…");
    state.showRequired = true;
    try {
      const res = await postJSON(`/api/workspaces/${state.ws}/validate`, { text: state.text, ai: withAi }, withAi ? llmHeaders() : {});
      state.results = res;
      state.resultsStale = false;
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
      renderAiAvailability();
      runLive();
      renderChecks();
      $("#checks-panel").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function focusField(fid) {
    const w = state.widgets.get(fid);
    if (fid === state.venue.manuscript_field) { $("#manuscript-panel").scrollIntoView({ behavior: "smooth", block: "center" }); return; }
    if (!w) return;
    w.card.scrollIntoView({ behavior: "smooth", block: "center" });
    const target = w.card.querySelector("input, textarea, select");
    if (target) setTimeout(() => target.focus({ preventScroll: true }), 300);
  }

  // ------------------------------------------------------- bottom actions

  function downloadSub() {
    download(new Blob([state.text], { type: "text/plain" }), state.venue.subfile_name);
  }

  async function downloadBundle() {
    try {
      const res = await api(`/api/workspaces/${state.ws}/bundle`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: state.text }) });
      download(await res.blob(), `${state.venue.subfile_name.replace(/\.sub$/, "")}-submission.zip`);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // A standalone copy of the page as it is now: the form's values, the
  // requirements, and the check results, with the styles inlined and the
  // scripts left out.
  async function savePage() {
    let css = "";
    try { css = await (await api("/static/style.css")).text(); } catch { /* unstyled copy */ }
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
      copy.setAttribute("disabled", "");
    });
    $$("script, dialog, #toast, .settings, .topbar-key, .final-actions, .check-actions, .autofill, #restored, #venue-options", doc).forEach((n) => n.remove());
    $$("details", doc).forEach((d) => d.setAttribute("open", ""));
    const link = $("link[rel=stylesheet]", doc);
    if (link) link.replaceWith(Object.assign(document.createElement("style"), { textContent: css }));
    const stamp = el("p", { class: "hint", text: `Saved from paperpush ${state.info?.version || ""} on ${new Date().toLocaleString()}. To keep working on it, open ${state.venue.subfile_name} in paperpush.` });
    $("main", doc)?.prepend(stamp);
    const html = `<!doctype html>\n${doc.outerHTML}`;
    download(new Blob([html], { type: "text/html" }), `${state.venue.slug}-paperpush.html`);
  }

  function command(text) {
    const code = el("code", { text });
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
    if (!v.supports_submit) {
      body.replaceChildren(
        el("p", { text: `paperpush can check a manuscript for ${v.name}, but cannot fill its submission portal yet.` }),
        el("ol", {},
          el("li", {}, "Download your submission files. The bundle contains your .sub file and uploaded files; download the .sub alone if you already have the manuscript files.",
            el("div", { class: "cmd" },
              el("button", { type: "button", class: "btn primary btn-small", onclick: downloadBundle, text: "Download bundle (.zip)" }),
              el("button", { type: "button", class: "btn btn-small", onclick: downloadSub, text: `Download ${sub} only` }))),
          el("li", {}, "Visit ", v.site_url
            ? el("a", { href: v.site_url, target: "_blank", rel: "noopener", text: `${v.name}'s website ↗` })
            : `${v.name}'s website`, ", then complete the submission there using your files and the values in the .sub file.")),
        el("p", {}, "Want paperpush to fill this portal in the future? ",
          el("a", { href: "https://github.com/pachterlab/paperpush/discussions", target: "_blank", rel: "noopener", text: "Suggest this venue on GitHub Discussions ↗" }), "."),
      );
      $("#portal-dialog").showModal();
      return;
    }
    steps.push(el("li", {},
      el("strong", { text: "Download your submission." }), ` The bundle holds ${sub} and the files you uploaded; unzip it into a folder. (Already have the files? The .sub alone is enough if it sits next to them.)`,
      el("div", { class: "cmd" },
        el("button", { type: "button", class: "btn primary btn-small", onclick: downloadBundle, text: "Download bundle (.zip)" }),
        el("button", { type: "button", class: "btn btn-small", onclick: downloadSub, text: `Download ${sub} only` })),
    ));
    steps.push(el("li", {}, el("strong", { text: "Install paperpush" }), " (Python 3.10 or later), and the browser it drives:", command("pip install paperpush"), command("playwright install chromium")));
    if (v.login_required) {
      steps.push(el("li", {}, el("strong", { text: `Log in to ${v.name}` }), " once. paperpush keeps the login in your system keychain:", command(`paperpush login ${v.slug}`)));
    }
    steps.push(el("li", {}, el("strong", { text: "Submit" }), " from inside the unzipped folder:", command(`paperpush submit ${sub}`),
      el("p", { class: "hint", text: "paperpush validates again, opens a browser, and fills in the portal. It stops before the final submit button, so you can look everything over and press it yourself." })));
    body.replaceChildren(el("ol", {}, steps));
    $("#portal-dialog").showModal();
  }

  // ------------------------------------------------------------- routing

  async function route() {
    const m = location.hash.match(/^#\/v\/([^/]+)/);
    if (m) {
      const slug = decodeURIComponent(m[1]);
      if (!state.venue || state.venue.slug !== slug) await openVenue(slug, pendingText);
      pendingText = null;
    } else {
      state.venue = null;
      $("#workspace").hidden = true;
      $("#venue-search").value = "";
      document.title = "paperpush";
    }
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
    $("#download-sub").addEventListener("click", downloadSub);
    $("#save-page").addEventListener("click", savePage);
    $("#portal-btn").addEventListener("click", openPortalDialog);
    $("#form").addEventListener("submit", (e) => e.preventDefault());
    window.addEventListener("hashchange", route);
  }

  async function init() {
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
    const input = $("#venue-search");
    input.disabled = false;
    $("#venue-hint").textContent = `${state.venues.length} venues. Search by name, or open the list.`;
    await route();
    if (!state.venue) input.focus();
  }

  init();
})();
