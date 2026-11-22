"""Fixes from the ICLR 2026 v9 run: free OpenAlex lookups, OpenAlex name parsing, organization
authors, and the two papers that timed out on every run (shared PDF text, bounded image decoding)."""
from __future__ import annotations

import urllib.parse

import paperpush.reference_search as rs
from paperpush import colorblind, manuscript


class _FakeOpenAlex:
    """Answers the autocomplete and single-work endpoints from dicts; records every URL."""

    def __init__(self, autocomplete: dict[str, list[dict]], works: dict[str, dict]):
        self.autocomplete, self.works, self.urls = autocomplete, works, []
        self.available = True

    def get_json(self, url: str):
        self.urls.append(url)
        parts = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        if parts.path.endswith("/autocomplete/works"):
            return {"results": self.autocomplete.get(q["q"], [])}
        wid = parts.path.rsplit("/", 1)[-1]
        return self.works.get(wid)


def _hit(wid, title, cited):
    return {"id": f"https://openalex.org/{wid}", "display_name": title, "cited_by_count": cited}


def _work(title, names, year=2022):
    return {"display_name": title, "publication_year": year, "authorships": [{"author": {"display_name": n}} for n in names], "doi": None, "type": "article"}


PHENAKI = "Phenaki: Variable length video generation from open domain textual descriptions"


def test_free_lookup_prefers_the_well_cited_duplicate(monkeypatch):
    # The whole-title query finds only a record with unparsed names; the first-words chunk brings
    # the clean, more cited one, which has to come first.
    norm = rs._normalize_text(PHENAKI)
    words = norm.split()
    svc = _FakeOpenAlex(
        {norm: [_hit("W1", "Phenaki: Variable Length Video Generation from Open Domain Textual Descriptions", 119)],
         " ".join(words[:4]): [_hit("W2", "Phenaki: Variable Length Video Generation from Open Domain Textual Descriptions", 358), _hit("W1", "Phenaki: Variable Length Video Generation from Open Domain Textual Descriptions", 119)]},
        {"W1": _work("Phenaki", ["Villegas, R", "H. Hernan Moraldo"]), "W2": _work("Phenaki", ["Ruben Villegas", "Mohammad Babaeizadeh"])},
    )
    monkeypatch.setattr(rs, "_openalex_paid_search", lambda t, s: (_ for _ in ()).throw(AssertionError("paid search used")))
    records = rs.openalex_search(PHENAKI, svc)
    assert [a["literal"] for a in records[0]["author"]] == ["Ruben Villegas", "Mohammad Babaeizadeh"]
    assert records[1]["author"][0]["family"] == "Villegas"


def test_paid_search_only_when_autocomplete_finds_no_similar_title(monkeypatch):
    svc = _FakeOpenAlex({}, {})
    calls = []
    monkeypatch.setattr(rs, "_openalex_paid_search", lambda t, s: calls.append(t) or [])
    assert rs.openalex_search("A completely invented title about spectral reweighting of transformers", svc) == []
    assert len(calls) == 1
    # Paid budgets spent (None): autocomplete's index lacks some works, so its miss is no answer.
    monkeypatch.setattr(rs, "_openalex_paid_search", lambda t, s: None)
    assert rs.openalex_search("A completely invented title about spectral reweighting of transformers", svc) is None


def _atom(*titles):
    entries = "".join(
        f"<entry><id>http://arxiv.org/abs/2110.1416{i}v2</id><title>{t}</title><published>2021-10-27T00:00:00Z</published>"
        f"<author><name>Karl Cobbe</name></author><author><name>Vineet Kosaraju</name></author></entry>"
        for i, t in enumerate(titles)
    )
    return f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode()


def _ref(title):
    return rs.Reference(label="bibliography entry 'c'", where="x.bib", title=title, author="Cobbe", year="2021", kind="inproceedings")


def _arxiv_answers(monkeypatch, body):
    def get(self, url, accept="application/json", missing_ok=False):
        if "export.arxiv.org" in url:
            return body
        return None
    monkeypatch.setattr(rs._Service, "get", get)


def test_arxiv_title_search_finds_what_openalex_autocomplete_lacks(monkeypatch):
    monkeypatch.setattr(rs, "crossref_search", lambda q, s: [])
    monkeypatch.setattr(rs, "openalex_search", lambda t, s: None)
    _arxiv_answers(monkeypatch, _atom("Training Verifiers to Solve Math Word Problems"))
    result = rs.search_references([_ref("Training verifiers to solve math word problems")], services=rs._services())
    assert [lk.status for lk in result.lookups] == [rs.MATCHED]
    assert result.findings == []


def test_miss_in_crossref_and_arxiv_titles_is_reported(monkeypatch):
    monkeypatch.setattr(rs, "crossref_search", lambda q, s: [])
    monkeypatch.setattr(rs, "openalex_search", lambda t, s: None)
    _arxiv_answers(monkeypatch, _atom())
    result = rs.search_references([_ref("Spectral reweighting of gradient free adversarial diffusion alignment")], services=rs._services())
    assert [lk.status for lk in result.lookups] == [rs.NOT_FOUND]
    assert [f.category for f in result.findings] == ["no registered work"]
    assert "Crossref and arXiv titles" in result.findings[0].detail


def test_no_answer_from_openalex_or_arxiv_is_not_an_accusation(monkeypatch):
    monkeypatch.setattr(rs, "crossref_search", lambda q, s: [])
    monkeypatch.setattr(rs, "openalex_search", lambda t, s: None)
    result = rs.search_references([_ref("Spectral reweighting of gradient free adversarial diffusion alignment")], services=rs._services())
    assert [lk.status for lk in result.lookups] == [rs.UNKNOWN]
    assert result.findings == []


def test_autocomplete_text_keeps_apostrophes_and_drops_run_on_text():
    assert rs._autocomplete_text("Let\u2019s verify step by step") == "let's verify step by step"
    assert rs._autocomplete_text("Training verifiers to solve math word problems, 2021") == "training verifiers to solve math word problems"
    assert rs._autocomplete_text("MedDr: Diagnosis-guided bootstrapping.arXiv preprint, 2024") == "meddr diagnosis guided bootstrapping"


def test_openalex_author_names():
    assert rs._openalex_author("Villegas, R") == {"family": "Villegas", "literal": "R Villegas"}
    assert rs._openalex_author("CortesCorinna") == {"family": "Cortes", "literal": "Corinna Cortes"}
    assert rs._openalex_author("Yann LeCun")["family"] == "LeCun"
    assert rs._openalex_author("LeCun")["family"] == "LeCun"
    assert rs._openalex_author("McDonald")["family"] == "McDonald"


def test_organization_author_is_not_a_wrong_author_list():
    text = "Meta AI Research. Llama 2: Open foundation and fine-tuned chat models. Technical report, Meta Platforms, Inc., 2023."
    record = {"title": "Llama 2: Open Foundation and Fine-Tuned Chat Models", "author": [{"family": f, "literal": f} for f in ("Touvron", "Martin", "Stone", "Albert")], "issued": {"date-parts": [[2023]]}}
    problems = rs._prose_problems(text, set(rs._normalize_text(text).split()), record, 1.0)
    assert not any(p.startswith(rs.WRONG_AUTHORS) or "registered first author" in p for p in problems)
    # People named instead of the organization are still checked.
    wrong = "Yusheng Yao, Shikun Zhang, Xingcheng Pan, and Peng Zhang. Llama 2: Open foundation and fine-tuned chat models. 2023."
    assert rs._prose_problems(wrong, set(rs._normalize_text(wrong).split()), record, 1.0)


def test_pdf_text_is_extracted_once_for_every_reader(monkeypatch, tmp_path):
    import pypdf

    pdf = tmp_path / "p.pdf"
    w = pypdf.PdfWriter()
    w.add_blank_page(612, 792)
    w.write(str(pdf))
    opened = []
    real = pypdf.PdfReader

    def counting(*a, **k):
        opened.append(1)
        return real(*a, **k)

    manuscript._PDF_RAW_CACHE.clear()
    monkeypatch.setattr(pypdf, "PdfReader", counting)
    assert manuscript.pdf_raw_pages(pdf) == ([""], 0)
    assert manuscript.pdf_raw_pages(pdf) == ([""], 0)
    assert manuscript._pdf_pages_pypdf(pdf) == [""]
    assert len(opened) == 1


class _Raw(dict):
    def get_object(self):
        return self


class _Images:
    def __init__(self, sizes):
        self.sizes, self.decoded = sizes, []

    def keys(self):
        return [f"/Im{i}" for i in range(len(self.sizes))]

    def __getitem__(self, key):
        self.decoded.append(key)
        w, h = self.sizes[int(key[3:])]

        class _I:
            image = type("Img", (), {"width": w, "height": h})()

        return _I()


class _Page(dict):
    def __init__(self, sizes):
        super().__init__({"/Resources": _Raw({"/XObject": _Raw({f"/Im{i}": _Raw({"/Width": w, "/Height": h}) for i, (w, h) in enumerate(sizes)})})})
        self.images = _Images(sizes)


def test_page_images_decodes_only_large_enough_and_at_most_a_dozen():
    sizes = [(50, 50)] * 100 + [(400, 300)] * 20 + [(20000, 20000)]
    page = _Page(sizes)
    out = colorblind._page_images(page, 200)
    assert len(out) == colorblind.MAX_IMAGES_PER_PAGE
    assert len(page.images.decoded) == colorblind.MAX_IMAGES_PER_PAGE
    assert "/Im120" not in page.images.decoded  # 400 MP: not decoded


def test_team_names_are_not_a_given_name_conflict():
    record = {"title": "InternAgent", "author": [{"family": "Team", "given": "InternAgent", "literal": "InternAgent Team"}], "issued": {"date-parts": [[2025]]}}
    assert rs._lead_given_conflict("NovelSeek Team. NovelSeek: When agent becomes the scientist. arXiv:2505.16938, 2025.", record) == ""


def test_wrapped_url_line_runs_on_into_the_entry_before():
    from paperpush import bibliography_check as bc

    assert bc._runs_on("Module_Networks_CVPR_2016_paper.html.")
    assert bc._runs_on("paper_files/paper/2020/file/abc.pdf")
    assert not bc._runs_on("Matej Balog, Alexander L. Gaunt, Marc Brockschmidt, and Daniel Tarlow.")
    assert not bc._runs_on("Anthropic. System card: Claude.")


def test_misread_titles_are_not_searched_or_compared():
    assert rs._not_a_title("Ilya Loshchilov and Frank Hutter")
    assert rs._not_a_title("URL https://arxiv.org/ abs/2407.21783")
    assert rs._not_a_title("Proceedings of the National Academy of Sciences, 115(34):8505–8510, 2018")
    assert not rs._not_a_title("Neural ordinary differential equations")
    # "Weinan E." made the venue read as the title, which rejected Crossref's right record.
    text = "Jiequn Han, Arnulf Jentzen, and Weinan E. Solving high-dimensional partial differential equations using deep learning. Proceedings of the National Academy of Sciences, 115(34):8505–8510, 2018."
    record = {"title": ["Solving high-dimensional partial differential equations using deep learning"], "author": [{"family": "Han", "given": "Jiequn"}, {"family": "Jentzen", "given": "Arnulf"}], "issued": {"date-parts": [[2018]]}, "type": "journal-article", "DOI": "10.1073/pnas.1718942115"}
    lk = rs.match_text(rs.Reference(label="x", where="p", text=text), [record])
    assert lk.status == rs.MATCHED and not lk.problems


def test_registered_middle_initial_is_the_same_person():
    record = {"title": "Neural Ordinary Differential Equations", "author": [{"family": "Chen", "given": "Ricky T Q", "literal": "Ricky T Q Chen"}]}
    assert rs._lead_given_conflict("Tian Qi Chen, Yulia Rubanova, and David Duvenaud. Neural ordinary differential equations. 2018.", record) == ""
    other = {"title": "FILIP", "author": [{"family": "Yao", "given": "Lewei", "literal": "Lewei Yao"}]}
    assert rs._lead_given_conflict("Yusheng Yao, Shikun Zhang, Xingcheng Pan, Peng Zhang, et al. Filip: Fine-grained interactive language-image pre-training. In ICML, 2022.", other)


def test_arxiv_title_search_uses_the_longest_words_then_fewer(monkeypatch):
    urls = []

    def get(self, url, accept="application/json", missing_ok=False):
        urls.append(urllib.parse.unquote_plus(url))
        return _atom() if len(urls) <= 1 else _atom("Faster Diffusion via Temporal Attention Decomposition")

    monkeypatch.setattr(rs._Service, "get", get)
    found = rs.arxiv_title_search("Faster diffusion through temporal attention decomposition", rs._services().arxiv)
    assert [r["title"] for r in found] == ["Faster Diffusion via Temporal Attention Decomposition"]
    assert "through" not in urls[0] and "ti:decomposition" in urls[0]
    assert len(urls) == 2 and urls[-1].count("ti:") == 3


def test_arxiv_requests_are_paced_across_processes(monkeypatch, tmp_path):
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    slept = []
    monkeypatch.setattr(rs.time, "sleep", lambda s: slept.append(s))
    rs._pace_across_processes("arXiv", 3.5)
    rs._pace_across_processes("arXiv", 3.5)  # a second process right after: waits
    assert slept and 3.0 < slept[-1] <= 3.5
    assert (tmp_path / "paperpush-arxiv.pace").exists()
    assert rs._services().arxiv.shared_pace


def test_empty_arxiv_feeds_are_not_cached(monkeypatch, tmp_path):
    import io

    monkeypatch.undo()  # drop conftest's offline stub of _Service.get; urlopen is stubbed below
    monkeypatch.setenv("PAPERPUSH_LOOKUP_CACHE", str(tmp_path))
    bodies = iter([b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>', _atom("A Title")])

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(rs.urllib.request, "urlopen", lambda req, timeout=None: _Resp(next(bodies)))
    monkeypatch.setattr(rs, "_pace_across_processes", lambda *a: None)
    svc = rs._Service("arXiv", 5)
    url = "https://export.arxiv.org/api/query?id_list=2110.14168"
    assert b"<entry" not in svc.get(url, accept="application/atom+xml")
    assert rs._lookup_cache_read(url, "application/atom+xml") is None
    assert b"<entry" in svc.get(url, accept="application/atom+xml")
    assert rs._lookup_cache_read(url, "application/atom+xml") is not None


def test_a_429_puts_off_every_process(monkeypatch, tmp_path):
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    slept = []
    monkeypatch.setattr(rs.time, "sleep", lambda s: slept.append(s))
    rs._pace_across_processes("arXiv", 3.5, backoff=30)
    assert 29 < slept[-1] <= 30
    rs._pace_across_processes("arXiv", 3.5)  # another process: waits out the backoff too
    assert 29 < slept[-1] <= 30


_LORA = {"data": {"attributes": {"doi": "10.48550/arxiv.2106.09685", "titles": [{"title": "LoRA: Low-Rank Adaptation of Large Language Models"}], "creators": [{"familyName": "Hu", "givenName": "Edward J."}, {"name": "Shen, Yelong"}], "publicationYear": 2021, "version": "2", "relatedIdentifiers": [{"relationType": "IsVersionOf", "relatedIdentifier": "10.5555/journal.1", "relatedIdentifierType": "DOI"}]}}}


def test_datacite_answers_arxiv_ids_and_a_404_means_no_such_paper(monkeypatch):
    import json as _json

    def get(self, url, accept="application/json", missing_ok=False):
        if "api.datacite.org/dois/10.48550/arxiv.2106.09685" in url:
            return _json.dumps(_LORA).encode()
        if "api.datacite.org/dois/10.48550/arxiv.2106.99999" in url:
            return b"" if missing_ok else None
        return None  # DataCite fails for the third id; arXiv is down too

    monkeypatch.setattr(rs._Service, "get", get)
    records, checked = rs._arxiv_records(["2106.09685", "2106.99999", "2401.00001"], rs._services())
    assert checked == {"2106.09685", "2106.99999"}
    rec = records["2106.09685"]
    assert [a["family"] for a in rec["author"]] == ["Hu", "Shen"] and rec["latest_version"] == 2 and rec["journal_doi"] == "10.5555/journal.1"
    result = rs.search_references([rs.Reference(label="r", where="x", text="Hu et al. LoRA. arXiv:2106.99999, 2021.", arxiv_id="2106.99999")], services=rs._services())
    assert result.lookups[0].status == rs.MATCHED and "no record" in result.lookups[0].problems[0]


def test_arxiv_api_answers_what_datacite_did_not(monkeypatch):
    monkeypatch.setattr(rs, "arxiv_lookup", lambda ids, service: {i: {"title": "T", "author": []} for i in ids})
    records, checked = rs._arxiv_records(["2401.00001"], rs._services())  # DataCite stubbed offline
    assert checked == {"2401.00001"} and "2401.00001" in records


def test_datacite_title_search_tries_the_whole_title_first(monkeypatch):
    import json as _json

    queries = []

    def get(self, url, accept="application/json", missing_ok=False):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["query"]
        queries.append(q)
        hit = {"attributes": dict(_LORA["data"]["attributes"], titles=[{"title": "Let's Verify Step by Step"}], doi="10.48550/arxiv.2305.20050")}
        return _json.dumps({"data": [hit] if q.startswith('titles.title:"') else []}).encode()

    monkeypatch.setattr(rs._Service, "get", get)
    found = rs.datacite_arxiv_title_search("Let’s verify step by step", rs._services().datacite)
    assert queries == ['titles.title:"let\'s verify step by step"']
    assert found[0]["title"] == "Let's Verify Step by Step" and found[0]["DOI"] == "10.48550/arxiv.2305.20050"
