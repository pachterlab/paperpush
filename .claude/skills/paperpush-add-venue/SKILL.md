---
name: paperpush-add-venue
description: >-
  Add a journal, preprint server, or conference to paperpush (or correct one)
  by filling in a venue file from the venue's own author-guideline and
  submission pages, then merging it with `paperpush add-venue`. Triggers: "add
  <venue> to paperpush", "support <journal> in paperpush", "fill in this venue
  file", "fix <venue>'s word limits in paperpush".
---

# Add a venue to paperpush

A venue file (`SLUG.txt`) holds everything paperpush knows about a venue: its
master entry, the portal's submission form, and the author guidelines'
manuscript rules. Every key is described in a comment above it.

1. **Write the file.**
   - New venue: `paperpush add-venue --template SLUG` (slug: lowercase, digits,
     underscores, e.g. `nature_genetics`).
   - Correcting a venue, or a sister journal of one: `paperpush add-venue
     --template --from EXISTING -o SLUG.txt`.

2. **Read the venue's pages.** Read the author guidelines (all article types),
   the submission/initial-submission instructions, and, if the user can log in,
   the portal's form itself. Publisher sites (Nature, Science, Cell, Springer,
   Elsevier) often refuse plain fetches; render them with Playwright instead.
   PDF and Word guideline files count too. Ask the user for anything behind a
   login rather than guessing.

3. **Fill in the file.** Read it top to bottom first.
   - Record only what the pages state. Leave every other key empty: an empty
     key means "the venue does not say", and paperpush checks nothing for it.
   - `[venue]`: set `anonymous: yes` only for double-blind review.
   - `[form]`: list the portal's fields in the portal's order. Delete the
     starter `[field]` blocks the portal lacks. Reuse a common id when one fits
     (the comment lists them), otherwise give the field a `section`. Copy
     `options` exactly as the portal words them. Set `include: no` when you
     cannot see the form; the venue is then validate-only.
   - `[requirements.*]`: the research-article rules. Use the structured keys
     where they fit and put everything else in that section's `notes`, one
     factual sentence per line. List every page you used in `source_urls` and
     set `retrieved` to today.
   - `[article_type NAME]`: one block per other article type, setting only what
     differs (`manuscript.max_words_before_refs: 3000`). NAME must be an
     option of the article-type field, word for word.

4. **Check, then add.** Run `paperpush add-venue SLUG.txt --dry-run` and fix
   every error, which comes with its line number. Treat warnings as questions:
   a portal limit that differs from the guidelines is fine only if the venue
   really says both. Then run `paperpush add-venue SLUG.txt`, adding `--force`
   when replacing an existing venue. Finish with `paperpush subfile SLUG` and
   `paperpush requirements SLUG` to see the result.

5. **Report.** Tell the user what was added, which pages it came from, what you
   could not determine (form fields behind a login, unclear limits), and, in a
   source checkout, the follow-up commands `add-venue` printed. Submitting to a
   new venue also needs a runner script (`CONTRIBUTING.md`); say so instead of
   writing one unasked.
