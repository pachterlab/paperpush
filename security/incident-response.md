# Incident response plan

*Control SEC-12. Owner: lead maintainer. Reviewed every year.*

An incident is anything that has or could have exposed confidential or
secret data (see [policy.md](policy.md#data-classification)), let someone
act as an author, or taken the website down for more than a day.

## Severity

| Level | Examples | Response |
| --- | --- | --- |
| **SEV1** | Uploaded files or shared forms readable by others; a leaked database URL or PyPI token; a malicious release; an agent tool that can send a submission | Start within 24 h, work until contained |
| **SEV2** | An exploitable vulnerability with no known exploitation; a critical CVE in a production dependency; a secret in git history that was never used | Start within 3 business days, fix within 30 days |
| **SEV3** | Website outage; a low-severity vulnerability; a failed weekly security scan | Fix within 90 days, or record an accepted risk |

## Roles

- **Incident lead:** the lead maintainer, or whoever they name. Makes the
  calls and keeps the record.
- **Responders:** any maintainer the lead pulls in.

## Steps

1. **Detect and open a record.** Sources include a SECURITY.md report, a
   failed `security.yml` or `uptime.yml` run, a Dependabot alert, or a user
   report. Open a private GitHub security advisory draft (SEV1–2) or an issue
   (SEV3). Log every step in it with a timestamp.
2. **Triage.** Assign a severity. Work out what data, which users, and since
   when.
3. **Contain** (playbooks below).
4. **Fix** through the normal change process, or the emergency path in
   [change-management.md](change-management.md#emergency-changes).
5. **Recover.** Redeploy and confirm `/healthz` and the affected feature work.
6. **Notify.**
   - *Website users:* there are no accounts or email addresses, so post a
     notice on the website and publish the GitHub advisory.
   - *Package users:* publish the GitHub advisory (which also reaches the
     GitHub Advisory Database and pip-audit) and ship a release.
   - *The reporter:* keep them updated and credit them.
   - *Vendors:* tell the affected vendor when their service is involved.
7. **Review.** Within 2 weeks of a SEV1 or SEV2, write a short post-incident
   review in the record: timeline, cause, what worked, what to change.
   Update the risk register.

## Playbooks

### Uploaded files or shared forms exposed

1. Suspend the Render service (Dashboard › paperpush › Settings › Suspend).
2. Workspaces: the next deploy wipes the disk, so redeploy once the fix is
   in. Shared forms are ciphertext, but if the encryption or the write tokens
   are at fault, delete the affected rows in Neon (`DELETE FROM shared_forms
   WHERE ...`).
3. Fix, review, deploy, and resume the service.

### A secret is leaked

| Secret | Rotate by |
| --- | --- |
| `PAPERPUSH_WEB_DATABASE_URL` (Neon) | Neon › Roles › reset the password; paste the new URL into Render › Environment; redeploy |
| PyPI API token | pypi.org › Account settings › API tokens › revoke, then create a new one. Check the release history for uploads nobody made. |
| A maintainer's GitHub, Render, or Neon password | Change it, revoke sessions and tokens, check the audit log |
| A token in git history | Rotate first, then remove it from history (`git filter-repo`) and ask GitHub support to purge cached views |

### Vulnerable dependency (pip-audit or Dependabot alert)

1. Check whether the vulnerable code path is reachable in paperpush.
2. Merge the Dependabot PR, or bump the pin in `requirements/web.txt` with
   the command at the top of the file. Let CI and the deploy run.
3. If no fix exists and the path is reachable, mitigate in code or turn the
   feature off. Otherwise record an accepted risk with a review date.

### Malicious or broken release on PyPI

1. Yank the release on PyPI (do not delete it, so the record stays).
2. Publish an advisory naming the bad versions, and release a fixed version.

### Website down

1. Check the Render dashboard (events, logs) and the free-plan hour quota.
2. Redeploy the last good commit if a deploy caused it.
3. Point users to `pip install paperpush` and `paperpush web` meanwhile.

## Record template

```
Title:
Severity:                 Lead:
Detected (UTC):           Source:
Contained (UTC):          Resolved (UTC):
Data / users affected:
Timeline:
Root cause:
Notifications sent:
Follow-ups (issues):
```
