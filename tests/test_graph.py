"""Tests for the citation graph: maths, settings, reference reading, rounds."""

import gzip
import json

import numpy as np
import pytest
from test_gautam import JATS, lib  # noqa: F401  (lib is a pytest fixture)

from gautam.errors import SettingsError
from gautam.graph import GraphExpander, GraphSettings
from gautam.harvest import Harvester
from gautam.io.references import JatsReferenceReader, make_reference
from gautam.numerics import graph_weights as gw

REFS = b"""<back><ref-list>
<ref id="r1"><element-citation>
<pub-id pub-id-type="doi">10.1000/classic</pub-id>
<pub-id pub-id-type="pmid">111</pub-id></element-citation></ref>
<ref id="r2"><mixed-citation>
<pub-id pub-id-type="pmid">222</pub-id></mixed-citation></ref>
<ref id="r3"><mixed-citation><ext-link xlink:href="https://doi.org/10.1000/TEM1"
 xmlns:xlink="http://www.w3.org/1999/xlink">x</ext-link></mixed-citation></ref>
</ref-list></back>"""


def _article(pmc: str) -> bytes:
    body = JATS.split(b"?>", 1)[1].replace(
        b"<article-meta>",
        b'<article-meta><article-id pub-id-type="pmc">' + pmc.encode()
        + b"</article-id>")
    return body.replace(b"<back><ref-list><ref>ignored</ref></ref-list>"
                        b"</back>", REFS)


# ------------------------------------------------------------------ maths
def test_citation_weight_decays_with_hops():
    """A citation from one step out counts half (decay 0.5)."""
    w = gw.citation_weight([0, 0, 1], [0, 1, 0], 2, decay=0.5)
    assert np.allclose(w, [1.5, 1.0])


def test_combined_weight_and_select():
    """Weights combine both signals; selection follows the eligibility rule."""
    w = gw.combined_weight([4, 1, 2], [0.9, 0.9, np.nan], beta=0.5)
    assert w[2] == 0 and w[0] > w[1]
    idx = gw.select(w, [4, 1, 2], [0.9, 0.9, np.nan], 3, 0.6, budget=5)
    assert list(idx) == [0, 1]  # third: cited twice, no relevance -> out
    assert gw.topk_mean(np.array([[1, 2, 3, 4]]), 2)[0] == 3.5
    assert gw.coverage(1, 4) == 0.25 and gw.coverage(0, 0) == 0.0


# ------------------------------------------------------------------ settings
def test_settings_checks_and_suggests():
    """Bad names get a suggestion; bad values name the allowed range."""
    assert GraphSettings.from_mapping({"decay": 0.3}).decay == 0.3
    with pytest.raises(SettingsError, match="did you mean 'budget'"):
        GraphSettings.from_mapping({"budjet": 10})
    with pytest.raises(SettingsError, match="beta"):
        GraphSettings(beta=2)


# ------------------------------------------------------------------ reading
def test_jats_reference_reader():
    """DOIs and PMIDs are read from pub-id tags and doi.org links."""
    out = JatsReferenceReader().read_bytes(
        b"<pmc-articleset>" + _article("777") + b"</pmc-articleset>")
    keys = [r.key for r in out["777"]]
    assert keys == ["doi:10.1000/classic", "pmid:222", "doi:10.1000/tem1"]
    assert make_reference("https://doi.org/10.1/X.", None).doi == "10.1/x"


def test_parallel_xml_reading_matches_serial(tmp_path):
    """Reading files on several cores gives the same references."""
    for i in range(6):
        art = (b"<pmc-articleset>" + _article(str(800 + i))
               + b"</pmc-articleset>")
        (tmp_path / f"f{i}.xml.gz").write_bytes(gzip.compress(art))
    reader = JatsReferenceReader()
    serial = dict(reader.read_folder(tmp_path, workers=1))
    parallel = dict(reader.read_folder(tmp_path, workers=3))
    assert serial == parallel and len(parallel) == 6


# ------------------------------------------------------------------ a round
class _Resp:
    """Stand-in for an HTTP response."""

    def __init__(self, payload, code=200):
        self.status_code, self._p = code, payload

    def json(self):
        """Return the payload."""
        return self._p


def test_round_adds_cited_classic_and_raises_coverage(lib, tmp_path):  # noqa: F811,E501
    """A cited classic is looked up, added as an abstract, and resolved."""
    L, _ = lib
    L.update()
    H = Harvester(L)
    H.db.execute(
        "INSERT INTO candidates (pmcid, doi, title, year, topics) VALUES "
        "('PMC777','10.1371/journal.pcbi.000001','x',2021,'t')")
    xml_dir = tmp_path / "xml"
    xml_dir.mkdir()
    art = b"<pmc-articleset>" + _article("777") + b"</pmc-articleset>"
    (xml_dir / "a.xml.gz").write_bytes(gzip.compress(art))
    H.ingest(str(xml_dir))
    L.update()

    classic = {"pmid": "111", "doi": "10.1000/classic", "pubYear": "1991",
               "title": "Conformational substates in a protein structure",
               "abstractText": "Omega loop dynamics and conformational "
                               "substates in TEM-1 beta-lactamase.",
               "isOpenAccess": "N"}

    def fake_get(url, params=None, timeout=120, tries=6):
        if url.endswith("/search"):
            q = params["query"]
            hits = [classic] if ("10.1000/classic" in q or "111" in q) else []
            return _Resp({"resultList": {"result": hits}})
        if "/references" in url:
            return _Resp({"referenceList": {"reference": []}})
        return _Resp({"message": {"reference": []}})

    H._get = fake_get
    settings = GraphSettings(min_citations=1, min_relevance=0.0, budget=10)
    G = GraphExpander(L, H, settings)
    posts = []

    def fake_post(url, data):
        posts.append(data["id"])
        return _Resp({"linksets": [{"ids": [int(i)], "linksetdbs": [
            {"linkname": "pubmed_pubmed_refs", "links": [333]}]}
            for i in data["id"]]})

    G.ncbi_refs._post = fake_post
    first = G.collect_references(xml_dir, online=True, crossref=True)
    assert first["references"] == 3 and first["in_library"] == 1  # TEM-1
    hist = G.run(rounds=2, xml_dir=None)
    assert hist[0]["added"] == 1
    assert hist[0]["coverage_after"] > hist[0]["coverage_before"]
    p = L.store.paper_by_doi("10.1000/classic")
    assert p is not None and p["type"] == "abstract" and p["graph_hop"] == 1
    # the added classic's own reference list came from NCBI in one batch
    assert posts and posts[0] == ["111"]
    src = G.graph.db.execute("SELECT source FROM ref_sources WHERE "
                             "paper_id=?", (p["id"],)).fetchone()[0]
    assert src == "ncbi"
    top = G.graph.db.execute(
        "SELECT status FROM graph_nodes WHERE key='doi:10.1000/classic'")
    assert top.fetchone()[0] == "added"
    assert json.loads(json.dumps(G.graph.coverage()))["coverage"] > 0.3
