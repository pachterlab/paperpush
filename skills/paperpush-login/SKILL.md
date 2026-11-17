---
name: paperpush-login
description: >-
  Sign the author in to a venue's submission system so paperpush can submit
  for them, without the password ever passing through the chat: starts
  `paperpush login <venue> --browser`, which opens a one-time sign-in page on
  this computer where the author types their credentials, checks them against
  the venue, and stores them in the OS keyring. Also reports, re-checks, or
  removes a stored login. Called by paperpush-submit and
  paperpush-prepare-submission when a login is missing. Triggers: "log me in
  to <venue>", "store my <venue> login", "sign in to <venue> for paperpush",
  "am I logged in to <venue>", "check my <venue> login still works", "log me
  out of <venue>", "switch my <venue> account", "paperpush-login".
---

# paperpush login (Claude skill)

Gets a working, stored login for one venue. The author types the username and
password into a page that `paperpush login --browser` serves on 127.0.0.1, not
into the chat. You only ever see the page's URL and whether the login was
stored.

Other skills call this one when a venue has no working login:
`paperpush-submit` (step 1) and `paperpush-prepare-submission` (its up-front
check). Called that way, do steps 1-4 and hand back one of three answers
(skip step 1 when the caller has just run `--check` and it failed):
**logged in**, **waiting** (the page is open and the author has not finished),
or **not logged in** (with the reason, and whether the author would rather
sign in by hand in the submission's browser window).

## Inputs

- **The venue slug.** It comes from the user, the calling skill, or the
  `.sub` (`venue:` near the top). `paperpush venues` lists the slugs. A venue
  that submits through another's portal (the AAAS and Nature families) shares
  that base venue's login; paperpush redirects the login there by itself.
- **What the user wants**: a new login (the default), the status, a re-check,
  or a logout.
- **A sign-in method, only if the user names one.** "I sign in with ORCID"
  means add `--orcid`, "with GitHub" means `--github`, and an NCBI provider
  (sra, geo) means `--provider <name>`. Otherwise add nothing. The page
  offers every method the venue supports, and the author picks one there, so
  don't ask about it.

## Steps

### 1. Check what is stored

```
paperpush login <venue> --status
```

MCP: `login_status(venue=<venue>)`.

- "does not require login", or a validate-only venue: there is nothing to sign
  in to. Say so and stop (calling skill: **logged in**).
- Logged in, and the user only asked whether they are: report who it signs in
  as (never anything else) and stop.
- Logged in, and the login is needed for a submission, or the user asked
  whether it still works: run `paperpush login <venue> --check` (MCP:
  `login_status(venue=..., check=true)`). If it passes, or says the venue
  "could not be checked", you are done (**logged in**). If it fails, the
  password probably changed: go to step 2.
- Not logged in: go to step 2.

To **log out** or **switch accounts**, confirm with the user first (the next
submission will then need a new sign-in), run `paperpush login <venue>
--logout`, and for a switch go on to step 2.

### 2. Open the sign-in page

Start the login **in the background**. It needs no keyboard input and exits
on its own once a login is stored, after 5 failed tries, or after 15 minutes:

```
paperpush login <venue> --browser [--orcid | --github | --provider NAME]
```

- In Claude Code, use the Bash tool with `run_in_background: true`. Read the
  first lines of its output for the URL (`http://127.0.0.1:<port>/<token>`).
- Over MCP, call `login(venue=<venue>, browser=true)` instead. It returns
  `status: awaiting_sign_in` and the `url`. A second call while the page is
  still open returns the same page rather than opening another.

paperpush opens a browser tab on the page by itself. In your next message,
tell the user:

- A sign-in page for `<venue>` has opened in their browser (give them the URL
  in case it didn't). They should type their username and password there,
  **not in this chat**. The page saves them on this computer only.
- paperpush checks the details by signing in to the portal before it saves
  them. A browser window may open for a CAPTCHA or a two-factor code; they
  should finish it there.
- With no account on the venue's submission system yet, they have to register
  first (the page links to the portal). paperpush cannot create accounts.
- The page closes after 15 minutes.

When another skill called this one, it can go on with its own work while the
author signs in (`paperpush-prepare-submission` does). Otherwise, end your
turn here.

### 3. Wait for it to finish

- **Background Bash:** you are notified when the command exits, so don't poll
  it. Exit 0 means the login is stored; its last lines say who it signs in as.
- **MCP:** call `login_status(venue=<venue>)` when the user says they are
  done, or at intervals of a minute or more. `logged_in: true` means it is
  stored. `login_progress.state` is `waiting` while the page is open,
  `finished` once the command has exited cleanly, and `failed` once it has
  given up, with the reason in `output_tail`.

If the user says they signed in but nothing is stored yet, the check is
probably still running, or waiting on the CAPTCHA/two-factor window. Ask them
to look for that window.

### 4. Report

- **Stored:** say which account it signs in as and that it is saved on this
  computer. paperpush already checked it during sign-in, so you don't need
  `--check` (unless the user passed `--no-verify`).
- **Failed** (exit 1, or `login_progress.state` is `failed`): give the reason
  from the output: a wrong password after 5 tries, no sign-in within 15
  minutes, or a sign-in method paperpush can't drive for this venue. Offer to
  open the page again. If the venue's sign-in page itself seems to have
  changed, the venue's `login()` code probably needs fixing (contributors:
  `paperpush-add-venue`).

## When the page can't be reached

The page is served only on this computer. If the user can't open it:

- **Over SSH or on a remote machine:** paperpush prints an `ssh -L <port>:...`
  line. Have the user run it on their own machine, then open the same URL
  there. Pass `--port <n>` to fix the port in advance.
- **No browser at all, or the user would rather not use the page:** they can
  run `paperpush login <venue>` in their **own** terminal window. A `!`
  command in this session gets no keyboard input, so the prompts fail there.
- **For a submission**, the user can also skip storing a login and sign in by
  hand in the submission's browser window (`paperpush-submit`'s manual
  sign-in). Hand that choice back to the calling skill as **not logged in,
  manual sign-in preferred**.

## Rules

- Never ask for a password, and never accept one in the chat. If the user
  pastes one anyway, don't use it. Tell them it is now in the conversation
  history and that they may want to change it, then open the page.
- Never pass `--password`, `-u` with a password, or `PAPERPUSH_PASSWORD`, and
  never set credential environment variables.
- Never open, fill, or submit the sign-in page yourself, with Playwright, a
  browser tool, or `curl`. The page is for the author's hands only.
- Never read the credential store (the keyring, or
  `~/.config/paperpush/credentials.json`), and never print a stored password.
- To store a login, run `paperpush login <venue>` only with `--browser`.
  Without it, the command waits for keyboard input that you can't give.
  `--status`, `--check`, `--list`, and `--logout` are fine to run.
- Confirm before `--logout`.
