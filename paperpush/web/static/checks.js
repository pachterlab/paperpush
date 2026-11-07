// The checks the page runs as the author types.
//
// A port of the form-level checks in paperpush/validate.py (_schema_issues,
// _field_checks, _check_length, _check_url, _check_authors, _check_subfields) and of
// requirements_check._text_fields_check, which compares the title, abstract,
// and keywords against the author-guideline limits the server sends as
// `liveRules` (see paperpush/web/app.py:live_rules). The ids and titles match
// the checks `paperpush validate` reports, so the page can show either
// result in the same place. A venue's own field validators (Nature's subject
// tree, ...) exist only in Python: the server reports what they find as the
// plan's `schema_issues`, passed in here as `serverIssues`. tests/test_web.py runs these against the Python
// versions; change both together.
(function (root) {
  "use strict";

  const TRUE = new Set(["yes", "y", "true", "1", "on"]);
  const FALSE = new Set(["no", "n", "false", "0", "off"]);
  const DEFAULT_AUTHOR_FIELDS = ["name", "email?", "affiliation?", "orcid?", "corresponding"];
  const AUTHOR_SPECIAL = new Set(["name", "first_name", "last_name", "open_review_id", "corresponding"]);

  const TITLES = {
    "form.schema": ["Field values have the right type and known field ids", "Submission form"],
    "form.required": ["Required fields are filled and confirmations given", "Submission form"],
    "form.options": ["Choices are valid options", "Submission form"],
    "form.counts": ["Number of entries in list fields (keywords, authors, suggested reviewers, …) within the form's limits", "Submission form"],
    "form.lengths": ["Text within the form's word/character limits", "Submission form"],
    "form.urls": ["Link fields hold http(s) URLs", "Submission form"],
    "form.authors": ["Author list is complete (names, corresponding author, emails)", "Submission form"],
    "form.subfields": ["Structured entries (funding, reviewers, ...) are complete", "Submission form"],
    "guidelines.text_fields": ["Title, abstract, and keywords within the guidelines' limits", "Guidelines"],
  };

  const truthy = (v) => TRUE.has(v.trim().toLowerCase());
  const falsey = (v) => FALSE.has(v.trim().toLowerCase());
  const words = (v) => (v.trim().match(/\S+/g) || []).length;
  const nonEmptyLines = (v) => v.split(/\r\n|\r|\n/).map((l) => l.trim()).filter(Boolean);
  const issue = (level, field, message) => ({ level, field, message });

  function specs(fields) {
    return fields.map((c) => (c.endsWith("?") ? [c.slice(0, -1), false] : [c, true]));
  }

  function parseAuthors(value, fields) {
    const names = specs(fields || DEFAULT_AUTHOR_FIELDS).map(([n]) => n);
    return nonEmptyLines(value).map((line) => {
      const parts = line.split("|").map((p) => p.trim());
      const out = {};
      names.forEach((n, i) => (out[n] = n === "corresponding" ? truthy(parts[i] || "") : parts[i] || ""));
      return out;
    });
  }

  function authorName(a) {
    if (a.name) return a.name.trim();
    return [a.first_name || "", a.last_name || ""].map((s) => s.trim()).filter(Boolean).join(" ");
  }

  function countItems(field, raw) {
    if (field.type === "multichoice") return raw.split(",").filter((c) => c.trim()).length;
    return nonEmptyLines(raw).length;
  }

  function isHttpUrl(value) {
    try {
      const u = new URL(value.trim());
      return (u.protocol === "http:" || u.protocol === "https:") && Boolean(u.host);
    } catch {
      return false;
    }
  }

  function checkLength(field, raw) {
    const out = [];
    if (field.word_count != null) {
      const w = words(raw);
      if (w > field.word_count) out.push(issue("error", field.id, `${field.label}: ${w} words exceeds the ${field.word_count}-word limit`));
    }
    const chars = raw.trim().length;
    if (field.character_count != null && chars > field.character_count) {
      out.push(issue("error", field.id, `${field.label}: ${chars} characters exceeds the ${field.character_count}-character limit`));
    }
    if (field.min_character_count != null && chars < field.min_character_count) {
      out.push(issue("error", field.id, `${field.label}: ${chars} characters is under the ${field.min_character_count}-character minimum`));
    }
    return out;
  }

  function checkUrl(field, raw) {
    const out = [];
    raw.split(/\r\n|\r|\n/).forEach((line, i) => {
      line = line.trim();
      if (line && !isHttpUrl(line)) out.push(issue("error", field.id, `${field.label}: line ${i + 1} '${line}' is not a valid http/https URL`));
    });
    return out;
  }

  function checkAuthors(field, raw) {
    const out = [];
    const authors = parseAuthors(raw, field.fields);
    if (!authors.length) return [issue("error", field.id, "at least one author is required")];
    const sp = specs(field.fields || DEFAULT_AUTHOR_FIELDS);
    const columns = sp.map(([n]) => n);
    const hasCorresponding = columns.includes("corresponding");
    if (hasCorresponding) {
      const corr = authors.filter((a) => a.corresponding);
      if (!corr.length) out.push(issue("error", field.id, "no corresponding author marked (set the last field to 'yes')"));
      else if (corr.length > 1) out.push(issue("error", field.id, `${corr.length} corresponding authors marked: ${corr.map(authorName).join(", ")} (mark exactly one)`));
    }
    const requiredCols = sp.filter(([n, req]) => req && !AUTHOR_SPECIAL.has(n)).map(([n]) => n);
    const emailAll = requiredCols.includes("email");
    for (const a of authors) {
      if (!authorName(a) && !(a.open_review_id || "").trim()) out.push(issue("error", field.id, "an author line is missing a name"));
      const who = authorName(a) || "an author";
      for (const col of requiredCols) {
        if (!String(a[col] || "").trim()) out.push(issue("error", field.id, `author '${who}' is missing ${col.replace(/_/g, " ")}`));
      }
    }
    if (hasCorresponding && !emailAll && columns.includes("email")) {
      for (const a of authors) if (a.corresponding && !a.email) out.push(issue("error", field.id, `corresponding author '${authorName(a)}' has no email`));
    }
    return out;
  }

  function checkSubfields(field, raw) {
    const required = specs(field.fields || []).map(([n, req], pos) => [pos, n, req]).filter(([, , req]) => req);
    if (!required.length) return [];
    const out = [];
    nonEmptyLines(raw).forEach((line, i) => {
      const cells = line.split("|").map((c) => c.trim());
      for (const [pos, name] of required) {
        if (!(cells[pos] || "")) out.push(issue("error", field.id, `${field.label}: line ${i + 1} is missing ${name.replace(/_/g, " ")}`));
      }
    });
    return out;
  }

  // The guideline limits on the title, abstract, and keywords (`rules` is
  // live_rules[field.id] from the server).
  function checkGuidelineText(field, raw, rules) {
    const out = [];
    const value = raw.trim();
    if (!value || !rules) return out;
    const w = words(value);
    const where = "in the author guidelines";
    if (rules.max_words != null && w > rules.max_words) out.push(issue("error", field.id, `${field.label}: ${w} words exceeds the ${rules.max_words}-word limit ${where}`));
    if (rules.min_words != null && w < rules.min_words) out.push(issue("warning", field.id, `${field.label}: ${w} words is under the ${rules.min_words}-word minimum ${where}`));
    if (rules.max_characters != null && value.length > rules.max_characters) out.push(issue("error", field.id, `${field.label}: ${value.length} characters exceeds the ${rules.max_characters}-character limit ${where}`));
    // A structured abstract and citations in the abstract are checked on the
    // server (front_matter_check.py), against the manuscript as well.
    if (rules.min_keywords != null || rules.max_keywords != null) {
      const n = value.split(/[,;\n]/).filter((k) => k.trim()).length;
      if (rules.min_keywords != null && n < rules.min_keywords) out.push(issue("error", field.id, `${field.label}: ${n} keywords given; the author guidelines require at least ${rules.min_keywords}`));
      if (rules.max_keywords != null && n > rules.max_keywords) out.push(issue("error", field.id, `${field.label}: ${n} keywords given; the author guidelines allow at most ${rules.max_keywords}`));
    }
    return out;
  }

  // The generic part of validate.py's pydantic schema: known field ids, closed
  // choices, yes/no answers, whole numbers within bounds, and filelist types.
  // `filled` holds the stripped non-blank values.
  function checkSchema(fields, filled, venue) {
    const out = [];
    const byId = new Map(fields.map((f) => [f.id, f]));
    for (const [id, value] of Object.entries(filled)) {
      const field = byId.get(id);
      if (!field) {
        out.push(issue("error", id, `unknown field '${id}' is not part of the ${venue || "venue's"} template; remove it or fix the spelling`));
        continue;
      }
      const label = field.label;
      if (field.type === "choice" && field.options && field.options.length && !field.options_recommended) {
        if (!field.options.includes(value)) out.push(issue("error", id, `${label}: '${value}' is not a valid option; choose one of: ${field.options.join(", ")}`));
      } else if (field.type === "boolean") {
        if (!truthy(value) && !falsey(value)) out.push(issue("error", id, `${label}: expected a yes/no value (e.g. yes, no, true, false)`));
      } else if (field.type === "int") {
        if (!/^[+-]?\d+(?:_\d+)*$/.test(value)) out.push(issue("error", id, `${label}: '${value}' is not a whole number`));
        else {
          const n = Number(value.replace(/_/g, ""));
          if (field.min_value != null && n < field.min_value) out.push(issue("error", id, `${label}: must be at least ${field.min_value} (got ${n})`));
          else if (field.max_value != null && n > field.max_value) out.push(issue("error", id, `${label}: must be at most ${field.max_value} (got ${n})`));
        }
      } else if (field.type === "filelist" && field.type_options && field.type_options.length) {
        const allowed = new Set(field.type_options.map((o) => o.toLowerCase()));
        const lines = value.split(/\r\n|\r|\n/);
        for (let i = 0; i < lines.length; i++) {
          const line = lines[i].trim();
          if (!line) continue;
          const parts = line.split("|").map((x) => x.trim());
          if (parts.length >= 2 && parts[1] && !allowed.has(parts[1].toLowerCase())) {
            out.push(issue("error", id, `${label}: line ${i + 1}: file type '${parts[1]}' is not allowed; choose one of: ${field.type_options.join(", ")}`));
            break; // validate.py stops at the first bad line
          }
        }
      }
    }
    return out;
  }

  function statusFor(issues) {
    if (issues.some((i) => i.level === "error")) return "failed";
    if (issues.length) return "warning";
    return "passed";
  }

  // Run every live check. Returns {checks: [{id, title, category, status,
  // issues}], byField: {fieldId: [issue, ...]}}. `opts.venue` names the venue
  // in messages; `opts.serverIssues` are the venue validators' findings.
  function run(fields, values, liveRules = {}, opts = {}) {
    const buckets = {};
    for (const id of Object.keys(TITLES)) buckets[id] = [];
    const applied = new Set(["form.required"]);
    const byId = new Map(fields.map((f) => [f.id, f]));

    const filled = {};
    for (const [k, v] of Object.entries(values)) if (v && v.trim()) filled[k] = v.trim();
    if (Object.keys(filled).length) {
      applied.add("form.schema");
      buckets["form.schema"].push(...checkSchema(fields, filled, opts.venue));
      // A field the generic schema already rejects is not run through the venue's validator.
      const flagged = new Set(buckets["form.schema"].map((i) => i.field));
      for (const i of opts.serverIssues || []) {
        if (filled[i.field] && !flagged.has(i.field)) buckets["form.schema"].push(issue(i.level || "error", i.field, i.message));
      }
    }

    for (const field of fields) {
      const raw = values[field.id] || "";
      const present = Boolean(raw.trim());
      const trigger = field.required_if ? byId.get(field.required_if) : null;
      const triggerRaw = field.required_if ? values[field.required_if] || "" : "";
      const triggered = trigger && trigger.type === "boolean" ? truthy(triggerRaw) : Boolean(triggerRaw.trim());
      const required = field.required || triggered;

      if (liveRules[field.id]) applied.add("guidelines.text_fields");
      if (required && !present) {
        let msg;
        if (field.type === "boolean" && field.confirm) msg = `${field.label} must be confirmed (set to yes)`;
        else if (field.type === "boolean") msg = `${field.label} must be answered (yes or no)`;
        else if (field.required_if) {
          const label = trigger ? trigger.label : field.required_if;
          msg = `${field.label} is required when ${label} ${trigger && trigger.type === "boolean" ? "is yes" : "is provided"}`;
        } else msg = `${field.label} is required but empty`;
        buckets["form.required"].push(issue("error", field.id, msg));
        continue;
      }
      if (!present) continue;

      if (field.options && field.options.length && ["choice", "multichoice", "text"].includes(field.type)) {
        applied.add("form.options");
        // Options written "code (Label)" (arXiv categories) also take the bare code.
        const accepted = new Set(field.accepted_options || field.options);
        const chosen = field.type === "multichoice" ? raw.split(",").map((c) => c.trim()) : [raw.trim()];
        for (const choice of chosen) {
          if (!choice || accepted.has(choice)) continue;
          if (field.options_recommended) {
            const entered = field.type === "multichoice" ? "added as a custom keyword" : "entered as custom text";
            buckets["form.options"].push(issue("warning", field.id, `${field.label}: '${choice}' is not in the recommended list; it will be ${entered}`));
          } else if (field.type === "multichoice" || field.type === "text") {
            // A closed single choice is a <select> here, and validate reports a bad one under form.schema.
            buckets["form.options"].push(issue("error", field.id, `${field.label}: '${choice}' is not a valid option`));
          }
        }
      }
      if (field.min_count != null || field.max_count != null) {
        applied.add("form.counts");
        const n = countItems(field, raw);
        if (field.min_count != null && n < field.min_count) buckets["form.counts"].push(issue("error", field.id, `${field.label}: provide at least ${field.min_count} (got ${n})`));
        if (field.max_count != null && n > field.max_count) buckets["form.counts"].push(issue("error", field.id, `${field.label}: provide at most ${field.max_count} (got ${n})`));
      }
      if (field.word_count != null || field.character_count != null || field.min_character_count != null) {
        applied.add("form.lengths");
        buckets["form.lengths"].push(...checkLength(field, raw));
      }
      if (field.require_url) {
        applied.add("form.urls");
        buckets["form.urls"].push(...checkUrl(field, raw));
      }
      if (field.type === "boolean" && field.required && field.confirm && !truthy(raw) && falsey(raw)) {
        buckets["form.required"].push(issue("error", field.id, `${field.label} must be confirmed (set to yes)`));
      }
      if (field.type === "authorlist") {
        applied.add("form.authors");
        buckets["form.authors"].push(...checkAuthors(field, raw));
      } else if (field.type === "textarea" && field.fields && field.fields.length) {
        applied.add("form.subfields");
        buckets["form.subfields"].push(...checkSubfields(field, raw));
      }
      if (liveRules[field.id]) buckets["guidelines.text_fields"].push(...checkGuidelineText(field, raw, liveRules[field.id]));
    }

    const checks = Object.entries(TITLES).map(([id, [title, category]]) => ({
      id,
      title,
      category,
      status: applied.has(id) ? statusFor(buckets[id]) : "not_applicable",
      issues: buckets[id],
    }));
    const byField = {};
    for (const c of checks) for (const i of c.issues) (byField[i.field] ||= []).push({ ...i, check: c.id });
    return { checks, byField };
  }

  const api = { run, parseAuthors, countItems, isHttpUrl, words, TITLES };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LiveChecks = api;
})(typeof self !== "undefined" ? self : this);
