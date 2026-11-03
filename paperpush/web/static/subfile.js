// Reading and editing .sub text in the browser.
//
// A port of parse / replace_scalar / replace_block from paperpush/subfile.py,
// kept line-for-line equivalent so the form and the server read a .sub the
// same way. tests/test_web.py runs these against the Python versions.
(function (root) {
  "use strict";

  const BLOCK_INDENT = "  ";
  const MULTILINE_TYPES = new Set(["textarea", "authorlist", "filelist"]);

  function splitLines(text) {
    // Python's str.splitlines() for the line breaks a .sub can contain.
    const lines = text.split(/\r\n|\r|\n/);
    if (lines.length && lines[lines.length - 1] === "" && /(\r\n|\r|\n)$/.test(text)) lines.pop();
    if (text === "") return [];
    return lines;
  }

  function partition(s, sep) {
    const i = s.indexOf(sep);
    return i < 0 ? [s, "", ""] : [s.slice(0, i), sep, s.slice(i + sep.length)];
  }

  function stripNewlines(s) {
    return s.replace(/^\n+/, "").replace(/\n+$/, "");
  }

  function readBlock(lines, i) {
    const block = [];
    while (i < lines.length) {
      const nxt = lines[i];
      if (nxt.startsWith(BLOCK_INDENT)) block.push(nxt.slice(BLOCK_INDENT.length));
      else if (nxt.trim() === "") block.push("");
      else break;
      i++;
    }
    return [stripNewlines(block.join("\n")), i];
  }

  function parse(text) {
    let venue = "";
    const values = {};
    const lines = splitLines(text);
    let i = 0;
    while (i < lines.length) {
      const raw = lines[i];
      const stripped = raw.trim();
      i++;
      if (!stripped || stripped.startsWith("#")) continue;
      if (stripped.startsWith("@venue:")) {
        venue = partition(stripped, ":")[2].trim();
        continue;
      }
      if (!raw.includes(":")) continue;
      const [head, , restRaw] = partition(raw, ":");
      const key = head.trim();
      const rest = restRaw.trim();
      if (rest === "|") {
        const [value, next] = readBlock(lines, i);
        values[key] = value;
        i = next;
      } else {
        values[key] = rest;
      }
    }
    return { venue, values };
  }

  function rejoin(text, out) {
    let result = out.join("\n");
    if (/(\r\n|\r|\n)$/.test(text)) result += "\n";
    return result;
  }

  function replaceScalar(text, key, newValue) {
    const out = [];
    let replaced = false;
    for (const raw of splitLines(text)) {
      if (!replaced) {
        const [head, sep, rest] = partition(raw, ":");
        if (sep && head.trim() === key && rest.trim() !== "|") {
          out.push(`${head}: ${newValue}`);
          replaced = true;
          continue;
        }
      }
      out.push(raw);
    }
    return rejoin(text, out);
  }

  function replaceBlock(text, key, newValue) {
    const lines = splitLines(text);
    const out = [];
    let i = 0;
    let replaced = false;
    while (i < lines.length) {
      const raw = lines[i];
      const [head, sep, rest] = partition(raw, ":");
      if (!replaced && sep && head.trim() === key && rest.trim() === "|") {
        out.push(raw);
        i++;
        const body = [];
        while (i < lines.length && (lines[i].startsWith(BLOCK_INDENT) || lines[i].trim() === "")) {
          body.push(lines[i]);
          i++;
        }
        let trailing = 0;
        while (body.length && body[body.length - 1].trim() === "") {
          trailing++;
          body.pop();
        }
        const valueLines = splitLines(newValue);
        for (const v of valueLines.length ? valueLines : [""]) out.push(v ? BLOCK_INDENT + v : BLOCK_INDENT);
        for (let t = 0; t < trailing; t++) out.push("");
        replaced = true;
        continue;
      }
      out.push(raw);
      i++;
    }
    return rejoin(text, out);
  }

  // Index of the line holding `key:` (scalar or block), or -1.
  function keyLine(text, key) {
    const lines = splitLines(text);
    for (let i = 0; i < lines.length; i++) {
      if (/^\s/.test(lines[i]) || lines[i].trim().startsWith("#")) continue;
      const [head, sep] = partition(lines[i], ":");
      if (sep && head.trim() === key) return i;
    }
    return -1;
  }

  // Set a field's value in the text, whichever way it is written. A field the
  // text does not mention (deleted by hand) is appended at the end.
  function setField(text, field, value) {
    const multiline = MULTILINE_TYPES.has(field.type);
    const line = keyLine(text, field.id);
    if (line >= 0) {
      const isBlock = partition(splitLines(text)[line], ":")[2].trim() === "|";
      if (isBlock) return replaceBlock(text, field.id, value);
      if (!value.includes("\n")) return replaceScalar(text, field.id, value);
      // A one-line field that now needs several lines becomes a block.
      return replaceBlock(replaceScalar(text, field.id, "|"), field.id, value);
    }
    let addition;
    if (multiline || value.includes("\n")) {
      addition = `${field.id}: |\n` + (splitLines(value).length ? splitLines(value) : [""]).map((v) => BLOCK_INDENT + v).join("\n");
    } else {
      addition = `${field.id}: ${value}`;
    }
    const sep = text === "" || text.endsWith("\n\n") ? "" : text.endsWith("\n") ? "\n" : "\n\n";
    return `${text}${sep}${addition}\n`;
  }

  // A .sub packed into a URL: deflated, then base64url, so a share link
  // carries the whole form with no server storage (it lives in the URL's
  // #fragment, which browsers never send to the server).
  async function pipeBytes(bytes, stream) {
    const out = new Response(new Blob([bytes]).stream().pipeThrough(stream));
    return new Uint8Array(await out.arrayBuffer());
  }

  async function toShareCode(text) {
    const bytes = await pipeBytes(new TextEncoder().encode(text), new CompressionStream("deflate-raw"));
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  async function fromShareCode(code) {
    const b64 = code.replace(/-/g, "+").replace(/_/g, "/");
    const bin = atob(b64 + "===".slice((b64.length + 3) % 4));
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    return new TextDecoder().decode(await pipeBytes(bytes, new DecompressionStream("deflate-raw")));
  }

  // Three-way merge of a shared form, field by field. `base` is the text both
  // sides started from, `mine` has this page's unsaved edits, and `theirs`
  // is what someone else saved. A field only one side changed takes that
  // side's value; a field both changed to different values is a conflict,
  // which keeps `mine` in the returned text and is listed so the page can
  // ask. `fields` maps a field id to its {id, type} (for setField).
  function mergeFields(base, mine, theirs, fields) {
    const b = parse(base).values;
    const m = parse(mine).values;
    const t = parse(theirs).values;
    let text = theirs;
    const conflicts = [];
    const ids = new Set([...Object.keys(m), ...Object.keys(t)]);
    for (const id of ids) {
      const bv = b[id] ?? "";
      const mv = m[id] ?? "";
      const tv = t[id] ?? "";
      if (mv === tv || mv === bv) continue; // theirs already holds the answer
      if (tv !== bv) conflicts.push({ id, mine: mv, theirs: tv });
      const field = (fields && (fields.get ? fields.get(id) : fields[id])) || { id, type: "text" };
      text = setField(text, field, mv);
    }
    return { text, conflicts };
  }

  const api = { parse, replaceScalar, replaceBlock, keyLine, setField, splitLines, toShareCode, fromShareCode, mergeFields, MULTILINE_TYPES };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Subfile = api;
})(typeof self !== "undefined" ? self : this);
