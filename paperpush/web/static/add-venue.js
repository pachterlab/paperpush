// The "Add venue" page: a form for a venue file (paperpush/venuefile.py).
//
// The inputs are generated from /api/venue-file/schema -- the keys, types and
// descriptions of the venue databases' dataclasses -- so a key added there
// shows up here without editing this file. The page keeps one document shaped
// like the database entries ({slug, venue, form, requirements}); the server
// renders it as the venue file and checks it (/api/venue-file/render), so the
// file's syntax lives only in Python. Nothing is written on the server: the
// author downloads the file and runs `paperpush add-venue FILE`.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const DRAFT_KEY = "paperpush:add-venue";
  const THEME_KEY = "paperpush:theme";

  let schema = null;
  let venues = [];
  // The document being edited. formOn/reqOn keep a switched-off section's
  // values, so switching it back on restores them.
  const state = {
    slug: "", venue: {}, form: { fields: [] }, formOn: true, req: {}, reqOn: true,
    types: [], // [{name, rules: [{path: "section.key", value}], notes: []}]
    replacing: "", // the slug this was loaded from: replacing it is expected
    filename: "", // set once the author edits the file name
  };
  let lastText = "";

  // ---- small DOM helpers
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children) if (c !== null && c !== undefined && c !== false) node.append(c);
    return node;
  }
  const humanize = (name) => name.replace(/_/g, " ").replace(/\bmb\b/, "MB").replace(/\bpt\b/, "(pt)").replace(/\bmm\b/, "(mm)").replace(/\bdpi\b/, "DPI").replace(/\burl(s?)\b/, "URL$1").replace(/^./, (c) => c.toUpperCase());
  const isSet = (v) => v !== undefined && v !== "" && !(Array.isArray(v) && v.length === 0);
  let uid = 0;
  const nextId = (p) => `${p}-${++uid}`;

  function toast(message, error = false) {
    const t = $("toast");
    t.textContent = message;
    t.classList.toggle("error", error);
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { t.hidden = true; }, 3200);
  }

  // ---- one input for one key, from its spec ({name, kind, description, choices})
  // get() reads the current value, set(v) stores it (undefined = unset).
  function control(spec, get, set, opts = {}) {
    const id = opts.id || nextId(spec.name);
    const value = get();
    const changed = () => { scheduleRender(); opts.onChange && opts.onChange(); };
    let input;
    const kind = opts.kind || spec.kind;
    if (kind === "bool") {
      input = el("select", { id },
        el("option", { value: "", text: "—" }), el("option", { value: "yes", text: "yes" }), el("option", { value: "no", text: "no" }));
      input.value = value === true ? "yes" : value === false ? "no" : "";
      input.addEventListener("change", () => { set(input.value === "" ? undefined : input.value === "yes"); changed(); });
    } else if (kind === "int" || kind === "float") {
      input = el("input", { type: "number", id, step: kind === "float" ? "any" : "1", min: "0", inputmode: kind === "int" ? "numeric" : "decimal" });
      input.value = value === undefined || value === null ? "" : String(value);
      input.addEventListener("input", () => {
        const raw = input.value.trim();
        const n = kind === "int" ? parseInt(raw, 10) : parseFloat(raw);
        set(raw === "" || Number.isNaN(n) ? undefined : n);
        changed();
      });
    } else if (kind === "choice") {
      input = el("select", { id }, el("option", { value: "", text: "—" }), ...spec.choices.map((c) => el("option", { value: c, text: c })));
      input.value = value || "";
      input.addEventListener("change", () => { set(input.value || undefined); changed(); });
    } else if (kind === "list" && spec.choices && spec.choices.length) {
      const chosen = new Set(Array.isArray(value) ? value : []);
      input = el("div", { class: "av-checks", id, role: "group" });
      for (const c of spec.choices) {
        const box = el("input", { type: "checkbox", value: c });
        box.checked = chosen.has(c);
        box.addEventListener("change", () => {
          if (box.checked) chosen.add(c); else chosen.delete(c);
          set(chosen.size ? spec.choices.filter((x) => chosen.has(x)) : undefined);
          changed();
        });
        input.append(el("label", {}, box, c.replace(/_/g, " ")));
      }
    } else if (kind === "list" && opts.inline) {
      input = el("input", { type: "text", id, spellcheck: "false", placeholder: opts.placeholder || "Comma-separated" });
      input.value = Array.isArray(value) ? value.join(", ") : "";
      input.addEventListener("input", () => {
        const items = input.value.split(",").map((x) => x.trim()).filter(Boolean);
        set(items.length ? items : undefined);
        changed();
      });
    } else if (kind === "list") {
      const lines = Array.isArray(value) ? value : [];
      input = el("textarea", { id, rows: String(Math.min(8, Math.max(2, lines.length + 1))), spellcheck: spec.name === "notes" ? "true" : "false", placeholder: opts.placeholder || "One per line" });
      input.value = lines.join("\n");
      input.addEventListener("input", () => {
        const items = input.value.split("\n").map((s) => s.trim()).filter(Boolean);
        set(items.length ? items : undefined);
        changed();
      });
    } else if (kind === "json") {
      input = el("textarea", { id, rows: "3", class: "mono", spellcheck: "false", placeholder: "JSON" });
      input.value = value === undefined ? "" : JSON.stringify(value);
      input.addEventListener("input", () => {
        const raw = input.value.trim();
        if (!raw) { input.classList.remove("invalid"); set(undefined); changed(); return; }
        try { set(JSON.parse(raw)); input.classList.remove("invalid"); changed(); } catch { input.classList.add("invalid"); }
      });
    } else if (kind === "date") {
      input = el("input", { type: "date", id });
      input.value = value || "";
      input.addEventListener("input", () => { set(input.value || undefined); changed(); });
    } else {
      const long = opts.long || (schema.long_text || []).includes(spec.name);
      input = long ? el("textarea", { id, rows: "2" }) : el("input", { type: spec.name.endsWith("url") || spec.name === "submission_guide" ? "url" : "text", id, spellcheck: "false" });
      if (opts.placeholder) input.placeholder = opts.placeholder;
      if (opts.list) input.setAttribute("list", opts.list);
      input.value = value === undefined || value === null ? "" : String(value);
      input.addEventListener("input", () => { set(input.value.trim() === "" ? undefined : input.value); changed(); });
    }
    if (input.tagName !== "DIV") input.classList.add("input");
    return input;
  }

  // A labelled row: name, input, description.
  function row(spec, get, set, opts = {}) {
    const id = nextId(spec.name);
    const input = control(spec, get, set, { ...opts, id });
    const label = el("label", { class: "name", for: id }, opts.label || humanize(spec.name));
    if (opts.required) label.append(el("span", { class: "req", text: "*" }));
    const wide = opts.wide || input.tagName === "TEXTAREA" || input.classList.contains("av-checks");
    // Long descriptions show their first sentence; the rest is on hover.
    let hint = opts.hint || spec.description || "";
    if (!opts.hint && opts.short !== false) {
      const first = hint.split(/(?<=\.)\s+(?=[A-Z])/)[0];
      if (first.length < hint.length) { label.title = hint; hint = first; }
    }
    return el("div", { class: "av-row" + (wide ? " wide" : "") }, label, input, hint && !opts.noHint ? el("p", { class: "hint", text: hint }) : null);
  }

  const objSetter = (obj, key) => [() => obj[key], (v) => { if (v === undefined) delete obj[key]; else obj[key] = v; }];

  // ---- 1. venue
  function renderVenue() {
    const box = $("venue-fields");
    box.replaceChildren();
    for (const spec of schema.venue) {
      if (spec.name === "deprecated" && !state.venue.deprecated) continue;
      if (spec.name === "slug") {
        box.append(row(spec, () => state.slug, (v) => { state.slug = (v || "").trim(); syncFilename(); }, { required: true, placeholder: "e.g. nature_genetics", onChange: checkSlug }));
        continue;
      }
      const [get, set] = objSetter(state.venue, spec.name);
      const placeholder = { name: "e.g. Nature Genetics", full_name: "e.g. Nature Genetics (Springer Nature)", site_url: "https://", submission_guide: "https://" }[spec.name];
      box.append(row(spec, get, set, { placeholder, wide: spec.name === "description" }));
    }
    checkSlug();
  }
  function checkSlug() {
    const input = $("venue-fields").querySelector("input");
    if (input) input.classList.toggle("invalid", !!state.slug && !/^[a-z][a-z0-9_]*$/.test(state.slug));
  }

  // ---- 2. submission form
  const FIELD_MAIN = ["label", "type", "required"];
  const FIELD_BODY = ["help", "options", "accept", "fields", "section", "autofill"];
  const fieldSpec = (name) => schema.field.find((s) => s.name === name);

  function renderForm() {
    $("form-on").checked = state.formOn;
    $("form-body").hidden = !state.formOn;
    const box = $("form-fields");
    box.replaceChildren();
    for (const spec of schema.form) {
      if (spec.name === "include") continue;
      if (spec.name === "removed_fields" && !state.form.inherits && !state.form.removed_fields) continue;
      const [get, set] = objSetter(state.form, spec.name);
      const opts = spec.name === "inherits" ? { list: "venue-list", placeholder: "e.g. nature (an inheriting form lists only its changes)" } : {};
      box.append(row(spec, get, set, opts));
    }
    renderFields();
  }

  function renderFields() {
    const list = $("field-list");
    list.replaceChildren(...state.form.fields.map((f, i) => fieldCard(f, i)));
    $("field-count").textContent = `(${state.form.fields.length})`;
    renderAddPicker();
    renderTypeFieldNote();
  }

  function fieldCard(f, index) {
    const common = schema.common_fields.includes(f.id);
    const card = el("div", { class: "card av-field" });
    const fields = state.form.fields;
    const move = (delta) => {
      const j = index + delta;
      if (j < 0 || j >= fields.length) return;
      [fields[index], fields[j]] = [fields[j], fields[index]];
      renderFields();
      scheduleRender();
    };
    const idInput = el("input", { type: "text", class: "input mono av-id", value: f.id, spellcheck: "false", "aria-label": "Field id" });
    idInput.addEventListener("input", () => { f.id = idInput.value.trim(); scheduleRender(); });
    idInput.addEventListener("change", () => renderFields());
    const head = el("div", { class: "av-field-head" },
      idInput,
      el("span", { class: "badge" + (common ? "" : " cond"), text: common ? "common id" : "venue-specific", title: common ? "paperpush already places this id in the .sub" : "give it a section below" }),
      el("span", { class: "av-grow" }),
      el("button", { type: "button", class: "icon-btn", title: "Move up", "aria-label": "Move up", text: "↑", onclick: () => move(-1), disabled: index === 0 }),
      el("button", { type: "button", class: "icon-btn", title: "Move down", "aria-label": "Move down", text: "↓", onclick: () => move(1), disabled: index === fields.length - 1 }),
      el("button", { type: "button", class: "icon-btn", title: "Remove this field", "aria-label": "Remove this field", text: "✕", onclick: () => { fields.splice(index, 1); renderFields(); scheduleRender(); } }),
    );
    card.append(head);

    const main = el("div", { class: "av-grid av-field-main" });
    for (const name of FIELD_MAIN) {
      const [get, set] = objSetter(f, name);
      const onChange = name === "type" ? () => { renderFields(); } : undefined;
      main.append(row(fieldSpec(name), get, set, { required: name !== "required", noHint: true, onChange }));
    }
    card.append(main);

    const body = el("div", { class: "av-grid" });
    const type = f.type || "";
    for (const name of FIELD_BODY) {
      // Shown when the field's type uses them, or when already set.
      const shown = {
        options: ["choice", "multichoice"].includes(type),
        accept: ["file", "filelist"].includes(type),
        fields: type === "authorlist",
        section: !common,
      };
      if (name in shown && !shown[name] && !isSet(f[name])) continue;
      const spec = fieldSpec(name);
      const [get, set] = objSetter(f, name);
      const opts = {
        help: { long: true, wide: true, noHint: true },
        options: { placeholder: "One option per line, exactly as the portal shows it", wide: true, onChange: renderTypeFieldNote },
        accept: { inline: true, placeholder: ".pdf, .docx" },
        fields: { inline: true, wide: true, placeholder: "name, email, affiliation?, corresponding", hint: "The columns of each line, in order, separated by | in the .sub. A trailing ? marks an optional column." },
        autofill: { hint: "How autofill may fill it: extract from the manuscript, classify into an option, filemap a file, or never (left to the author)." },
      }[name] || {};
      if (name === "section") opts.hint = common ? spec.description : "Which part of the .sub this field is listed under. This id is not one paperpush already places, so pick one (otherwise: additional).";
      body.append(row(spec, get, set, opts));
    }
    card.append(body);

    const rest = schema.field.filter((s) => !FIELD_MAIN.includes(s.name) && !FIELD_BODY.includes(s.name));
    const setCount = rest.filter((s) => isSet(f[s.name])).length;
    const more = el("details", { class: "av-more" }, el("summary", { text: `More settings${setCount ? ` (${setCount} set)` : ""}` }));
    const moreGrid = el("div", { class: "av-grid" });
    for (const spec of rest) {
      const [get, set] = objSetter(f, spec.name);
      let kind;
      if (spec.name === "default") kind = type === "boolean" ? "bool" : type === "int" ? "int" : ["multichoice", "filelist"].includes(type) ? "list" : "str";
      moreGrid.append(row(spec, get, set, { kind }));
    }
    more.append(moreGrid);
    card.append(more);
    return card;
  }

  function renderAddPicker() {
    const used = new Set(state.form.fields.map((f) => f.id));
    const presets = schema.presets.filter((p) => !used.has(p.id));
    const others = schema.common_fields.filter((id) => !used.has(id) && !schema.presets.some((p) => p.id === id));
    const pick = $("add-field-pick");
    pick.replaceChildren(
      el("option", { value: "", text: "A new venue-specific field" }),
      presets.length ? el("optgroup", { label: "Common fields" }, ...presets.map((p) => el("option", { value: p.id, text: `${p.label} (${p.id})` }))) : null,
      others.length ? el("optgroup", { label: "Other ids paperpush places" }, ...others.map((id) => el("option", { value: id, text: id }))) : null,
    );
  }

  function addField() {
    const id = $("add-field-pick").value;
    let field;
    if (!id) {
      let n = 1;
      while (state.form.fields.some((f) => f.id === `new_field_${n}`)) n++;
      field = { id: `new_field_${n}`, label: "", type: "text", section: "additional" };
    } else {
      const preset = schema.presets.find((p) => p.id === id);
      field = preset ? JSON.parse(JSON.stringify(preset)) : { id, label: humanize(id), type: "text" };
    }
    state.form.fields.push(field);
    renderFields();
    scheduleRender();
    const cards = $("field-list").children;
    const card = cards[cards.length - 1];
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    (id ? card.querySelector("input[type=text]:not(.av-id)") : card.querySelector(".av-id"))?.focus({ preventScroll: true });
  }

  // ---- 3. author guidelines
  function renderReq() {
    $("req-on").checked = state.reqOn;
    $("req-body").hidden = !state.reqOn;
    const box = $("req-fields");
    box.replaceChildren();
    for (const spec of schema.requirements) {
      if (spec.name === "include") continue;
      const [get, set] = objSetter(state.req, spec.name);
      const opts = {
        source_urls: { placeholder: "https://… one guideline page per line" },
        retrieved: { kind: "date" },
        inherits: { list: "venue-list", placeholder: "e.g. science (start from that venue's rules)" },
        article_type_field: { placeholder: "article_type", onChange: renderTypeFieldNote },
        notes: { placeholder: "One rule per line" },
      }[spec.name] || {};
      box.append(row(spec, get, set, opts));
    }
    const sections = $("req-sections");
    sections.replaceChildren();
    for (const [name, sec] of Object.entries(schema.sections)) {
      const values = state.req[name] && typeof state.req[name] === "object" ? state.req[name] : (state.req[name] = {});
      const count = () => Object.values(values).filter(isSet).length;
      const counter = el("span", { class: "muted small" });
      const refresh = () => { counter.textContent = count() ? `${count()} set` : ""; };
      refresh();
      const det = el("details", { class: "av-rsec" }, el("summary", {}, el("b", { text: humanize(name) }), el("span", { class: "muted small", text: sec.doc.replace(/\.$/, "") }), counter));
      const grid = el("div", { class: "av-grid" });
      for (const spec of sec.keys) {
        const [get, set] = objSetter(values, spec.name);
        grid.append(row(spec, get, set, { onChange: refresh, placeholder: spec.name === "notes" ? "One rule per line" : undefined }));
      }
      det.append(grid);
      if (count()) det.open = true;
      sections.append(det);
    }
  }

  // ---- 4. article types
  function rulePaths() {
    const out = [];
    for (const [name, sec] of Object.entries(schema.sections)) for (const spec of sec.keys) out.push({ path: `${name}.${spec.name}`, section: name, spec });
    return out;
  }
  function typeOptions() {
    const fid = state.req.article_type_field;
    const field = fid && state.form.fields.find((f) => f.id === fid);
    return field && Array.isArray(field.options) ? field.options : [];
  }
  function renderTypeFieldNote() {
    const fid = state.req.article_type_field;
    const opts = typeOptions();
    const note = $("type-field-note");
    if (!fid) note.textContent = "set the article-type field under Author guidelines first (usually article_type).";
    else if (!state.formOn) note.textContent = `“${fid}”.`;
    else if (!opts.length) note.textContent = `“${fid}”, which has no options yet.`;
    else note.textContent = opts.map((o) => `“${o}”`).join(", ") + ".";
    const dl = $("type-names") || document.body.appendChild(el("datalist", { id: "type-names" }));
    dl.replaceChildren(...opts.map((o) => el("option", { value: o })));
  }

  function renderTypes() {
    $("type-list").replaceChildren(...state.types.map((t, i) => typeCard(t, i)));
    renderTypeFieldNote();
  }

  function typeCard(t, index) {
    const card = el("div", { class: "card av-type" });
    const name = el("input", { type: "text", class: "input", value: t.name, list: "type-names", placeholder: "e.g. Brief Communication", "aria-label": "Article type name" });
    name.addEventListener("input", () => { t.name = name.value; scheduleRender(); });
    card.append(el("div", { class: "av-field-head" }, name,
      el("button", { type: "button", class: "icon-btn", title: "Remove this article type", "aria-label": "Remove this article type", text: "✕", onclick: () => { state.types.splice(index, 1); renderTypes(); scheduleRender(); } })));
    const paths = rulePaths();
    const rules = el("div", { class: "av-rules" });
    const drawRules = () => {
      rules.replaceChildren(...t.rules.map((r, i) => {
        const pick = el("select", { class: "input", "aria-label": "Rule" },
          el("option", { value: "", text: "Choose a rule…" }),
          ...Object.keys(schema.sections).map((sec) => el("optgroup", { label: humanize(sec) },
            ...paths.filter((p) => p.section === sec).map((p) => el("option", { value: p.path, text: `${humanize(sec)}: ${p.spec.name.replace(/_/g, " ")}` })))));
        pick.value = r.path || "";
        pick.addEventListener("change", () => { r.path = pick.value; r.value = undefined; drawRules(); scheduleRender(); });
        const found = paths.find((p) => p.path === r.path);
        const value = found ? control(found.spec, () => r.value, (v) => { r.value = v; }) : el("span");
        if (found) value.setAttribute("aria-label", `${humanize(found.section)}: ${humanize(found.spec.name)}`);
        const del = el("button", { type: "button", class: "icon-btn", title: "Remove this rule", "aria-label": "Remove this rule", text: "✕", onclick: () => { t.rules.splice(i, 1); drawRules(); scheduleRender(); } });
        return el("div", { class: "av-rule" }, pick, value, del);
      }));
    };
    drawRules();
    card.append(rules, el("button", { type: "button", class: "btn btn-small", text: "+ Add a rule", onclick: () => { t.rules.push({ path: "", value: undefined }); drawRules(); } }));
    card.append(row({ name: "notes", kind: "list", description: "This type's rules with no rule of their own, one per line." }, () => t.notes, (v) => { t.notes = v || []; }));
    return card;
  }

  // ---- the document <-> the page
  function prune(obj) {
    const out = {};
    for (const [k, v] of Object.entries(obj || {})) {
      if (v === undefined || v === "") continue;
      if (v !== null && typeof v === "object" && !Array.isArray(v)) {
        const inner = prune(v);
        if (Object.keys(inner).length) out[k] = inner;
      } else out[k] = v;
    }
    return out;
  }

  function toDoc() {
    let requirements = null;
    if (state.reqOn) {
      requirements = prune(state.req);
      const types = {};
      for (const t of state.types) {
        const name = (t.name || "").trim();
        if (!name) continue;
        const rules = {};
        for (const r of t.rules) {
          if (!r.path || r.value === undefined || r.value === "") continue;
          const [sec, key] = r.path.split(".");
          (rules[sec] = rules[sec] || {})[key] = r.value;
        }
        if (t.notes && t.notes.length) rules.notes = t.notes;
        types[name] = rules;
      }
      if (Object.keys(types).length) requirements.article_types = types;
    }
    return {
      slug: state.slug,
      venue: prune(state.venue),
      form: state.formOn ? { ...prune({ ...state.form, fields: undefined }), fields: state.form.fields.map((f) => ({ id: f.id, ...prune({ ...f, id: undefined }) })) } : null,
      requirements,
    };
  }

  function loadDoc(doc, { replacing = "", formOn, reqOn } = {}) {
    state.slug = doc.slug || "";
    state.venue = { ...(doc.venue || {}) };
    state.formOn = formOn ?? !!doc.form;
    state.form = doc.form ? JSON.parse(JSON.stringify(doc.form)) : { fields: JSON.parse(JSON.stringify(schema.presets)) };
    state.form.fields = state.form.fields || [];
    state.reqOn = reqOn ?? !!doc.requirements;
    const req = doc.requirements ? JSON.parse(JSON.stringify(doc.requirements)) : { retrieved: new Date().toISOString().slice(0, 10) };
    state.types = Object.entries(req.article_types || {}).map(([name, rules]) => ({
      name,
      notes: (rules && rules.notes) || [],
      rules: Object.entries(rules || {}).filter(([k]) => k !== "notes").flatMap(([sec, values]) =>
        values && typeof values === "object" ? Object.entries(values).map(([key, value]) => ({ path: `${sec}.${key}`, value })) : []),
    }));
    delete req.article_types;
    state.req = req;
    state.replacing = replacing;
    state.filename = "";
    renderAll();
  }

  function renderAll() {
    renderVenue();
    renderForm();
    renderReq();
    renderTypes();
    syncFilename();
    scheduleRender(0);
  }

  // ---- the venue file: rendered and checked by the server
  function syncFilename() {
    if (!state.filename) $("filename").value = `${state.slug || "venue"}.txt`;
    const force = state.replacing && state.slug === state.replacing ? " --force" : "";
    $("cmd").textContent = `paperpush add-venue ${$("filename").value || "venue.txt"}${force}`;
  }

  let renderTimer = null;
  let renderSeq = 0;
  function scheduleRender(delay = 350) {
    clearTimeout(renderTimer);
    $("preview-state").textContent = "checking…";
    renderTimer = setTimeout(renderFile, delay);
    saveDraft();
  }

  async function renderFile() {
    const seq = ++renderSeq;
    syncFilename();
    try {
      const res = await fetch("/api/venue-file/render", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ doc: toDoc(), filename: $("filename").value || "venue.txt", replacing: !!state.replacing && state.slug === state.replacing }),
      });
      const data = await res.json();
      if (seq !== renderSeq) return;
      if (!res.ok) throw new Error(data.detail || res.statusText);
      lastText = data.text;
      $("preview").textContent = data.text;
      showIssues(data.errors, data.warnings);
      $("preview-state").textContent = "";
    } catch (err) {
      if (seq !== renderSeq) return;
      $("preview-state").textContent = "";
      showIssues([`Could not check the file: ${err.message}`], []);
    }
  }

  function showIssues(errors, warnings) {
    const verdict = $("verdict");
    if (errors.length) {
      verdict.className = "verdict bad";
      verdict.textContent = `${errors.length} problem${errors.length > 1 ? "s" : ""} to fix before paperpush can add it`;
    } else if (warnings.length) {
      verdict.className = "verdict partial";
      verdict.textContent = `Ready to add, with ${warnings.length} note${warnings.length > 1 ? "s" : ""}`;
    } else {
      verdict.className = "verdict ok";
      verdict.textContent = "Ready to add";
    }
    $("issues").replaceChildren(
      ...errors.map((e) => el("li", { class: "error", text: e })),
      ...warnings.map((w) => el("li", { class: "warning", text: w })),
    );
  }

  // ---- the draft, kept in this browser so a reload does not lose work
  function saveDraft() {
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify({ doc: toDoc(), formOn: state.formOn, reqOn: state.reqOn, form: state.form, req: state.req, types: state.types, replacing: state.replacing, filename: state.filename }));
    } catch { /* storage unavailable */ }
  }
  function readDraft() {
    try { return JSON.parse(localStorage.getItem(DRAFT_KEY) || "null"); } catch { return null; }
  }

  // ---- start from: an existing venue, a file, or nothing
  function resolveVenue(text) {
    const q = text.trim().toLowerCase();
    return venues.find((v) => v.slug === q || v.name.toLowerCase() === q || v.full_name.toLowerCase() === q)
      || venues.find((v) => `${v.name} (${v.slug})`.toLowerCase() === q);
  }

  async function startFrom(slug) {
    const res = await fetch(`/api/venue-file/start${slug ? `?from=${encodeURIComponent(slug)}` : ""}`);
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const doc = await res.json();
    loadDoc(doc, slug ? { replacing: doc.slug } : { formOn: true, reqOn: true });
  }

  function wire() {
    $("form-on").addEventListener("change", (e) => { state.formOn = e.target.checked; $("form-body").hidden = !state.formOn; renderTypeFieldNote(); scheduleRender(); });
    $("req-on").addEventListener("change", (e) => { state.reqOn = e.target.checked; $("req-body").hidden = !state.reqOn; scheduleRender(); });
    $("add-field").addEventListener("click", addField);
    $("add-type").addEventListener("click", () => {
      const used = new Set(state.types.map((t) => t.name));
      const name = typeOptions().find((o) => !used.has(o)) || "";
      state.types.push({ name, rules: [{ path: "", value: undefined }], notes: [] });
      renderTypes();
      scheduleRender();
    });
    $("filename").addEventListener("input", () => { state.filename = $("filename").value.trim(); syncFilename(); saveDraft(); });
    $("load-venue").addEventListener("click", async () => {
      const v = resolveVenue($("from-venue").value);
      if (!v) { $("start-status").textContent = "Pick a venue from the list."; return; }
      try {
        await startFrom(v.slug);
        $("start-status").textContent = `Loaded ${v.name}'s current entries. Change what is wrong and keep the slug to replace them, or change the slug to add a sister venue.`;
      } catch (err) { toast(`Could not load ${v.name}: ${err.message}`, true); }
    });
    $("from-venue").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("load-venue").click(); } });
    $("open-file").addEventListener("change", async (e) => {
      const file = e.target.files[0];
      e.target.value = "";
      if (!file) return;
      const text = await file.text();
      const res = await fetch("/api/venue-file/parse", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text }) });
      const data = await res.json();
      if (!res.ok) { toast(data.detail || "Could not read the file", true); return; }
      if (!data.doc) {
        $("start-status").textContent = "";
        showIssues(data.problems.map((p) => `${file.name}: ${p}`), []);
        toast(`${file.name} has ${data.problems.length} problem(s); see the list on the right.`, true);
        return;
      }
      const existing = venues.some((v) => v.slug === data.doc.slug);
      loadDoc(data.doc, { replacing: existing ? data.doc.slug : "" });
      state.filename = file.name;
      $("filename").value = file.name;
      syncFilename();
      $("start-status").textContent = `Opened ${file.name}.`;
    });
    $("start-over").addEventListener("click", async () => {
      try { localStorage.removeItem(DRAFT_KEY); } catch { /* storage unavailable */ }
      $("from-venue").value = "";
      await startFrom("");
      $("start-status").textContent = "Started a blank venue.";
    });
    $("download").addEventListener("click", async () => {
      await renderFile();
      const name = ($("filename").value || "venue.txt").replace(/[\\/]/g, "_");
      const url = URL.createObjectURL(new Blob([lastText], { type: "text/plain" }));
      const a = el("a", { href: url, download: name });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    $("copy").addEventListener("click", async () => {
      await renderFile();
      try { await navigator.clipboard.writeText(lastText); toast("Copied the venue file."); } catch { toast("Could not copy; use the preview instead.", true); }
    });

    // Light and dark, as on the main page.
    const setTheme = (t, remember) => {
      document.documentElement.dataset.theme = t;
      if (remember) { try { localStorage.setItem(THEME_KEY, t); } catch { /* storage unavailable */ } }
      const label = t === "dark" ? "Switch to light mode" : "Switch to dark mode";
      $("theme-toggle").title = label;
      $("theme-toggle").setAttribute("aria-label", label);
    };
    setTheme(document.documentElement.dataset.theme || "light");
    $("theme-toggle").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true));
  }

  async function init() {
    wire();
    const [s, v] = await Promise.all([fetch("/api/venue-file/schema").then((r) => r.json()), fetch("/api/venues").then((r) => r.json())]);
    schema = s;
    venues = v;
    $("venue-list").replaceChildren(...venues.map((x) => el("option", { value: x.slug, label: `${x.name} — ${x.full_name}` })));
    const draft = readDraft();
    if (draft && draft.doc) {
      loadDoc(draft.doc, { replacing: draft.replacing || "", formOn: draft.formOn, reqOn: draft.reqOn });
      // Switched-off sections keep what they held.
      if (!draft.formOn && draft.form) { state.form = draft.form; renderForm(); }
      if (!draft.reqOn && draft.req) { state.req = draft.req; renderReq(); }
      if (draft.types) { state.types = draft.types; renderTypes(); }
      if (draft.filename) { state.filename = draft.filename; $("filename").value = draft.filename; syncFilename(); }
      $("start-status").textContent = "Restored your draft from this browser.";
    } else {
      await startFrom("");
    }
  }

  init().catch((err) => toast(`Could not load the page: ${err.message}`, true));
})();
