/*
 * To run maually: copy the contents of this file into the browser console on the Nature submission step that shows the subject-category menus, then call downloadNatureSubjects() to scrape and save subjects.json.

 * Scrape Nature's subject-category tree from the manuscript submission page
 * (eJournalPress).
 *
 * The subject picker is a stack of dependent <select> menus: choosing a level-1
 * subject calls the page's own get_next_level_subjects("1"), which fires a
 * Prototype.js Ajax.Request that repopulates the level-2 menu, and so on. The
 * tree is NOT a fixed depth -- some branches stop at level 2, others continue to
 * level 3, 4, 5, or 6 -- so this walks the menus recursively, descending as far
 * as each branch actually goes rather than assuming a depth.
 *
 * Rather than guess at a fixed settle delay after each selection (the previous
 * approach, which was both slow and racy), this hooks Prototype's
 * Ajax.Request so it can await the exact moment each get_next_level_subjects call
 * finishes. The hook captures every request's onSuccess and, while a walk step is
 * waiting, hands the response text to a one-shot resolver; the walker parses the
 * returned <option>s to learn the child level and only recurses when there are
 * children. A get_next_level_subjects call that fires no Ajax (the deepest menu
 * has none) resolves immediately to "no children", ending that branch.
 *
 * Two entry points:
 *
 *   scrapeNatureSubjects()      -> Promise<tree>   (returns the data)
 *   downloadNatureSubjects()    -> Promise<void>   (returns + saves subjects.json)
 *
 * scrapeNatureSubjects is the one Playwright calls via page.evaluate; the
 * download wrapper is for running by hand in the browser console.
 *
 * The shape is a uniform recursive tree: every node is keyed by its category
 * name and carries a "value" (the option value) plus a "children" object of the
 * same shape. A leaf has an empty "children" object, so the same structure
 * represents a branch that stops at any depth. The Python side
 * (convert_scraped_categories) passes this through unchanged into
 * nature_categories.json.
 *
 *   {
 *     "Biological sciences": {
 *       "value": "Biological sciences:631",
 *       "children": {
 *         "Biochemistry": {
 *           "value": "Biochemistry:45",
 *           "children": {
 *             "Biocatalysis": { "value": "Biocatalysis:603", "children": {} }
 *           }
 *         }
 *       }
 *     }
 *   }
 */
(function () {
  // One-shot resolver set by loadNext() while it awaits the next level's Ajax
  // response, and called by the Ajax.Request hook below when that response
  // arrives. Null when no walk step is waiting.
  let ajaxResolver = null;

  // Wrap Prototype's Ajax.Request once so every request's onSuccess also feeds
  // the waiting resolver (if any) the raw response text, then runs the page's
  // own handler unchanged. Idempotent: a flag guards against double-wrapping if
  // scrapeNatureSubjects is invoked more than once on the same page.
  function installAjaxHook() {
    if (typeof Ajax === "undefined" || !Ajax.Request) {
      throw new Error(
        "Prototype's Ajax.Request not found -- run this on the Nature " +
          "submission step (eJournalPress) that shows the subject menus."
      );
    }
    if (Ajax.Request.prototype.__natureHooked) return;

    const original = Ajax.Request.prototype.request;
    Ajax.Request.prototype.request = function (...args) {
      const success = this.options.onSuccess;
      this.options.onSuccess = function (transport) {
        if (ajaxResolver) {
          const resolve = ajaxResolver;
          ajaxResolver = null;
          resolve(transport.responseText);
        }
        if (success) success(transport);
      };
      return original.apply(this, args);
    };
    Ajax.Request.prototype.__natureHooked = true;
  }

  // Trigger the population of the menu below `level` and resolve with the child
  // options once the Ajax response lands. The resolver receives the response
  // HTML; its <option>s (minus the leading "-- select --" placeholder) are the
  // children. If get_next_level_subjects fires no Ajax there is no deeper menu,
  // so resolve to an empty list. A safety timeout guards against a request that
  // never completes so a single stuck branch cannot hang the whole walk.
  function loadNext(level, timeoutMs) {
    return new Promise((resolve) => {
      let settled = false;
      const finish = (opts) => {
        if (settled) return;
        settled = true;
        ajaxResolver = null;
        resolve(opts);
      };

      ajaxResolver = (html) => {
        const doc = new DOMParser().parseFromString(html, "text/html");
        const opts = [...doc.querySelectorAll("option")]
          .slice(1)
          .map((o) => ({ name: o.textContent.trim(), value: o.value }));
        finish(opts);
      };

      const result = get_next_level_subjects(String(level));

      // No Ajax fired means no menu below this one: this level is a leaf.
      if (result === undefined || result === false) {
        finish([]);
      }

      setTimeout(() => finish([]), timeoutMs);
    });
  }

  // Walk one dependent menu and return its {name: {value, children}} map.
  // `level` is the 1-based menu number (#subject_level_<level>). For each option
  // it selects the option, asks the page to populate the next menu (loadNext),
  // and -- only when that yields children -- recurses into the now-populated
  // deeper menu. An option whose next menu is absent or empty is a leaf
  // ({children: {}}). The menu is re-queried each iteration in case the DOM node
  // is replaced when a deeper level repopulates.
  async function walk(level, timeoutMs) {
    const select = document.querySelector(`#subject_level_${level}`);
    if (!select || select.options.length <= 1) return {};

    const tree = {};
    for (let i = 1; i < select.options.length; i++) {
      const current = document.querySelector(`#subject_level_${level}`);
      current.selectedIndex = i;
      const option = current.options[i];

      const node = { value: option.value, children: {} };
      tree[option.text] = node;

      const children = await loadNext(level, timeoutMs);
      if (children.length) {
        node.children = await walk(level + 1, timeoutMs);
      }

      console.log(
        `${"  ".repeat(level - 1)}${option.text}: ${
          Object.keys(node.children).length
        }`
      );
    }
    return tree;
  }

  // `timeoutMs` is the per-request safety timeout for each get_next_level_subjects
  // Ajax call; it is floored so a tiny value passed by an older caller cannot make
  // a slow response read as "no children". Walking is otherwise driven by the Ajax
  // completions, not by fixed delays.
  async function scrapeNatureSubjects(timeoutMs = 20000) {
    installAjaxHook();

    if (!document.querySelector("#subject_level_1")) {
      throw new Error(
        "#subject_level_1 not found -- run this on the Nature submission step " +
          "that shows the subject-category menus."
      );
    }
    if (typeof get_next_level_subjects !== "function") {
      throw new Error(
        "get_next_level_subjects() not found -- run this on the Nature " +
          "submission step that shows the subject-category menus."
      );
    }

    const timeout = Math.max(Number(timeoutMs) || 0, 8000);
    return walk(1, timeout);
  }

  // Convenience wrapper for manual console use: scrape, then save subjects.json.
  async function downloadNatureSubjects(timeoutMs = 20000) {
    const tree = await scrapeNatureSubjects(timeoutMs);
    const blob = new Blob([JSON.stringify(tree, null, 2)], {
      type: "application/json",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "subjects.json";
    a.click();
    return tree;
  }

  // Expose for Playwright's page.evaluate and for the console.
  if (typeof window !== "undefined") {
    window.scrapeNatureSubjects = scrapeNatureSubjects;
    window.downloadNatureSubjects = downloadNatureSubjects;
  }
})();
