"""Offline tests: fake PDFs, the hash embedder, and a fake Crossref. Run: uv run --with pytest pytest -q"""
import json
from pathlib import Path

import pymupdf
import pytest

from gautam import bib, scholarly
from gautam.config import load_config
from gautam.library import Library
from gautam import tools

PARA = ("The omega loop of TEM-1 beta-lactamase shows large conformational fluctuations on the microsecond "
        "timescale, and these motions gate substrate entry into the active site. ")
OTHER = ("Loop 6 of triosephosphate isomerase closes over the active site during catalysis, and its hinge "
         "residues control the rate of closure in the TIM barrel enzyme. ")


def make_pdf(path: Path, title: str, body: str, doi: str | None, pages: int = 3):
    doc = pymupdf.open()
    for i in range(pages):
        pg = doc.new_page()
        y = 72
        if i == 0:
            pg.insert_text((72, y), title, fontsize=14)
            y += 30
            if doi:
                pg.insert_text((72, y), f"https://doi.org/{doi}", fontsize=9)
                y += 30
        pg.insert_textbox(pymupdf.Rect(72, y, 520, 760), (body * 6).strip(), fontsize=10)
    doc.set_metadata({"title": title})
    doc.save(path)


FAKE = {
    "10.1000/tem1": {"DOI": "10.1000/tem1", "title": ["Omega loop dynamics in TEM-1 beta-lactamase"],
                     "author": [{"given": "Sarath", "family": "Dantu"}], "issued": {"date-parts": [[2019]]},
                     "container-title": ["J Mol Biol"], "type": "journal-article", "volume": "431", "page": "1-10"},
    "10.1000/retracted": {"DOI": "10.1000/retracted", "title": ["A retracted study"],
                          "author": [{"given": "A", "family": "Smith"}], "issued": {"date-parts": [[2015]]},
                          "type": "journal-article", "container-title": ["Nature"],
                          "updated-by": [{"type": "retraction", "DOI": "10.1000/notice"}]},
}


@pytest.fixture
def lib(tmp_path, monkeypatch):
    lib_dir = tmp_path / "pdfs"
    lib_dir.mkdir()
    cfg_file = tmp_path / "gautam.toml"
    cfg_file.write_text(f'[library]\ndirs=["{lib_dir}"]\n[embedding]\nbackend="hash"\ndim=256\n')
    monkeypatch.setattr(scholarly, "crossref_work",
                        lambda doi, email="": scholarly.parse_crossref(FAKE[doi]) if doi in FAKE else None)

    def fake_search(title, email="", author="", rows=3):
        return [scholarly.parse_crossref(m) for m in FAKE.values()
                if scholarly.title_similarity(m["title"][0], title) > 0.5][:rows]
    monkeypatch.setattr(scholarly, "crossref_search", fake_search)
    monkeypatch.setattr(scholarly, "find_open_pdf", lambda doi, email="": [])
    make_pdf(lib_dir / "tem1.pdf", "Omega loop dynamics in TEM-1 beta-lactamase", PARA, "10.1000/tem1")
    make_pdf(lib_dir / "tim.pdf", "Loop 6 closure in triosephosphate isomerase", OTHER, None)
    return Library(load_config(cfg_file)), lib_dir


def test_index_search_incremental(lib):
    L, d = lib
    r = L.update()
    assert len(r.added) == 2 and not r.failed
    tem = L.store.paper_by_doi("10.1000/tem1")
    assert tem["citekey"] == "dantu2019omega" and tem["meta_source"] == "crossref"
    hits = L.search("omega loop substrate entry TEM-1")
    assert hits and hits[0]["citekey"] == "dantu2019omega"
    hits = L.search("triosephosphate isomerase hinge")
    assert "triosephosphate" in hits[0]["title"].lower()
    # nothing changed -> nothing to do
    assert L.scan()[0] == []
    # rename = move, not re-index
    (d / "tim.pdf").rename(d / "tim_renamed.pdf")
    r = L.update()
    assert len(r.moved) == 1 and not r.added and not r.removed
    # delete = removed from index
    (d / "tim_renamed.pdf").unlink()
    r = L.update()
    assert len(r.removed) == 1 and L.store.stats()["papers"] == 1


def test_bib_roundtrip_export_and_summary(lib):
    L, _ = lib
    L.update()
    r = tools.export_bib(L)
    entries = bib.parse_bib(r["bibtex"])
    e = [x for x in entries if x["ID"] == "dantu2019omega"][0]
    assert e["doi"] == "10.1000/tem1" and e["year"] == "2019" and "Dantu" in e["author"]
    assert "not_found" in r and r["n"] == 2
    t = tools.paper_text(L, "dantu2019omega")
    assert "omega loop" in t["text"].lower() and "## Key points" in t["summary_template"]
    out = tools.save_summary(L, "10.1000/tem1", "# test summary")
    assert Path(out).read_text().startswith("# test summary")


def test_verify_and_fetch(lib, tmp_path):
    L, _ = lib
    L.update()
    b = tmp_path / "refs.bib"
    b.write_text("""
@article{good, author={Dantu, Sarath}, title={Omega loop dynamics in {TEM-1} beta-lactamase}, year={2019}, doi={10.1000/tem1}}
@article{fake, author={Nobody, A}, title={A study that does not exist at all}, year={2020}, doi={10.9999/nope}}
@article{nodoi, author={Smith, A}, title={A retracted study}, year={2015}}
@article{wrongyear, author={Dantu, S}, title={Omega loop dynamics in TEM-1 beta-lactamase}, year={2009}, doi={https://doi.org/10.1000/TEM1}}
""")
    r = tools.verify_bib(L, str(b))
    by = {p["key"]: p for p in r["problems"]}
    assert "good" not in by
    assert any("does not exist" in x for x in by["fake"]["problems"])
    assert by["nodoi"]["status"] == "retracted" and by["nodoi"]["fixes"]["doi"] == "10.1000/retracted"
    assert by["wrongyear"]["fixes"]["year"] == "2019"
    fixed = bib.parse_bib(Path(r["fixed_bib"]).read_text())
    assert {e["ID"] for e in fixed} == {"good", "fake", "nodoi", "wrongyear"}
    f = tools.fetch_missing(L, str(b), index_after=False)
    assert f["already_in_library"] == 2  # good + wrongyear point at the same paper
    assert Path(f["manual_list"]).exists()


def test_chunker_and_doi():
    from gautam.pdftext import Block, find_doi, make_chunks
    assert find_doi("see https://doi.org/10.1021/acs.jctc.5b00255.") == "10.1021/acs.jctc.5b00255"
    blocks = [Block(1, "Introduction")] + [Block(1 + i // 3, PARA * 3) for i in range(9)]
    ch = make_chunks(blocks)
    assert ch and all(c.section == "Introduction" for c in ch)
    assert max(len(c.text.split()) for c in ch) <= 380 + 40


def test_parallel_matches_serial(lib, tmp_path):
    L, d = lib
    for i in range(6):
        make_pdf(d / f"extra{i}.pdf", f"Extra paper number {i} on allostery", OTHER + f" Variant {i}. ", None)
    make_pdf(d / "dup.pdf", "Omega loop dynamics in TEM-1 beta-lactamase", PARA, "10.1000/tem1")  # same bytes? no: new file
    r = L.update(workers=3)
    assert not r.failed, r.failed
    assert L.store.stats()["papers"] >= 8
    assert "reading PDFs" in r.summary()
    assert L.search("omega loop substrate entry TEM-1")[0]["doi"] == "10.1000/tem1"


def test_stages_resumable(lib):
    L, d = lib
    r = L.update(stages=("read",))
    assert len(r.added) == 2 and L.store.count_unembedded() > 0
    assert L.search("omega loop")  # keyword search already works before fingerprints
    assert L.store.paper_by_doi("10.1000/tem1")["meta_source"] == "pdf"
    L.update(stages=("meta",))
    assert L.store.paper_by_doi("10.1000/tem1")["citekey"] == "dantu2019omega"
    r = L.update()  # nothing new to read; finishes fingerprints
    assert L.store.count_unembedded() == 0 and r.embedded > 0
    assert {p["stage"] for p in L.store.all_papers()} == {"done"}


JATS = b"""<?xml version="1.0"?>
<article><front><journal-meta><journal-title-group><journal-title>PLoS Comput Biol</journal-title></journal-title-group></journal-meta>
<article-meta><article-id pub-id-type="doi">10.1371/journal.pcbi.000001</article-id>
<title-group><article-title>Skp1 dynamics in the SCF complex</article-title></title-group>
<contrib-group><contrib contrib-type="author"><name><surname>Dantu</surname><given-names>Sarath</given-names></name></contrib></contrib-group>
<pub-date><year>2021</year></pub-date><volume>17</volume><fpage>1</fpage><lpage>20</lpage>
<abstract><p>Skp1 links the F-box protein to Cul1 and its C-terminal helices are highly dynamic in solution.</p></abstract></article-meta></front>
<body><sec><title>Results</title><p>""" + (b"The disordered C-terminal region of Skp1 samples many conformations and binds F-box domains. " * 8) + b"""</p>
<fig><caption><p>NMR relaxation of Skp1.</p></caption></fig></sec></body><back><ref-list><ref>ignored</ref></ref-list></back></article>"""


def test_harvest_parse_and_fetch(lib, monkeypatch):
    from gautam import harvest
    L, _ = lib
    meta, blocks = harvest.parse_jats(JATS)
    assert meta["doi"] == "10.1371/journal.pcbi.000001" and meta["authors"][0]["family"] == "Dantu"
    assert any(b.text.startswith("Figure:") for b in blocks) and not any("ignored" in b.text for b in blocks)
    H = harvest.Harvester(L)
    H.db.execute("INSERT INTO candidates (pmcid, doi, title, year, topics) VALUES ('PMC1','10.1371/journal.pcbi.000001','x',2021,'Skp1')")
    H.db.execute("INSERT INTO candidates (pmcid, doi, title, year, topics) VALUES ('PMC2','10.1000/tem1','dup',2019,'x')")
    L.update()
    monkeypatch.setattr(H, "_download", lambda pmcid: JATS)
    r = H.fetch(workers=2)
    assert r["stored"] == 1 and r["already_in_library"] == 1
    L.update()  # fingerprints the harvested passages
    hit = L.search("Skp1 C-terminal disordered F-box")[0]
    assert hit["citekey"] == "dantu2021skp1" and hit["page"] == 0


def test_doi_duplicates_and_archive_relink(lib, tmp_path):
    import shutil
    L, d = lib
    make_pdf(d / "copy_of_tem1.pdf", "Omega loop dynamics in TEM-1 beta-lactamase", PARA + " extra words. ", "10.1000/tem1")
    r = L.update()
    assert len(r.duplicates) == 1 and L.store.stats()["papers"] == 2
    print(L.archive())
    moved = tmp_path / "elsewhere"
    shutil.move(str(d), str(moved))   # PDFs gone from the indexed folder
    r = L.update()
    assert not r.removed and L.store.stats()["papers"] == 2   # archived text survives
    shutil.move(str(moved), str(d))   # PDFs reappear (e.g. on the Mac) -> re-linked, not re-read
    r = L.update()
    assert not r.added and len(r.moved) == 2


def test_harvest_ncbi_batches(lib, monkeypatch):
    from gautam import harvest, scholarly as sch
    L, _ = lib
    H = harvest.Harvester(L)
    art = JATS.split(b"?>", 1)[1].replace(b"<article-meta>", b'<article-meta><article-id pub-id-type="pmc">111</article-id>')
    xml = b"<pmc-articleset>" + art + b"</pmc-articleset>"
    assert list(harvest.split_pmc_articleset(xml)) == ["111"]
    H.db.execute("INSERT INTO candidates (pmcid, doi, title, year, topics) VALUES ('PMC111','10.1371/journal.pcbi.000001','x',2021,'Skp1')")
    H.db.execute("INSERT INTO candidates (pmcid, doi, title, year, topics) VALUES ('PMC222',NULL,'not served',2020,'Skp1')")

    class R:
        status_code = 200
        content = xml

    class C:
        def __init__(s, *a, **k): pass
        def __enter__(s): return s
        def __exit__(s, *a): pass
        def post(s, url, data): assert "111" in data["id"] and "222" in data["id"]; return R()
    monkeypatch.setattr(sch, "_client", lambda *a, **k: C())
    r = H.fetch_ncbi()
    assert r["stored"] == 1 and r["still_todo"] == 1
