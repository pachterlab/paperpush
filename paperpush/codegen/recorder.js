// paperpush codegen: the in-page half of the recorder.
//
// Injected into every frame of every page (context.add_init_script). It turns
// the author's clicks, typing, selections and uploads into events for the
// Python side (window.__ppRecord) and, in the top frame, draws the toolbar,
// whose buttons send commands (window.__ppCommand). Python owns the script and
// pushes its state back with window.__ppSetState, so a page that navigates
// simply asks for it again ("hello").
(() => {
  if (window.__ppInstalled) return;
  window.__ppInstalled = true;

  const IS_TOP = (() => { try { return window.top === window; } catch (e) { return false; } })();
  const state = { recording: true, playing: false, paused: false, picking: null };
  let full = null; // last full state from Python (top frame only)
  let host = null, root = null, highlight = null, ui = {};
  let lastTrustedClick = null;
  let uploadSeq = 0;

  // ---------------------------------------------------------------- transport
  const call = (name, payload) => {
    try {
      const fn = window[name];
      if (typeof fn === 'function') { const p = fn(payload); if (p && p.catch) p.catch(() => {}); }
    } catch (e) { /* binding not ready or page closing */ }
  };
  const record = (evt) => { if (state.recording && !state.playing) call('__ppRecord', evt); };
  const command = (cmd) => call('__ppCommand', cmd);

  // ----------------------------------------------------------------- locators
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const TEXT_INPUT_TYPES = new Set(['', 'text', 'email', 'tel', 'url', 'search', 'password', 'number', 'date',
    'datetime-local', 'month', 'week', 'time', 'color']);

  function isVisible(el) {
    if (!el || !el.isConnected) return false;
    if (el.closest && el.closest('[aria-hidden="true"]')) return false;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    return el.getClientRects().length > 0;
  }

  function isTextLike(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.isContentEditable) return true;
    if (el.localName === 'textarea') return true;
    return el.localName === 'input' && TEXT_INPUT_TYPES.has((el.getAttribute('type') || '').toLowerCase());
  }

  function getRole(el) {
    const explicit = norm(el.getAttribute('role')).split(' ')[0];
    if (explicit && !['none', 'presentation', 'generic'].includes(explicit)) return explicit;
    const tag = el.localName;
    const type = (el.getAttribute('type') || '').toLowerCase();
    switch (tag) {
      case 'a': case 'area': return el.hasAttribute('href') ? 'link' : null;
      case 'button': return 'button';
      case 'select': return (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
      case 'option': return 'option';
      case 'textarea': return 'textbox';
      case 'img': return el.getAttribute('alt') === '' ? null : 'img';
      case 'h1': case 'h2': case 'h3': case 'h4': case 'h5': case 'h6': return 'heading';
      case 'summary': return 'button';
      case 'input':
        if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
        if (type === 'checkbox') return 'checkbox';
        if (type === 'radio') return 'radio';
        if (type === 'range') return 'slider';
        if (type === 'number') return 'spinbutton';
        if (type === 'search') return el.hasAttribute('list') ? 'combobox' : 'searchbox';
        if (['', 'text', 'email', 'tel', 'url'].includes(type)) return el.hasAttribute('list') ? 'combobox' : 'textbox';
        return null;
    }
    return null;
  }

  const ROLE_SELECTORS = {
    link: 'a[href],area[href]',
    button: 'button,summary,input[type=button],input[type=submit],input[type=reset],input[type=image]',
    combobox: 'select,input[list]',
    listbox: 'select',
    option: 'option',
    textbox: 'textarea,input:not([type]),input[type=text],input[type=email],input[type=tel],input[type=url],[contenteditable=""],[contenteditable=true]',
    searchbox: 'input[type=search]',
    checkbox: 'input[type=checkbox]',
    radio: 'input[type=radio]',
    slider: 'input[type=range]',
    spinbutton: 'input[type=number]',
    heading: 'h1,h2,h3,h4,h5,h6',
    img: 'img',
  };
  const NAME_FROM_CONTENT = new Set(['button', 'link', 'checkbox', 'radio', 'heading', 'option', 'tab', 'menuitem',
    'menuitemcheckbox', 'menuitemradio', 'treeitem', 'cell', 'columnheader', 'rowheader', 'switch', 'tooltip', 'row', 'gridcell']);

  function textOf(el) {
    let text = norm(el.innerText !== undefined ? el.innerText : el.textContent);
    if (!text) {
      const inner = el.querySelector && el.querySelector('img[alt], [aria-label], svg title');
      if (inner) text = norm(inner.getAttribute('alt') || inner.getAttribute('aria-label') || inner.textContent);
    }
    return text;
  }

  function accName(el, role) {
    const doc = el.ownerDocument;
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const text = norm(labelledby.split(/\s+/).map((id) => { const r = doc.getElementById(id); return r ? textOf(r) : ''; }).join(' '));
      if (text) return text;
    }
    const aria = norm(el.getAttribute('aria-label'));
    if (aria) return aria;
    const tag = el.localName;
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (tag === 'input' && ['button', 'submit', 'reset'].includes(type)) return norm(el.value) || (type === 'submit' ? 'Submit' : type === 'reset' ? 'Reset' : '');
      if (tag === 'input' && type === 'image') return norm(el.getAttribute('alt')) || 'Submit';
      if (el.labels && el.labels.length) {
        const text = norm([...el.labels].map((l) => labelText(l, el)).join(' '));
        if (text) return text;
      }
      return norm(el.getAttribute('title')) || norm(el.getAttribute('placeholder'));
    }
    if (tag === 'img') return norm(el.getAttribute('alt')) || norm(el.getAttribute('title'));
    if (role && NAME_FROM_CONTENT.has(role)) {
      const text = textOf(el);
      if (text) return text;
    }
    return norm(el.getAttribute('title'));
  }

  // A label's text without the text of the control nested inside it.
  function labelText(label, control) {
    const clone = label.cloneNode(true);
    clone.querySelectorAll('input,select,textarea').forEach((n) => n.remove());
    return norm(clone.textContent);
  }

  function rootOf(el) { const r = el.getRootNode(); return r && r.querySelectorAll ? r : el.ownerDocument; }

  function roleMatches(el, role) {
    const r = rootOf(el);
    const sel = (ROLE_SELECTORS[role] ? ROLE_SELECTORS[role] + ',' : '') + `[role="${role}"]`;
    return [...r.querySelectorAll(sel)].filter((e) => getRole(e) === role && isVisible(e));
  }

  const STABLE_ID_BAD = /(\d{3,}|^[:\-_]|:|^(ember|ext-gen|ext-comp|yui_|react-|mui-|radix-|headlessui-|jQuery|uid|ui-id|gwt-))/i;
  function stableId(id) { return id && id.length <= 40 && !STABLE_ID_BAD.test(id); }

  function uniqueCss(el, sel) {
    try { const found = rootOf(el).querySelectorAll(sel); return found.length === 1 && found[0] === el; } catch (e) { return false; }
  }

  function cssPath(el) {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur.localName !== 'html') {
      if (cur.id && stableId(cur.id) && uniqueCss(cur, '#' + CSS.escape(cur.id))) { parts.unshift('#' + CSS.escape(cur.id)); break; }
      let sel = cur.localName;
      const parent = cur.parentElement;
      if (parent) {
        const same = [...parent.children].filter((c) => c.localName === cur.localName);
        if (same.length > 1) sel += `:nth-of-type(${same.indexOf(cur) + 1})`;
      }
      parts.unshift(sel);
      cur = parent;
    }
    return parts.join(' > ');
  }

  // The best locator for el as [method, args, kwargs] segments (Playwright's own order of preference).
  function locate(el) {
    const testid = el.getAttribute('data-testid');
    if (testid && uniqueCss(el, `[data-testid='${CSS.escape(testid)}']`)) return [['get_by_test_id', [testid], {}]];

    const role = getRole(el);
    const name = role ? accName(el, role) : '';
    let roleNth = null;
    if (role && name && name.length <= 80) {
      const peers = roleMatches(el, role);
      const folded = name.toLowerCase();
      const loose = peers.filter((e) => accName(e, role).toLowerCase().includes(folded));
      if (loose.length === 1 && loose[0] === el) return [['get_by_role', [role], { name }]];
      const exact = peers.filter((e) => accName(e, role) === name);
      if (exact.length === 1 && exact[0] === el) return [['get_by_role', [role], { name, exact: true }]];
      const i = exact.indexOf(el);
      if (i >= 0 && exact.length <= 10) roleNth = [['get_by_role', [role], { name, exact: true }], ['nth', [i], {}]];
    }

    const tag = el.localName;
    const isControl = ['input', 'select', 'textarea'].includes(tag);
    const placeholder = norm(el.getAttribute('placeholder'));
    if (isControl && placeholder) {
      const same = [...rootOf(el).querySelectorAll('[placeholder]')].filter((e) => norm(e.getAttribute('placeholder')) === placeholder && isVisible(e));
      if (same.length === 1 && same[0] === el) return [['get_by_placeholder', [placeholder], { exact: true }]];
    }
    if (isControl && el.labels && el.labels.length) {
      const label = norm([...el.labels].map((l) => labelText(l, el)).join(' '));
      if (label) {
        const same = [...rootOf(el).querySelectorAll('input,select,textarea')].filter((e) =>
          e.labels && norm([...e.labels].map((l) => labelText(l, e)).join(' ')) === label);
        if (same.length === 1 && same[0] === el) return [['get_by_label', [label], { exact: true }]];
      }
    }

    if (el.id && stableId(el.id) && uniqueCss(el, '#' + CSS.escape(el.id))) return [['locator', ['#' + CSS.escape(el.id)], {}]];
    const nameAttr = el.getAttribute('name');
    if (isControl && nameAttr) {
      const sel = `${tag}[name='${CSS.escape(nameAttr)}']`;
      if (uniqueCss(el, sel)) return [['locator', [sel], {}]];
    }

    if (!isControl && el.children.length === 0) {
      const text = textOf(el);
      if (text && text.length <= 60) {
        const same = [...rootOf(el).querySelectorAll('*')].filter((e) => e.children.length === 0 && textOf(e) === text && isVisible(e));
        if (same.length === 1 && same[0] === el) return [['get_by_text', [text], { exact: true }]];
      }
    }

    if (roleNth) return roleNth;
    return [['locator', [cssPath(el)], {}]];
  }
  window.__ppLocate = locate;

  // The choices a dropdown-like control offers: a <select>, an input's <datalist>, an ARIA
  // listbox/combobox/menu, a radio group, or a plain list. Null when el is none of these.
  function optionsOf(target) {
    const texts = (els) => els.map((o) => norm(o.label !== undefined && o.localName === 'option' ? (o.label || o.textContent) : textOf(o))).filter(Boolean);
    let el = target;
    const select = el.closest && el.closest('select');
    if (select) return { label: labelOf(select), options: texts([...select.options]) };
    if (el.localName === 'input' && el.list) return { label: labelOf(el), options: [...el.list.options].map((o) => norm(o.value || o.label)).filter(Boolean) };
    const radio = el.localName === 'input' && el.type === 'radio' ? el : (el.localName === 'label' && el.control && el.control.type === 'radio' ? el.control : null);
    if (radio && radio.name) {
      const group = [...rootOf(radio).querySelectorAll(`input[type=radio][name="${CSS.escape(radio.name)}"]`)];
      const fieldset = radio.closest('fieldset');
      const legend = fieldset && fieldset.querySelector('legend');
      const groupEl = radio.closest('[role=radiogroup]');
      const label = legend ? textOf(legend) : (groupEl && labelOf(groupEl)) || questionBefore(group[0]) || radio.name;
      return { label, options: group.map((r) => labelOf(r)).filter(Boolean) };
    }
    // A combobox points at its popup list (only in the DOM while it is open).
    const combo = el.closest && el.closest('[role=combobox]');
    if (combo) {
      const id = combo.getAttribute('aria-controls') || combo.getAttribute('aria-owns');
      const list = id && el.ownerDocument.getElementById(id.split(/\s+/)[0]);
      if (list) return { label: labelOf(combo), options: texts([...list.querySelectorAll('[role=option]')]) };
    }
    const listbox = el.closest && el.closest('[role=listbox],[role=menu],[role=radiogroup],datalist');
    if (listbox) {
      const items = [...listbox.querySelectorAll('[role=option],[role=menuitem],[role=menuitemradio],[role=radio],option')];
      if (items.length) return { label: labelOf(listbox), options: texts(items) };
    }
    const list = el.closest && el.closest('ul,ol,menu');
    if (list) return { label: labelOf(list), options: texts([...list.children].filter((c) => c.localName === 'li')) };
    const inside = el.querySelectorAll && el.querySelectorAll('[role=option],option');
    if (inside && inside.length) return { label: labelOf(el), options: texts([...inside]) };
    return null;
  }

  // The question a control answers when nothing labels it: the nearest heading or
  // paragraph before it in the page (that is not itself one of the choices).
  function questionBefore(el) {
    const walker = el.ownerDocument.createTreeWalker(el.ownerDocument.body || el.ownerDocument, NodeFilter.SHOW_ELEMENT);
    walker.currentNode = el;
    for (let i = 0; i < 60 && walker.previousNode(); i++) {
      const node = walker.currentNode;
      if (node.matches('h1,h2,h3,h4,h5,h6,legend,[role=heading],p,strong,b') && !node.querySelector('input,select,textarea') && isVisible(node)) {
        const text = textOf(node);
        if (text) return text.length > 120 ? text.slice(0, 120) + '…' : text;
      }
    }
    return '';
  }

  // What the element says, for a branch path's key: its accessible name, else its text.
  function labelOf(el) {
    const name = accName(el, getRole(el)) || textOf(el);
    return name.length > 200 ? name.slice(0, 200) : name;
  }

  // Exposed for the Python side to name an iframe element in its parent frame.
  window.__ppFrameSelector = (el) => {
    if (el.id && stableId(el.id) && uniqueCss(el, '#' + CSS.escape(el.id))) return '#' + CSS.escape(el.id);
    for (const attr of ['name', 'title']) {
      const v = el.getAttribute(attr);
      if (v) { const sel = `${el.localName}[${attr}='${CSS.escape(v)}']`; if (uniqueCss(el, sel)) return sel; }
    }
    return cssPath(el);
  };

  // The element a click really meant: the button or link around the icon/span that was hit.
  const INTERACTIVE = 'button,a[href],input,select,textarea,label,summary,[role=button],[role=link],[role=menuitem],[role=tab],[role=option],[role=checkbox],[role=radio],[role=treeitem],[role=switch],[onclick]';
  function clickTarget(target) {
    if (!target || target.nodeType !== 1) target = target && target.parentElement;
    if (!target) return null;
    let cur = target;
    for (let i = 0; cur && i < 6; i++, cur = cur.parentElement) {
      if (cur.matches && cur.matches(INTERACTIVE)) return cur;
    }
    return target;
  }

  function controlOfLabel(el) {
    const label = el.localName === 'label' ? el : null;
    return label ? label.control : null;
  }

  function skipClick(el) {
    // Typing, selecting and ticking are recorded from their own events; a click on them is noise.
    if (isTextLike(el) || el.localName === 'select' || el.localName === 'option') return true;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (el.localName === 'input' && ['checkbox', 'radio', 'file'].includes(type)) return true;
    const control = controlOfLabel(el);
    if (control && control.localName === 'input' && ['checkbox', 'radio', 'file'].includes((control.type || '').toLowerCase())) return true;
    return false;
  }

  const fromToolbar = (e) => host && e.composedPath().includes(host);
  const modifiers = (e) => ['Alt', 'Control', 'Meta', 'Shift'].filter((m) => e.getModifierState && e.getModifierState(m));

  // ------------------------------------------------------------------ events
  const BLOCK_WHILE_PICKING = ['mousedown', 'mouseup', 'pointerdown', 'pointerup', 'dblclick', 'auxclick', 'contextmenu'];
  const ISOLATE = ['click', 'mousedown', 'mouseup', 'pointerdown', 'pointerup', 'dblclick', 'keydown', 'keyup', 'keypress',
    'input', 'change', 'focusin', 'focusout', 'focus', 'blur', 'contextmenu', 'auxclick', 'paste', 'copy', 'cut'];

  // Toolbar events are handled here and go no further, so the page never reacts to them.
  // The shadow root is closed (so neither the page nor Playwright's locators can see the
  // toolbar), which hides its nodes from composedPath(); find the real target by position.
  const innerTarget = (e) => {
    if (e.clientX !== undefined && root.elementFromPoint) return root.elementFromPoint(e.clientX, e.clientY);
    return root.activeElement;
  };
  for (const type of ISOLATE) {
    window.addEventListener(type, (e) => {
      if (!fromToolbar(e)) return;
      e.stopImmediatePropagation();
      if (type === 'mouseup') { dragging = null; return; }
      const target = innerTarget(e);
      if (!target || !target.closest) return;
      if (type === 'mousedown' && target.closest('.grip')) startDrag(e);
      if (type === 'click') onToolbarClick(target);
      if (type === 'keydown') onToolbarKey(e);
    }, true);
  }

  for (const type of BLOCK_WHILE_PICKING) {
    window.addEventListener(type, (e) => {
      if (!state.picking || fromToolbar(e)) return;
      e.preventDefault(); e.stopImmediatePropagation();
    }, true);
  }

  window.addEventListener('mousemove', (e) => {
    if (dragging) { moveToolbar(e); return; }
    if (state.picking && !fromToolbar(e)) showHighlight(e.target);
  }, true);
  window.addEventListener('mouseup', () => { dragging = null; }, true);

  window.addEventListener('click', (e) => {
    if (fromToolbar(e)) return;
    if (state.picking) {
      e.preventDefault(); e.stopImmediatePropagation();
      const el = clickTarget(e.target) || e.target;
      hideHighlight();
      const opts = state.picking === 'options' ? optionsOf(el) : null;
      command({ cmd: 'picked', purpose: state.picking, locator: locate(el), html: el.outerHTML, options: opts });
      state.picking = null;
      return;
    }
    if (!e.isTrusted) return; // label forwarding, implicit form submission, input.click()
    const el = clickTarget(e.target);
    if (!el) return;
    lastTrustedClick = el;
    if (skipClick(el)) return;
    const button = ['left', 'middle', 'right'][e.button] || 'left';
    record({ kind: 'click', locator: locate(el), label: labelOf(el), button, modifiers: modifiers(e), detail: e.detail || 1 });
  }, true);

  window.addEventListener('input', (e) => {
    if (fromToolbar(e)) return;
    const el = e.composedPath()[0];
    if (!isTextLike(el)) return;
    const sensitive = el.localName === 'input' && (el.type || '').toLowerCase() === 'password';
    record({ kind: 'fill', locator: locate(el), label: labelOf(el), value: el.isContentEditable ? el.innerText : el.value, sensitive });
  }, true);

  window.addEventListener('change', (e) => {
    if (fromToolbar(e)) return;
    const el = e.composedPath()[0];
    if (!el || el.nodeType !== 1) return;
    if (el.localName === 'select') {
      const labels = [...el.selectedOptions].map((o) => norm(o.label || o.textContent));
      record({ kind: 'select', locator: locate(el), label: labels.join(', '), value: labels });
      return;
    }
    if (el.localName !== 'input') return;
    const type = (el.type || '').toLowerCase();
    if (type === 'checkbox') { record({ kind: el.checked ? 'check' : 'uncheck', locator: locate(el), label: labelOf(el) }); return; }
    if (type === 'radio') { if (el.checked) record({ kind: 'check', locator: locate(el), label: labelOf(el) }); return; }
    if (type === 'file') {
      if (!el.files || !el.files.length) return;
      const token = String(++uploadSeq) + '-' + Date.now();
      el.setAttribute('data-pp-upload', token);
      // Opened by clicking the input (or its label) itself, or by some other button?
      const c = lastTrustedClick;
      const direct = !!c && (c === el || (c.localName === 'label' && c.control === el) || (c.contains && c.contains(el)));
      record({ kind: direct ? 'upload' : 'chooser_upload', locator: locate(el), value: [...el.files].map((f) => f.name), token });
    }
  }, true);

  const KEYS = new Set(['Enter', 'Escape', 'Tab', 'ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'PageDown', 'PageUp', 'Home', 'End', 'Delete', 'Backspace', 'F2']);
  window.addEventListener('keydown', (e) => {
    if (fromToolbar(e) || state.picking) {
      if (state.picking && e.key === 'Escape') { state.picking = null; hideHighlight(); command({ cmd: 'cancelpick' }); }
      return;
    }
    if (!e.isTrusted || !KEYS.has(e.key)) return;
    const el = e.composedPath()[0];
    const text = isTextLike(el);
    // Editing keys inside a text field are already captured by the fill.
    if (text && ['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Delete', 'Backspace'].includes(e.key)) return;
    if (e.key === 'Enter' && el && (el.localName === 'textarea' || el.isContentEditable)) return;
    let key = e.key;
    if (e.shiftKey && key === 'Tab') key = 'Shift+Tab';
    const onElement = el && el.nodeType === 1 && el !== el.ownerDocument.body && el !== el.ownerDocument.documentElement;
    record({ kind: 'press', locator: onElement ? locate(el) : null, value: key });
  }, true);

  // ---------------------------------------------------------------- highlight
  function ensureHost() {
    if (host && host.isConnected) return;
    const parent = document.documentElement;
    if (!parent) return;
    if (!host) {
      host = document.createElement('paperpush-codegen');
      host.style.cssText = 'all: initial; position: fixed; inset: 0; pointer-events: none; z-index: 2147483647;';
      root = host.attachShadow({ mode: 'closed' });
      root.innerHTML = `<style>${CSS_TEXT}</style><div class="hl" hidden></div>`;
      highlight = root.querySelector('.hl');
      if (IS_TOP) buildToolbar();
    }
    parent.appendChild(host);
  }

  function showHighlight(target) {
    ensureHost();
    const el = clickTarget(target) || target;
    if (!el || !el.getBoundingClientRect || !highlight) return;
    const r = el.getBoundingClientRect();
    Object.assign(highlight.style, { left: r.left + 'px', top: r.top + 'px', width: r.width + 'px', height: r.height + 'px' });
    highlight.hidden = false;
  }
  function hideHighlight() { if (highlight) highlight.hidden = true; }

  // ------------------------------------------------------------------ toolbar
  const CSS_TEXT = `
    :host { all: initial; }
    [hidden] { display: none !important; }
    .hl { position: fixed; background: rgba(77,144,254,.25); outline: 2px solid #4d90fe; pointer-events: none; }
    .tb { position: fixed; top: 8px; right: 8px; pointer-events: auto; font: 12px/1.3 -apple-system, system-ui, sans-serif;
          color: #eee; background: #1f2328; border: 1px solid #444c56; border-radius: 8px; box-shadow: 0 4px 16px rgba(0,0,0,.35);
          max-width: calc(100vw - 16px); display: flex; flex-direction: column; }
    .row { display: flex; flex-wrap: nowrap; align-items: center; gap: 1px; padding: 3px; overflow-x: auto; scrollbar-width: none; }
    .grip { cursor: move; padding: 0 5px; color: #8b949e; user-select: none; white-space: nowrap; }
    button { all: unset; cursor: pointer; padding: 3px 6px; border-radius: 5px; color: #e6edf3; white-space: nowrap; flex: none; }
    button:hover { background: #30363d; }
    button:disabled { opacity: .35; cursor: default; background: none; }
    button.on { background: #8b1d1d; }
    button.on:hover { background: #a32626; }
    button.active { background: #1f3a5f; }
    .sep { width: 1px; height: 16px; background: #444c56; margin: 0 3px; flex: none; }
    .depth { color: #d29922; padding: 0 6px; white-space: nowrap; }
    .prompt { display: flex; gap: 4px; align-items: center; padding: 4px 6px; border-top: 1px solid #444c56; }
    .prompt label { color: #8b949e; white-space: nowrap; max-width: 40%; overflow: hidden; text-overflow: ellipsis; }
    .prompt input { all: unset; flex: 1; min-width: 160px; background: #0d1117; border: 1px solid #444c56; border-radius: 4px;
                    padding: 3px 6px; color: #e6edf3; font: 12px ui-monospace, Menlo, monospace; }
    .status { padding: 3px 8px; color: #8b949e; border-top: 1px solid #444c56; }
    .status:empty { display: none; }
    .code { contain: inline-size; margin: 0; max-height: 40vh; overflow: auto; border-top: 1px solid #444c56; background: #0d1117;
            font: 11px/1.45 ui-monospace, Menlo, monospace; padding: 4px 0; }
    .ln { display: block; padding: 0 8px; white-space: pre; cursor: default; color: #c9d1d9; min-width: max-content; }
    .ln.own { cursor: pointer; }
    .ln.own:hover { background: #161b22; }
    .ln.sel { background: #1f3a5f; }
    .ln.edit { text-decoration: underline dotted #d29922; text-underline-offset: 3px; cursor: text; }
    .ln.editing { outline: 1px solid #d29922; background: #0d1117; text-decoration: none; }
  `;

  const BUTTONS = [
    ['record', '● Record', 'Start or stop recording actions'],
    ['play', '▶ Play', 'Replay the recorded script in a new tab (resumes when paused)'],
    ['pause', '❚❚ Pause', 'Pause the replay after the current action'],
    ['stop', '■ Stop', 'Stop the replay'],
    '|',
    ['undo', 'Undo', 'Undo the last change to the script'],
    ['redo', 'Redo', 'Redo the change just undone'],
    '|',
    ['branch', '⑂ Branch', 'Start a choice between paths: what you record from now on is the first path (one option of the question)'],
    ['nextpath', '⑂ Next', 'Finish this path: what you record from now on is the branch\'s next path'],
    ['endbranch', '⑂ End', 'End the branch: press before the action that follows the choice (e.g. Continue)'],
    ['optional', '? Optional', 'Mark what you record next as optional (# ---OPTIONAL---); press again to end the section (# ---END OPTIONAL---)'],
    '|',
    ['pickoptions', '⌖ Dropdown Choices', 'Pick a dropdown, list or radio group: its choices go into the code as a comment'],
    ['variable', '{x} Variable', 'Replace the selected line\'s value with a variable (a .sub field), or rename the selected branch'],
    '|',
    ['pickhtml', '⌖ HTML', 'Pick an element and copy its HTML'],
    ['copyhtml', 'Copy Page HTML', 'Copy the HTML of this page and its frames'],
    '|',
    ['panel', 'Code', 'Show or hide the generated code'],
    ['copy', 'Copy Code', 'Copy the generated code'],
    ['save', 'Save Code', 'Save the generated code to a file'],
  ];

  let dragging = null;
  let prompt = null; // { kind: 'variable' | 'save', index }
  let editing = null; // { span, kind, index }
  let codeSig = null; // the code the panel shows, to skip needless rebuilds

  function buildToolbar() {
    const tb = document.createElement('div');
    tb.className = 'tb';
    const row = document.createElement('div');
    row.className = 'row';
    const grip = document.createElement('span');
    grip.className = 'grip'; grip.textContent = '⠿ paperpush'; grip.title = 'Drag to move';
    row.appendChild(grip);
    for (const b of BUTTONS) {
      if (b === '|') { const s = document.createElement('span'); s.className = 'sep'; row.appendChild(s); continue; }
      const [cmd, label, title] = b;
      const el = document.createElement('button');
      el.dataset.cmd = cmd; el.textContent = label; el.title = title;
      row.appendChild(el);
      ui[cmd] = el;
    }
    const depth = document.createElement('span'); depth.className = 'depth'; row.appendChild(depth); ui.depth = depth;
    tb.appendChild(row);

    const pr = document.createElement('div');
    pr.className = 'prompt'; pr.hidden = true;
    pr.innerHTML = '<label></label><input list="pp-fields" spellcheck="false"><datalist id="pp-fields"></datalist>' +
      '<button data-cmd="prompt-ok">OK</button><button data-cmd="prompt-cancel">Cancel</button>';
    tb.appendChild(pr);
    ui.prompt = pr; ui.promptLabel = pr.querySelector('label'); ui.promptInput = pr.querySelector('input');
    ui.datalist = pr.querySelector('datalist');

    const status = document.createElement('div'); status.className = 'status'; tb.appendChild(status); ui.status = status;
    const code = document.createElement('pre'); code.className = 'code'; tb.appendChild(code); ui.code = code;
    root.appendChild(tb);
    ui.tb = tb;
    try {
      const pos = JSON.parse(sessionStorage.getItem('__ppToolbarPos') || 'null');
      if (pos) placeToolbar(pos.left, pos.top);
      if (sessionStorage.getItem('__ppPanel') === '0') code.hidden = true;
    } catch (e) { /* storage blocked */ }
    render();
  }

  function startDrag(e) {
    const r = ui.tb.getBoundingClientRect();
    dragging = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    e.preventDefault();
  }

  function placeToolbar(left, top) {
    const tb = ui.tb;
    left = Math.max(0, Math.min(left, window.innerWidth - 60));
    top = Math.max(0, Math.min(top, window.innerHeight - 30));
    Object.assign(tb.style, { left: left + 'px', top: top + 'px', right: 'auto' });
  }
  function moveToolbar(e) {
    placeToolbar(e.clientX - dragging.dx, e.clientY - dragging.dy);
    try { sessionStorage.setItem('__ppToolbarPos', JSON.stringify({ left: parseFloat(ui.tb.style.left), top: parseFloat(ui.tb.style.top) })); } catch (err) { /* ignore */ }
  }

  // Variable: name the selected line's value, or rename the branch it belongs to.
  function openVariable() {
    if (!full) return;
    const v = full.variable;
    if (!v) { command({ cmd: 'variable', index: full.variableIndex, name: null }); return; }
    prompt = { kind: 'variable', index: v.index };
    ui.promptLabel.textContent = (v.kind === 'branch' ? 'rename ' : 'variable for ') + v.what + ' →';
    ui.promptLabel.title = v.what;
    ui.promptInput.value = v.current || '';
    ui.datalist.innerHTML = '';
    for (const f of full.fields || []) { const opt = document.createElement('option'); opt.value = f; ui.datalist.appendChild(opt); }
    ui.prompt.hidden = false;
    setTimeout(() => { ui.promptInput.focus(); ui.promptInput.select(); }, 0);
  }
  function openSave() {
    if (!full) return;
    prompt = { kind: 'save' };
    ui.promptLabel.textContent = 'save code to';
    ui.promptLabel.title = 'A path on this computer (relative paths are from where paperpush codegen was started)';
    ui.promptInput.value = full.savePath || '';
    ui.datalist.innerHTML = '';
    ui.prompt.hidden = false;
    setTimeout(() => { ui.promptInput.focus(); ui.promptInput.select(); }, 0);
  }
  function closePrompt() { prompt = null; if (ui.prompt) ui.prompt.hidden = true; }
  function submitPrompt() {
    if (!prompt) return;
    if (prompt.kind === 'save') command({ cmd: 'save', path: ui.promptInput.value });
    else command({ cmd: 'variable', index: prompt.index, name: ui.promptInput.value });
    closePrompt();
  }

  // Editable lines of the code panel (a branch's name, a path's key): retype them in place.
  function startEdit(span) {
    if (editing) finishEdit(false);
    editing = { span, kind: span.dataset.edit, index: Number(span.dataset.owner), original: span.textContent };
    span.classList.add('editing');
    span.contentEditable = 'plaintext-only';
    span.focus();
    const range = document.createRange();
    range.selectNodeContents(span);
    const sel = root.getSelection ? root.getSelection() : window.getSelection();
    if (sel) { sel.removeAllRanges(); sel.addRange(range); }
  }
  function finishEdit(save) {
    if (!editing) return;
    const { span, kind, index, original } = editing;
    editing = null;
    span.contentEditable = 'false';
    span.classList.remove('editing');
    codeSig = null; // the span may hold typed text: rebuild it from the next state
    const text = span.textContent;
    if (save && text.trim() !== original.trim()) command({ cmd: 'editline', kind, index, text });
    else render();
  }

  function onToolbarKey(e) {
    if (editing) {
      if (e.key === 'Enter') { e.preventDefault(); finishEdit(true); }
      if (e.key === 'Escape') { e.preventDefault(); finishEdit(false); }
      return;
    }
    if (!prompt) return;
    if (e.key === 'Enter') { e.preventDefault(); submitPrompt(); }
    if (e.key === 'Escape') { e.preventDefault(); closePrompt(); }
  }

  // Text the author dragged over in the code panel (to copy it): clicks must not re-render it away.
  function codeSelected() {
    const sel = root.getSelection ? root.getSelection() : window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount || !ui.code) return false;
    const node = sel.getRangeAt(0).commonAncestorContainer;
    return ui.code === node || ui.code.contains(node);
  }

  function onToolbarClick(target) {
    const line = target.closest('.ln');
    if (line && codeSelected() && !(editing && editing.span === line)) return;
    if (line) {
      if (editing && editing.span === line) return;
      if (editing) finishEdit(true);
      if (line.dataset.edit) { startEdit(line); return; }
      if (line.dataset.owner !== '') command({ cmd: 'select', index: Number(line.dataset.owner) });
      return;
    }
    if (editing) finishEdit(true);
    const btn = target.closest('[data-cmd]');
    if (!btn || btn.disabled) return;
    const cmd = btn.dataset.cmd;
    switch (cmd) {
      case 'variable': openVariable(); return;
      case 'save': openSave(); return;
      case 'pickoptions': command({ cmd: 'pick', purpose: 'options' }); return;
      case 'prompt-ok': submitPrompt(); return;
      case 'prompt-cancel': closePrompt(); return;
      case 'pickhtml': command({ cmd: 'pick', purpose: 'html' }); return;
      case 'panel':
        ui.code.hidden = !ui.code.hidden;
        try { sessionStorage.setItem('__ppPanel', ui.code.hidden ? '0' : '1'); } catch (err) { /* ignore */ }
        render();
        return;
      default: command({ cmd });
    }
  }

  function setStatus(text) { if (ui.status) ui.status.textContent = text || ''; }

  function render() {
    if (!ui.tb || !full) return;
    const s = full;
    ui.record.classList.toggle('on', s.recording);
    ui.record.textContent = s.recording ? '● Recording' : '● Record';
    ui.play.textContent = s.paused ? '▶ Resume' : '▶ Play';
    ui.play.disabled = s.playing && !s.paused;
    ui.pause.hidden = ui.stop.hidden = !s.playing;
    ui.pause.disabled = s.paused;
    for (const c of ['record', 'branch', 'nextpath', 'endbranch', 'optional', 'variable', 'pickoptions']) ui[c].disabled = s.playing;
    ui.optional.classList.toggle('active', !!s.optional);
    ui.optional.textContent = s.optional ? '? End Optional' : '? Optional';
    ui.undo.disabled = s.playing || !s.can_undo;
    ui.redo.disabled = s.playing || !s.can_redo;
    ui.branch.disabled = s.playing || !!s.depth;
    ui.nextpath.disabled = s.playing || !s.pathOpen;
    ui.endbranch.disabled = s.playing || !s.depth;
    ui.panel.classList.toggle('active', !ui.code.hidden);
    ui.depth.textContent = s.depth ? `in ${s.branch}` : '';
    const pickHelp = { html: 'Click an element to copy its HTML (Esc to cancel)', options: 'Click a dropdown, list or radio button: its options go into the code (Esc to cancel)' };
    setStatus(s.message || pickHelp[s.picking] || '');

    if (editing) return; // don't throw away a line being retyped
    // Rebuild the lines only when the code changed: rebuilding drops any text the author selected.
    const sig = JSON.stringify([s.lines, s.owners, s.edits]);
    if (sig === codeSig) {
      let selected = null;
      for (const span of ui.code.children) {
        const own = span.dataset.owner !== '';
        const on = own && Number(span.dataset.owner) === s.selected;
        span.classList.toggle('sel', on);
        if (on && !selected) selected = span;
      }
      if (!ui.code.hidden && selected && !codeSelected()) selected.scrollIntoView({ block: 'nearest' });
      return;
    }
    codeSig = sig;
    const atBottom = ui.code.scrollTop + ui.code.clientHeight >= ui.code.scrollHeight - 4;
    ui.code.textContent = '';
    let selected = null;
    (s.lines || []).forEach((text, i) => {
      const owner = s.owners[i];
      const edit = (s.edits || [])[i];
      const span = document.createElement('span');
      const own = owner !== null && owner !== undefined;
      span.className = 'ln' + (own ? ' own' : '') + (own && owner === s.selected ? ' sel' : '') + (edit ? ' edit' : '');
      span.dataset.owner = own ? String(owner) : '';
      span.dataset.edit = edit || '';
      if (edit) span.title = edit === 'branch_name' ? 'Click to rename the branch' : 'Click to rename this path';
      span.textContent = text || ' ';
      ui.code.appendChild(span);
      if (own && owner === s.selected && !selected) selected = span;
    });
    if (ui.code.hidden) return;
    if (s.selected !== null && s.selected !== undefined && selected) selected.scrollIntoView({ block: 'nearest' });
    else if (atBottom) ui.code.scrollTop = ui.code.scrollHeight;
  }

  // Where a toolbar control is on screen, for tests (the shadow root is closed to Playwright).
  window.__ppToolbarRect = (cmd) => {
    const el = root && root.querySelector(cmd.startsWith('.') ? cmd : `[data-cmd="${cmd}"]`);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  };

  // ------------------------------------------------------------- python -> us
  window.__ppSetState = (s) => {
    state.recording = !!s.recording;
    state.playing = !!s.playing;
    state.paused = !!s.paused;
    state.picking = s.picking || null;
    if (!state.picking) hideHighlight();
    if (IS_TOP && s.lines) { full = s; ensureHost(); render(); }
    // Replay tabs are driven by Playwright: keep the toolbar out of the way of its clicks.
    if (host) host.style.display = s.hidden ? 'none' : '';
  };

  // Frames other than the top one only get a host (for the pick highlight) when they need it.
  const boot = () => {
    if (IS_TOP) ensureHost();
    command({ cmd: 'hello', top: IS_TOP });
  };
  if (document.documentElement) boot(); else document.addEventListener('readystatechange', boot, { once: true });
  if (IS_TOP) document.addEventListener('DOMContentLoaded', ensureHost);
  // Some pages rebuild <html>'s children; put the toolbar back.
  setInterval(() => { if (host && !host.isConnected) ensureHost(); }, 1000);
})();
