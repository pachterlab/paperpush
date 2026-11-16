Shared NCBI login
=================

NCBI services can reuse ``paperpush.venues.ncbi.login_ncbi`` in their runners.
It drives the account gateway at https://account.ncbi.nlm.nih.gov/ using any
of its seven featured providers:

.. list-table::
   :header-rows: 1

   * - ``provider``
     - Account
   * - ``ncbi`` (default)
     - NCBI Account
   * - ``era``
     - eRA Commons
   * - ``google``
     - Google Account
   * - ``orcid``
     - ORCiD
   * - ``login.gov``
     - Login.gov
   * - ``microsoft``
     - Microsoft
   * - ``nih``
     - NIH Account

The displayed labels are accepted as provider names too. Credentials must
belong to the selected provider. The caller supplies a Playwright page and
credentials already collected through a secure prompt or credential store:

.. code-block:: python

   from paperpush.venues.ncbi import login_ncbi

   login_ncbi(page, username, password, provider="orcid")

Google and Microsoft use separate identity and password steps. NIH/eRA
password submission is scoped separately from the PIV smart-card button.
The helper checks the expected provider host before filling either field;
enterprise federation to another organization requires manual completion.

For an interactive, headed browser, ``allow_manual=True`` permits a terminal
handoff if MFA, CAPTCHA, PIV, consent, account linking, or a changed form
prevents automatic completion. Omit both credentials to do the entire
provider sign-in yourself:

.. code-block:: python

   login_ncbi(page, provider="login.gov", allow_manual=True)

Keep ``allow_manual=False`` (the default) for unattended or headless callers.
No MFA code is collected, and no consent or account-linking choice is made
automatically. Failure raises ``NCBILoginError``, a ``VenueLoginError``
subclass, and leaves the page or provider popup open for recovery.

``login_url`` accepts a service's HTTPS NCBI account-gateway URL, preserving
its callback and state parameters. ``return_url`` optionally opens an HTTPS
NCBI service URL after authentication has been confirmed. Authentication
requires a return to NCBI with a visible, populated account control, rather
than merely a successful provider sign-in. A callback is never reloaded
while authentication is still pending. Both same-tab and popup handoffs
are supported.

The caller owns the browser context, credential storage, and session
persistence. ``NCBIVenue`` (in ``paperpush.venues.ncbi.main``) wraps the helper
as a ``Venue`` for the NCBI services paperpush lists, ``sra`` and ``geo``, so
``paperpush login sra`` and ``paperpush login geo`` sign in through it. The
provider comes from ``--provider`` (``--orcid`` selects ``orcid``) and is stored
with the credential. ``sra`` also has a submission runner
(``paperpush/venues/ncbi/sra.py``) that fills the SRA Submission Portal wizard;
``geo`` is a ``LOGIN_ONLY`` venue with no runner yet, so ``paperpush submit``
still refuses it. There is no standalone
``paperpush login ncbi``; each service keeps its own credential and session.

The service portals (``submit.ncbi.nlm.nih.gov``) do not carry NCBI's
``#account_info`` control, so ``NCBIVenue.is_logged_in`` reads a portal by its
own header: the header is rendered and its sign-in link is gone.

The public gateway and provider entry forms have been inspected. The offline
tests cover all seven branches; successful account authentication, MFA, and
first-time account linking require live testing with the corresponding
accounts before a service runner claims a verified walkthrough.
