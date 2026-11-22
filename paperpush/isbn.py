"""Read and check the ISBNs a reference gives.

An ISBN carries a check digit: the last digit of an ISBN-10 makes the weighted
sum of all ten (weights 10 down to 1) a multiple of 11, with ``X`` standing for
10; the last digit of an ISBN-13 makes the sum of its digits, weighted 1 and 3
alternately, a multiple of 10. A mistyped digit or two swapped neighbours
breaks the sum, and so does an ISBN made up for a reference that does not
exist. :func:`isbn_problem` says what is wrong with one, offline.

Only text labelled as an ISBN is read from a reference-list entry ("ISBN
978-0-387-84857-0", "ISBN-10: 0387848576"), since any 10- or 13-digit run
(a phone number, an accession) is not one. A ``.bib`` ``isbn`` field is read
whole, and may hold several separated by commas, semicolons, or spaces.

The lookup of what book an ISBN belongs to is in
:mod:`paperpush.reference_search` (Open Library).
"""

from __future__ import annotations

import re

# One ISBN as written: an ISBN-13 (978/979, then ten more digits), or an ISBN-10 (nine digits and
# a check digit that may be X), with single hyphens or spaces between the digits.
_ONE = r"(?:97[89](?:[\s-]?\d){9}[\s-]?\d|\d(?:[\s-]?\d){8}[\s-]?[\dXx])(?![\dXx])"
_IN_FIELD = re.compile(r"(?<![\dXx])" + _ONE)
# The label of an ISBN in running text, and what follows it: a well-shaped ISBN, or else a run of
# digits and hyphens ("ISBN 978-0-387-8485"), so a truncated ISBN is reported rather than skipped.
_LABEL = re.compile(r"\bISBN(?:-?1[03])?\s*:?\s*", re.IGNORECASE)
_WELL_SHAPED = re.compile(_ONE)
_RUN = re.compile(r"\d(?:-?[\dXx])*")


def compact(isbn: str) -> str:
    """The ISBN's digits (and a final X), without hyphens or spaces."""
    return re.sub(r"[^\dXx]", "", isbn).upper()


def isbns_in_field(value: str) -> list[str]:
    """The ISBNs a ``.bib`` ``isbn`` field gives, as written; the whole field when it holds no well-shaped one."""
    found = [m.group(0).strip() for m in _IN_FIELD.finditer(value or "")]
    if not found and compact(value or ""):
        return [value.strip()]
    return list(dict.fromkeys(found))


def isbns_in_text(text: str) -> list[str]:
    """The ISBNs a reference-list entry labels as such, as written."""
    found: list[str] = []
    for label in _LABEL.finditer(text or ""):
        m = _WELL_SHAPED.match(text, label.end()) or _RUN.match(text, label.end())
        if m and len(compact(m.group(0))) >= 4:
            found.append(m.group(0).strip(" -"))
    return list(dict.fromkeys(found))


def _isbn10_sum(digits: str) -> int:
    return sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(digits))


def _isbn13_sum(digits: str) -> int:
    return sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(digits))


def isbn_problem(isbn: str) -> str:
    """What is wrong with ``isbn`` as an ISBN ("" when it is a well-formed one with a valid check digit)."""
    digits = compact(isbn)
    if len(digits) == 10:
        if "X" in digits[:-1]:
            return "only an ISBN-10's last character can be X"
        return "" if _isbn10_sum(digits) % 11 == 0 else "its check digit is wrong (a digit was mistyped, or the ISBN was made up)"
    if len(digits) == 13:
        if "X" in digits:
            return "an ISBN-13 has no X"
        if not digits.startswith(("978", "979")):
            return "an ISBN-13 starts with 978 or 979"
        return "" if _isbn13_sum(digits) % 10 == 0 else "its check digit is wrong (a digit was mistyped, or the ISBN was made up)"
    return f"it has {len(digits)} digits, where an ISBN has 10 or 13"


def to_isbn13(isbn: str) -> str:
    """A valid ISBN as its 13-digit form ("" for an invalid one)."""
    if isbn_problem(isbn):
        return ""
    digits = compact(isbn)
    if len(digits) == 13:
        return digits
    body = "978" + digits[:9]
    return body + str((10 - _isbn13_sum(body) % 10) % 10)
