"""False alarms found by validating bioRxiv preprints (Europe PMC full text, as Word files; Nov 2026).

1. Word files written with indented XML (pandoc, some converters): the whitespace between
   elements read as blank lines, so the title page ran out before the title.
2. consistency.abstract: with no "Introduction" heading the abstract ran on into the
   introduction, and a complete .sub abstract was "only part of it".
3. consistency.statements: a manuscript with no competing-interest statement at all was
   "not found"; "declare no competing interests" did not match bioRxiv's default "The authors
   have declared no competing interest.".
4. rigor.code_availability: data "deposited on Zenodo ... and available from the authors upon
   request" were "offered only on request"; its quote started mid-word ("thors declare").
5. prose.brackets: in a paragraph with an unclosed bracket, the ")" of "(Fig. 2)" was blanked
   as an "a)"-style enumerator, so the wrong bracket was reported.
6. prose.punctuation_spacing: "f : X → Y" (a mapping) was a space before a colon.
7. details.reference_order: Vancouver names ("Berry PM") were sorted by their initials.
8. prose.acronyms: lab reagents and tags (DAPI, EDTA, PBS, FLAG) were undefined acronyms.
"""

import zipfile
from pathlib import Path

from paperpush import bibliography_check as B
from paperpush import consistency as K
from paperpush import prose_check as P
from paperpush import reproducibility_check as R
from paperpush.manuscript import docx_to_text
from paperpush.style_check import _KNOWN


def _docx(path: Path, body_xml: str) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8"?>\n<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n<w:body>\n' + body_xml + "\n</w:body>\n</w:document>")
    return path


def test_indented_docx_xml_does_not_read_as_blank_lines(tmp_path):
    paragraphs = "".join(f'\n    <w:p>\n      <w:pPr>\n        <w:pStyle w:val="Author" />\n      </w:pPr>\n      <w:r><w:t xml:space="preserve">{t}</w:t></w:r>\n    </w:p>' for t in ("A title", "Ann Author", "Text with  two spaces"))
    text = docx_to_text(_docx(tmp_path / "m.docx", paragraphs))
    assert text.splitlines()[:3] == ["A title", "Ann Author", "Text with  two spaces"]


def test_statement_topics_and_none_declared():
    topic = dict(K._STATEMENT_TOPICS)
    ci = next(p for k, p in K._STATEMENT_TOPICS if "competing" in k)
    assert ci.search("Conflict of Interest Statement") and not ci.search("We studied interests of the field.")
    assert topic  # every entry compiles
    for none in ("The authors have declared no competing interest.", "The authors declare that they have no competing or financial interests.", "None declared.", "None."):
        assert K._NONE_DECLARED.search(none), none
    for real in ("JPM is CSO of YouthBio Therapeutics, a company developing gene therapies.", "This work received partial financial support from Adisseo France S.A.S."):
        assert not K._NONE_DECLARED.search(real), real


def test_data_deposited_and_on_request_is_not_only_on_request():
    text = "All datasets are deposited on Zenodo Hidalgo Cenalmor et al. (2026). The authors declare that the data are available within the article and from the authors upon request."
    assert R._DEPOSITED.search(text)
    assert not R._DEPOSITED.search("The data are available from the corresponding author upon reasonable request.")
    quote = R._quote("x" * 200 + " The authors declare that the data are available from the authors upon request.", R._UPON_REQUEST)
    assert quote.split()[0].isalpha() and not quote.startswith("x")


def test_closing_bracket_after_a_number_is_not_an_enumerator_when_a_bracket_is_open():
    text = "we re-examined the data of P. atacamensis (Fig. 2). In each background b (ΔF(b) against the fitness."
    found = P.unbalanced(text)
    assert len(found) == 1 and "ΔF(b)" in found[0]
    assert P.unbalanced("We tested a) first and b) second.") == []


def test_mapping_colon_is_not_a_spacing_slip():
    assert P.punctuation_spacing("a distribution P over such functions f : X → Y. The inputs") == []
    assert P.punctuation_spacing("we found this : it was bad.")


def test_vancouver_initials_are_not_the_sort_surname():
    assert B._sort_surname("Berry PM, et al. (2004). Understanding lodging") == "Berry"
    assert B._sort_surname("Graf AB, Borer S, Radig B (2001). Normalization") == "Graf"
    assert B._sort_surname("Wu X, Li Y (2020). T") == "Wu"


def test_common_reagents_need_no_definition():
    assert {"DAPI", "EDTA", "PBS", "FLAG", "DMSO", "GFP"} <= _KNOWN


def test_abstract_ending_at_a_paragraph_is_whole(tmp_path):
    from paperpush import document

    abstract = "Bulk RNA-seq enables profiling of large cohorts. Here we present Rectangle, a framework for deconvolution of bulk data with explicit modeling of unknown content."
    intro = "The structure and function of tissues emerge from the composition and spatial organization of their cells. " * 4
    paras = ["A title", "Abstract", abstract, intro, "Results", "We found things."]
    body = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in paras)
    path = _docx(tmp_path / "m.docx", body)
    doc = document.get(path)
    assert K._abstract_cut_short(abstract, doc, path) == []
    # Only part of the first paragraph is still reported.
    assert K._abstract_cut_short("Bulk RNA-seq enables profiling of large cohorts.", doc, path)
