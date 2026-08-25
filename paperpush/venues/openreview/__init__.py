"""OpenReview-hosted venues.

OpenReview (https://openreview.net) hosts the submission workflow for many
conferences. The parts that are OpenReview rather than the conference -- signing
in, and the profile-search author widget -- live in :mod:`.main`; each conference
is a separate venue leaf under this subpackage (``<slug>.py`` exposing a
module-level ``VENUE``) carrying only its own submission form. AAAI 2027 and
ICLR 2027 are the venues here.
"""
