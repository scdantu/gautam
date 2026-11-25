"""The outer ring: harvest open-access full text from Europe PMC for a list of topics.

Three steps, each saved as it goes (so a stopped job carries on):
  count   -> how many open-access full-text papers each topic finds (no downloads)
  collect -> list every matching paper (ID, DOI, title) in the 'candidates' table, de-duplicated
  fetch   -> download each paper's full text (structured XML, no PDF), cut into passages, store
Fingerprinting is then the normal `gautam index --stage embed`.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import scholarly
from .bib import make_citekey
from .library import Library, Progress, log
from .pdftext import Block, Chunk, make_chunks

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"

DEFAULT_TOPICS = {
    "protein dynamics": '"protein dynamics" OR "protein conformational dynamics"',
    "MD of enzymes": '"molecular dynamics" AND (enzyme OR enzymes OR enzymatic)',
    "enzyme dynamics": '"enzyme dynamics" OR ("conformational dynamics" AND (enzyme OR catalysis))',
    "allostery": "allostery OR allosteric",
    "conformational ensembles": '"conformational ensemble" OR "conformational ensembles" OR "structural ensemble"',
    "beta-lactamase dynamics": '("beta-lactamase" OR "β-lactamase") AND (dynamics OR "molecular dynamics" OR loop)',
    "TIM barrel": '"TIM barrel" OR "TIM-barrel" OR "triosephosphate isomerase"',
    "coevolution": '(coevolution OR "co-evolution" OR "direct coupling analysis") AND protein',
    "Koopman / MSM": '"Markov state model" OR "Markov state models" OR Koopman',
    "coarse-graining": '("coarse-grained" OR "coarse grained" OR "coarse-graining") AND protein',
    "protein language models": '"protein language model" OR "protein language models"',
    "protein design": '"protein design" OR "de novo protein"',
    "intrinsically disordered proteins": '"intrinsically disordered" OR "disordered protein" OR "disordered region"',
    "Skp1": "Skp1 OR SKP1",
    "SCF complex": '"SCF complex" OR "SCF ubiquitin ligase" OR "Skp1-Cul1-F-box" OR "SCF E3"',
    # --- added: NMR, kinetics, stability, and neighbouring methods/ideas from Sarath's work
    "NMR protein dynamics": '(NMR OR "nuclear magnetic resonance") AND protein AND (dynamics OR relaxation OR '
                            '"relaxation dispersion" OR CPMG OR CEST OR "order parameter" OR "chemical exchange")',
    "hydrogen exchange": '("hydrogen exchange" OR "hydrogen-deuterium exchange" OR HDX) AND protein',
    "enzyme kinetics": '("enzyme kinetics" OR "steady-state kinetics" OR "pre-steady-state" OR "Michaelis-Menten" '
                       'OR kcat OR "single-molecule enzymology") AND (enzyme OR protein)',
    "protein stability": '("protein stability" OR thermostability OR "thermodynamic stability" OR "folding stability" '
                         'OR "stability change" OR ddG) AND (protein OR mutation OR mutant)',
    "conformational substates & loops": '"conformational substates" OR "conformational substate" OR '
                                        '("loop dynamics" AND protein) OR ("active-site loop" AND enzyme) OR "lid closure"',
    "conformational selection": '"conformational selection" OR ("induced fit" AND protein) OR "population shift"',
    "free energy landscape": '("free energy landscape" OR "energy landscape") AND protein',
    "enhanced sampling": '(metadynamics OR "replica exchange" OR "umbrella sampling" OR "accelerated molecular dynamics" '
                         'OR "enhanced sampling") AND protein',
    "essential dynamics & normal modes": '("essential dynamics" OR "principal component analysis" OR "normal mode analysis" '
                                         'OR "elastic network" OR "Gaussian network model") AND protein',
    "residue networks & communities": '("residue interaction network" OR "dynamical network" OR "community analysis" OR '
                                      '"dynamic network analysis" OR "correlated motions") AND protein',
    "protein hydration": '("protein hydration" OR "hydration shell" OR "water dynamics" OR "inhomogeneous fluid solvation" '
                         'OR GIST) AND protein',
    "epistasis & mutational scanning": '(epistasis OR "deep mutational scanning" OR "mutational scanning" OR '
                                       '"fitness landscape") AND protein',
    "ancestral reconstruction": '"ancestral sequence reconstruction" OR "ancestral protein" OR "resurrected ancestral"',
    "beta-lactamase evolution": '("beta-lactamase" OR "β-lactamase" OR TEM-1 OR CTX-M OR KPC OR "metallo-beta-lactamase") '
                                'AND (evolution OR mutation OR mechanism OR "omega loop" OR "Ω-loop" OR resistance)',
    "structure prediction ensembles": '(AlphaFold OR "structure prediction") AND (ensemble OR ensembles OR '
                                      '"conformational diversity" OR "alternative conformations" OR "multiple states")',
    "time-resolved & ensemble structure": '"time-resolved crystallography" OR "room-temperature crystallography" OR '
                                          '"multi-temperature crystallography" OR ("cryo-EM" AND (heterogeneity OR '
                                          '"conformational landscape")) OR ("single-molecule FRET" AND dynamics)',
    "cryptic pockets & binding kinetics": '"cryptic pocket" OR "cryptic pockets" OR "residence time" OR '
                                          '("binding kinetics" AND protein) OR ("ligand unbinding" AND simulation)',
    "force fields": '("force field" OR "force fields") AND (protein OR "molecular dynamics") AND '
                    '(accuracy OR parameterization OR benchmark OR "disordered")',
    "protein modularity": '("structural motifs" OR "protein modularity" OR "modular proteins" OR "building blocks" OR '
                          '"tertiary motifs") AND protein',
}

# Broad themes for open-access REVIEW articles: wider than the keyword topics, because a review's value is
# its overview and its reference list (which feeds the citation graph).
DEFAULT_REVIEW_TOPICS = {
    "review: protein dynamics": '"protein dynamics" OR "conformational dynamics" OR "protein motions"',
    "review: energy landscapes": '"energy landscape" OR "conformational ensemble" OR "conformational selection"',
    "review: allostery": "allostery OR allosteric",
    "review: enzyme catalysis": '(enzyme OR enzymes) AND (catalysis OR mechanism OR dynamics OR evolution)',
    "review: biomolecular simulation": '"molecular dynamics" OR "molecular simulation" OR "enhanced sampling" OR "coarse-grained"',
    "review: NMR of proteins": '(NMR OR "nuclear magnetic resonance") AND (protein OR proteins)',
    "review: protein evolution": '"protein evolution" OR coevolution OR "evolutionary dynamics" OR "ancestral"',
    "review: protein design & ML": '"protein design" OR "protein language model" OR ("deep learning" AND protein)',
    "review: disordered proteins": '"intrinsically disordered" OR "disordered protein" OR "phase separation"',
    "review: ubiquitin & SCF": '"ubiquitin ligase" OR "SCF" OR "cullin" OR Skp1 OR "F-box"',
    "review: folding & stability": '"protein folding" OR "protein stability" OR "misfolding"',
    "review: kinetic models": '"Markov state model" OR Koopman OR "transfer operator" OR "kinetic model"',
}

# Key people. Queries are raw Europe PMC syntax. Common names are pinned to an institution (AFF) or a subject,
# because "Baker D" or "Kern D" alone matches thousands of unrelated people.
_BIO = "(protein OR proteins OR enzyme OR enzymes OR NMR OR molecular OR folding OR dynamics)"
DEFAULT_AUTHORS = {
    "Andrei Lupas": 'AUTH:"Lupas AN"',
    "Helmut Grubmüller": '(AUTH:"Grubmüller H" OR AUTH:"Grubmuller H")',
    "Lewis Kay": f'AUTH:"Kay LE" AND (AFF:"Toronto" OR TITLE_ABS:{_BIO})',
    "Dorothee Kern": 'AUTH:"Kern D" AND (AFF:"Brandeis" OR AFF:"Howard Hughes")',
    "Hans Frauenfelder": 'AUTH:"Frauenfelder H"',
    "Rik Wierenga": 'AUTH:"Wierenga RK"',
    "Martin Karplus": f'AUTH:"Karplus M" AND TITLE_ABS:{_BIO}',
    "Arieh Warshel": 'AUTH:"Warshel A"',
    "Alessandro Pandini": 'AUTH:"Pandini A"',
    "Liskin Swint-Kruse": 'AUTH:"Swint-Kruse L"',
    "David Baker": 'AUTH:"Baker D" AND (AFF:"University of Washington" OR AFF:"Institute for Protein Design")',
    "Frances Arnold": 'AUTH:"Arnold FH"',
    "Debora Marks": 'AUTH:"Marks DS" AND (AFF:"Harvard" OR TITLE_ABS:(protein OR evolution OR sequence OR variant))',
    "Faruck Morcos": 'AUTH:"Morcos F"',
}

CAND_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    pmcid TEXT PRIMARY KEY,
    doi TEXT,
    title TEXT,
    year INTEGER,
    journal TEXT,
    topics TEXT,
    status TEXT DEFAULT 'todo'   -- todo | done | skipped_duplicate | failed
);
CREATE INDEX IF NOT EXISTS cand_status ON candidates(status);
CREATE INDEX IF NOT EXISTS cand_doi ON candidates(doi);
"""


class Harvester:
    def __init__(self, lib: Library):
        self.lib = lib
        self.db = lib.store.db
        self.db.executescript(CAND_SCHEMA)
        h = lib.cfg.raw.get("harvest", {})
        self.topics: dict[str, str] = h.get("topics") or DEFAULT_TOPICS
        self.review_topics: dict[str, str] = h.get("review_topics") or DEFAULT_REVIEW_TOPICS
        self.authors: dict[str, str] = h.get("authors") or DEFAULT_AUTHORS
        self.field = h.get("field", "TITLE_ABS")  # search titles+abstracts, not full text (far fewer false hits)
        self.year_from = h.get("year_from")
        self.year_to = h.get("year_to")
        self.rate = float(h.get("requests_per_second", 8))
        self._lock = threading.Lock()
        self._last = 0.0

    # ---------------------------------------------------------------- helpers
    def _query(self, q: str, scope: str = "open_access", reviews: bool = False, raw: bool = False) -> str:
        s = f"({q})" if raw else f"{self.field}:({q})"
        if reviews:
            s += ' AND PUB_TYPE:"review"'
        if scope == "open_access":
            s += " AND OPEN_ACCESS:y AND HAS_FT:y"
        else:  # every record with this topic: journal articles (MEDLINE, PMC) and preprints
            s += " AND (SRC:MED OR SRC:PMC OR SRC:PPR)"
        if self.year_from or self.year_to:
            s += f" AND PUB_YEAR:[{self.year_from or 1900} TO {self.year_to or 2100}]"
        return s

    def _get(self, url: str, params: dict | None = None, timeout: float = 120, tries: int = 6):
        """Europe PMC can take a minute on big searches: wait up to `timeout`s, retry with growing pauses."""
        with self._lock:  # shared speed limit across threads
            wait = 1 / self.rate - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
        for attempt in range(tries):
            try:
                with scholarly._client(self.lib.cfg.email, timeout=timeout) as c:
                    r = c.get(url, params=params)
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** (attempt + 1))
                    continue
                return r
            except Exception as e:
                log(f"network error ({e}); retry {attempt + 1}/{tries}")
                time.sleep(min(60, 5 * 2 ** attempt))
        return None

    def _sets(self, which: str) -> list[tuple[str, str, bool, bool]]:
        """(name, query, reviews?, raw?) for 'topics', 'reviews', 'authors' or 'all' (topics + authors)."""
        out = []
        if which in ("topics", "all"):
            out += [(n, q, False, False) for n, q in self.topics.items()]
        if which in ("authors", "all"):
            out += [(f"author: {n}", q, False, True) for n, q in self.authors.items()]
        if which == "reviews":
            out += [(n, q, True, False) for n, q in self.review_topics.items()]
        return out

    # ---------------------------------------------------------------- count
    def count(self, scope: str = "open_access", which: str = "all") -> dict[str, int | str]:
        out = {}
        for name, q, rev, raw in self._sets(which):
            r = self._get(f"{EPMC}/search", {"query": self._query(q, scope, rev, raw), "format": "json", "pageSize": 1,
                                             "resultType": "idlist"})
            if r is None or r.status_code != 200:
                out[name] = f"error {getattr(r, 'status_code', 'network')}"
            else:
                out[name] = int(r.json().get("hitCount", 0))
            log(f"  {name}: {out[name]}")
        return out

    # ---------------------------------------------------------------- collect
    def collect(self, max_per_topic: int | None = None, which: str = "all") -> dict:
        """List open-access full-text papers for topics + authors (which='all'), or open-access reviews."""
        added = 0
        for name, q, rev, raw in self._sets(which):
            cursor, n = "*", 0
            log(f"collecting: {name}")
            while True:
                r = self._get(f"{EPMC}/search", {"query": self._query(q, "open_access", rev, raw), "format": "json",
                                                 "pageSize": 1000, "resultType": "lite", "cursorMark": cursor})
                if r is None or r.status_code != 200:
                    log(f"  stopped early for {name} (error); run collect again to resume")
                    break
                j = r.json()
                res = j.get("resultList", {}).get("result", [])
                for x in res:
                    pmcid = x.get("pmcid")
                    if not pmcid:
                        continue
                    cur = self.db.execute("SELECT topics FROM candidates WHERE pmcid=?", (pmcid,)).fetchone()
                    if cur:
                        if name not in (cur[0] or "").split("|"):
                            self.db.execute("UPDATE candidates SET topics=? WHERE pmcid=?",
                                            (f"{cur[0]}|{name}", pmcid))
                        continue
                    self.db.execute("INSERT INTO candidates (pmcid, doi, title, year, journal, topics) "
                                    "VALUES (?,?,?,?,?,?)",
                                    (pmcid, (x.get("doi") or "").lower() or None, x.get("title"),
                                     int(x["pubYear"]) if str(x.get("pubYear", "")).isdigit() else None,
                                     x.get("journalTitle"), name))
                    added += 1
                n += len(res)
                self.db.commit()
                nxt = j.get("nextCursorMark")
                if not res or not nxt or nxt == cursor or (max_per_topic and n >= max_per_topic):
                    break
                cursor = nxt
            log(f"  {name}: {n} listed")
        total = self.db.execute("SELECT count(*) FROM candidates").fetchone()[0]
        todo = self.db.execute("SELECT count(*) FROM candidates WHERE status='todo'").fetchone()[0]
        return {"new_candidates": added, "total_unique": total, "to_fetch": todo}

    # ---------------------------------------------------------------- fetch (NCBI PMC, in batches)
    NCBI = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

    def fetch_ncbi(self, batch: int = 100, workers: int = 3, limit: int | None = None) -> dict:
        """Full text from PubMed Central via NCBI E-utilities: ~100 papers per request instead of one.
        NCBI allows 3 requests/s without a key, 10/s with [harvest] ncbi_api_key. Papers NCBI does not
        return stay 'todo' for the Europe PMC route (`gautam harvest fetch --source epmc`)."""
        h = self.lib.cfg.raw.get("harvest", {})
        key = h.get("ncbi_api_key", "")
        rate = 9.0 if key else 2.5
        rows = self.db.execute("SELECT * FROM candidates WHERE status IN ('todo','failed') ORDER BY year DESC"
                               + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
        todo, dup = [], 0
        for row in rows:
            if row["doi"] and self.lib.store.has_fulltext(row["doi"]):
                self._status(row["pmcid"], "skipped_duplicate")
                dup += 1
            else:
                todo.append(row)
        batches = [todo[i:i + batch] for i in range(0, len(todo), batch)]
        log(f"harvest (NCBI): {len(todo)} papers in {len(batches)} requests of {batch}; {dup} already in library")
        prog = Progress("harvest fetch (NCBI)", len(todo), "papers")
        lock, last = threading.Lock(), [0.0]

        def get(ids: list[str]):
            data = {"db": "pmc", "id": ",".join(i.upper().removeprefix("PMC") for i in ids), "retmode": "xml",
                    "tool": "gautam", "email": self.lib.cfg.email or ""}
            if key:
                data["api_key"] = key
            for attempt in range(6):
                with lock:  # NCBI's per-second limit, shared by all threads
                    w = 1 / rate - (time.time() - last[0])
                    if w > 0:
                        time.sleep(w)
                    last[0] = time.time()
                try:
                    with scholarly._client(self.lib.cfg.email, timeout=300) as c:
                        r = c.post(self.NCBI, data=data)
                    if r.status_code == 200 and b"<" in r.content[:200]:
                        return split_pmc_articleset(r.content)
                    log(f"NCBI said {r.status_code}; retry {attempt + 1}/6")
                except Exception as e:
                    log(f"NCBI network error ({e}); retry {attempt + 1}/6")
                time.sleep(min(60, 5 * 2 ** attempt))
            return None

        got = fail = 0
        with ThreadPoolExecutor(max_workers=workers) as tp:  # downloads in threads; database writes here
            futs = {tp.submit(get, [r["pmcid"] for r in b]): b for b in batches}
            for f in as_completed(futs):
                b, arts = futs[f], f.result()
                if arts is None:
                    prog.step(len(b), item="request failed; these stay queued")
                    continue
                for row in b:
                    xml = arts.get(row["pmcid"].upper().removeprefix("PMC"))
                    if xml is None:
                        continue  # not served by NCBI: stays 'todo' for the Europe PMC route
                    try:
                        self._store(row, xml)
                        self._status(row["pmcid"], "done")
                        got += 1
                    except Exception as e:
                        self._status(row["pmcid"], "failed")
                        fail += 1
                        if "duplicate" not in str(e):
                            log(f"failed {row['pmcid']}: {e}")
                prog.step(len(b), item=b[-1]["title"] or "")
        left = self.db.execute("SELECT count(*) FROM candidates WHERE status='todo'").fetchone()[0]
        return {"stored": got, "already_in_library": dup, "failed": fail, "still_todo": left,
                "next": "run `gautam index --stage embed` to fingerprint them"
                        + (f"; {left} not served by NCBI can be tried with --source epmc" if left else "")}

    # ---------------------------------------------------------------- download only / ingest (split for HPC)
    def todo_ids(self) -> list[str]:
        """PMC IDs still to get (not done, not already in the library as full text)."""
        out = []
        for row in self.db.execute("SELECT pmcid, doi FROM candidates WHERE status IN ('todo','failed')"):
            if not (row["doi"] and self.lib.store.has_fulltext(row["doi"])):
                out.append(row["pmcid"])
        return out

    def download(self, dest: str, ids: list[str] | None = None, source: str = "both",
                 batch: int = 100, workers: int = 3) -> dict:
        """Network only: save raw full-text XML into `dest` (gzipped). No parsing, no database writes, so it is
        light enough for a login node or a laptop. Re-running skips what is already in `dest`.
        Then `gautam harvest ingest --from dest` (on a compute node) reads the files into the library."""
        import gzip
        from pathlib import Path as P

        d = P(dest).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        got_f, miss_f = d / "downloaded.txt", d / "not_served_by_ncbi.txt"
        have = set(got_f.read_text().split()) if got_f.exists() else set()
        missed = set(miss_f.read_text().split()) if miss_f.exists() else set()
        ids = [i.strip().upper() for i in (ids if ids is not None else self.todo_ids()) if i.strip()]
        want = [i for i in ids if i not in have]
        lock = threading.Lock()
        n_ncbi = n_epmc = 0
        if source in ("both", "ncbi"):
            todo = [i for i in want if i not in missed]
            batches = [todo[k:k + batch] for k in range(0, len(todo), batch)]
            log(f"download (NCBI): {len(todo)} papers in {len(batches)} requests -> {d}")
            prog = Progress("download (NCBI)", len(todo), "papers")
            key = self.lib.cfg.raw.get("harvest", {}).get("ncbi_api_key", "")
            rate, last = (9.0 if key else 2.5), [0.0]

            def one(k_ids):
                k, b = k_ids
                data = {"db": "pmc", "id": ",".join(i.removeprefix("PMC") for i in b), "retmode": "xml",
                        "tool": "gautam", "email": self.lib.cfg.email or ""}
                if key:
                    data["api_key"] = key
                for attempt in range(6):
                    with lock:
                        w = 1 / rate - (time.time() - last[0])
                        if w > 0:
                            time.sleep(w)
                        last[0] = time.time()
                    try:
                        with scholarly._client(self.lib.cfg.email, timeout=300) as c:
                            r = c.post(self.NCBI, data=data)
                        if r.status_code == 200 and b"<" in r.content[:200]:
                            return k, b, r.content
                        log(f"NCBI said {r.status_code}; retry {attempt + 1}/6")
                    except Exception as e:
                        log(f"NCBI network error ({e}); retry {attempt + 1}/6")
                    time.sleep(min(60, 5 * 2 ** attempt))
                return k, b, None

            stamp = time.strftime("%Y%m%d%H%M%S")
            with ThreadPoolExecutor(max_workers=workers) as tp:
                for f in as_completed([tp.submit(one, kb) for kb in enumerate(batches)]):
                    k, b, content = f.result()
                    if content is None:
                        prog.step(len(b), item="request failed; re-run download to retry")
                        continue
                    served = set(split_pmc_articleset(content))
                    (d / f"ncbi-{stamp}-{k:05d}.xml.gz").write_bytes(gzip.compress(content))
                    ok = [i for i in b if i.removeprefix("PMC") in served]
                    no = [i for i in b if i.removeprefix("PMC") not in served]
                    with got_f.open("a") as fh:
                        fh.write("".join(i + "\n" for i in ok))
                    if no:
                        with miss_f.open("a") as fh:
                            fh.write("".join(i + "\n" for i in no))
                        missed.update(no)
                    n_ncbi += len(ok)
                    prog.step(len(b), item=f"{len(ok)}/{len(b)} served")
        if source in ("both", "epmc"):
            rest = [i for i in want if (i in missed or source == "epmc")]
            rest = [i for i in rest if not (d / f"epmc-{i}.xml.gz").exists()]
            if rest:
                log(f"download (Europe PMC, one by one): {len(rest)} papers NCBI did not serve")
                prog = Progress("download (Europe PMC)", len(rest), "papers")
                with ThreadPoolExecutor(max_workers=8) as tp:
                    futs = {tp.submit(self._download, i): i for i in rest}
                    for f in as_completed(futs):
                        i, x = futs[f], f.result()
                        if x is not None:
                            (d / f"epmc-{i}.xml.gz").write_bytes(gzip.compress(x))
                            with got_f.open("a") as fh:
                                fh.write(i + "\n")
                            n_epmc += 1
                        prog.step(item=i)
        return {"downloaded_ncbi": n_ncbi, "downloaded_epmc": n_epmc, "folder": str(d),
                "next": f"on a compute node: gautam harvest ingest --from {d}"}

    def ingest(self, src: str) -> dict:
        """Read downloaded XML files into the library (CPU work: do this inside a job)."""
        import gzip
        from pathlib import Path as P

        files = sorted(P(src).expanduser().glob("*.xml.gz"))
        cand = {r["pmcid"].upper(): r for r in self.db.execute("SELECT * FROM candidates")}
        prog = Progress("ingest", len(files), "files")
        got = skip = fail = 0
        for f in files:
            try:
                arts = split_pmc_articleset(gzip.decompress(f.read_bytes()))
            except Exception as e:
                log(f"unreadable {f.name}: {e}")
                prog.step(item=f.name)
                continue
            for num, xml in arts.items():
                row = cand.get("PMC" + num)
                if row is None or row["status"] == "done":
                    skip += 1
                    continue
                try:
                    self._store(row, xml, commit=False)
                    self.db.execute("UPDATE candidates SET status='done' WHERE pmcid=?", (row["pmcid"],))
                    got += 1
                except Exception as e:
                    self.db.execute("UPDATE candidates SET status=? WHERE pmcid=?",
                                    ("skipped_duplicate" if "duplicate" in str(e) else "failed", row["pmcid"]))
                    fail += 1
            self.db.commit()  # one save per file of ~100 papers (much faster on shared file systems)
            prog.step(item=f.name)
        left = len(self.todo_ids())
        return {"stored": got, "skipped_already_done": skip, "failed_or_duplicate": fail, "still_todo": left}

    # ---------------------------------------------------------------- abstracts for everything (all years)
    def abstracts(self, max_per_topic: int | None = None, which: str = "all") -> dict:
        """Title + abstract for EVERY record on your topics (not only open access), back to MEDLINE's start.
        Stored as 'abstract only' papers: searchable, citable, exportable; replaced by full text if that arrives."""
        added = seen = 0
        t0 = time.time()
        for name, q, rev, raw in self._sets(which):
            cursor, n = "*", 0
            log(f"abstracts: {name}")
            while True:
                r = self._get(f"{EPMC}/search", {"query": self._query(q, "all", rev, raw), "format": "json",
                                                 "pageSize": 1000, "resultType": "core", "cursorMark": cursor})
                if r is None or r.status_code != 200:
                    log(f"  stopped early for {name} (error); run again to resume (done ones are skipped)")
                    break
                j = r.json()
                res = j.get("resultList", {}).get("result", [])
                for x in res:
                    seen += 1
                    added += self._store_abstract(x, name)
                self.db.commit()  # one save per page of 1,000
                n += len(res)
                log(f"  {name}: {n} records looked at, {added} new abstracts so far ({time.time() - t0:.0f}s)")
                nxt = j.get("nextCursorMark")
                if not res or not nxt or nxt == cursor or (max_per_topic and n >= max_per_topic):
                    break
                cursor = nxt
        return {"records_seen": seen, "abstracts_added": added,
                "next": "run `gautam index --stage embed` to fingerprint them"}

    def _store_abstract(self, x: dict, topic: str) -> int:
        doi = (x.get("doi") or "").lower() or None
        pmid = x.get("pmid") or x.get("id")
        key = f"abstract:{pmid or doi}"
        if (doi and self.lib.store.paper_by_doi(doi)) or self.lib.store.paper_by_path(key):
            return 0  # already have it (full text or abstract)
        title = re.sub(r"<[^>]+>", "", x.get("title") or "").strip().rstrip(".")
        abstract = re.sub(r"<[^>]+>", " ", x.get("abstractText") or "")
        abstract = re.sub(r"\s+", " ", abstract).strip()
        if not title:
            return 0
        authors = [{"family": a.get("lastName") or a.get("collectiveName") or a.get("fullName", ""),
                    "given": a.get("firstName") or a.get("initials") or ""}
                   for a in (x.get("authorList") or {}).get("author", [])]
        ji = x.get("journalInfo") or {}
        year = x.get("pubYear")
        meta = {"doi": doi, "title": title, "authors": authors,
                "year": int(year) if str(year).isdigit() else None,
                "journal": (ji.get("journal") or {}).get("title") or x.get("journalTitle"),
                "volume": ji.get("volume"), "issue": ji.get("issue"), "pages": x.get("pageInfo"),
                "type": "abstract", "meta_source": "europepmc", "stage": "meta"}
        meta["citekey"] = make_citekey(meta, taken=self.lib.store.citekey_taken)
        text = f"{title}. {abstract}" if abstract else title
        chunk = Chunk(0, 0, "Abstract" if abstract else "Title only (no abstract in the record)", text)
        self.lib.store.add_paper(key, "", len(text), 0.0, meta, [chunk], None, text, 0, commit=False)
        return 1

    # ---------------------------------------------------------------- fetch (Europe PMC, one by one)
    def fetch(self, workers: int = 8, limit: int | None = None) -> dict:
        rows = self.db.execute("SELECT * FROM candidates WHERE status='todo' ORDER BY year DESC"
                               + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
        prog = Progress("harvest fetch", len(rows), "papers")
        got = dup = fail = 0
        with ThreadPoolExecutor(max_workers=workers) as tp:  # downloads in threads; database writes here
            futs = {}
            for row in rows:
                if row["doi"] and self.lib.store.has_fulltext(row["doi"]):
                    self._status(row["pmcid"], "skipped_duplicate")
                    dup += 1
                    prog.step(item=f"already have: {row['title'] or ''}")
                    continue
                futs[tp.submit(self._download, row["pmcid"])] = row
            for f in as_completed(futs):
                row = futs[f]
                xml = f.result()
                try:
                    if xml is None:
                        raise ValueError("no full text")
                    self._store(row, xml)
                    self._status(row["pmcid"], "done")
                    got += 1
                except Exception as e:
                    self._status(row["pmcid"], "failed")
                    fail += 1
                    log(f"failed {row['pmcid']}: {e}")
                prog.step(item=row["title"] or "")
        return {"stored": got, "already_in_library": dup, "failed": fail,
                "next": "run `gautam index --stage embed` to fingerprint them"}

    def _status(self, pmcid: str, s: str) -> None:
        self.db.execute("UPDATE candidates SET status=? WHERE pmcid=?", (s, pmcid))
        self.db.commit()

    def _download(self, pmcid: str) -> bytes | None:
        r = self._get(f"{EPMC}/{pmcid}/fullTextXML")
        return r.content if (r is not None and r.status_code == 200 and r.content.lstrip().startswith(b"<")) else None

    def _store(self, row, xml: bytes, commit: bool = True) -> None:
        meta, blocks = parse_jats(xml)
        meta.setdefault("doi", row["doi"])
        meta["title"] = meta.get("title") or row["title"]
        meta["year"] = meta.get("year") or row["year"]
        meta["journal"] = meta.get("journal") or row["journal"]
        meta.update({"meta_source": "europepmc", "stage": "meta", "type": "journal-article"})
        chunks = make_chunks(blocks)
        if not chunks:
            raise ValueError("full text had no paragraphs")
        old = self.lib.store.paper_by_doi(meta["doi"]) if meta.get("doi") else None
        if old is not None:
            if (old["type"] or "") != "abstract":
                raise ValueError("duplicate DOI")
            self.lib.store.delete_paper(old["id"])  # full text replaces an abstract-only entry
        meta["citekey"] = make_citekey(meta, taken=self.lib.store.citekey_taken)
        text = "\n\n".join(b.text for b in blocks)
        self.lib.store.add_paper(f"epmc:{row['pmcid']}", hashlib.sha1(xml).hexdigest(), len(xml), 0.0, meta,
                                 chunks, None, text, 0, commit=commit)


# -------------------------------------------------------------------- JATS XML -> passages
def split_pmc_articleset(xml: bytes) -> dict[str, bytes]:
    """NCBI returns <pmc-articleset><article>...</article>...</pmc-articleset>; key each article by its PMC number."""
    root = ET.fromstring(xml)
    out = {}
    for art in root.iter():
        if not (isinstance(art.tag, str) and art.tag.split("}")[-1] == "article"):
            continue
        pmc = None
        for aid in art.iter():
            if isinstance(aid.tag, str) and aid.tag.split("}")[-1] == "article-id" and \
                    aid.get("pub-id-type") in ("pmc", "pmcid", "pmcaid"):
                t = (aid.text or "").strip().upper().removeprefix("PMC")
                if t.isdigit():
                    pmc = t
                    break
        if pmc:
            out[pmc] = ET.tostring(art)
    return out



def _txt(el) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def parse_jats(xml: bytes) -> tuple[dict, list[Block]]:
    """Paper details and paragraphs from Europe PMC / PMC full-text XML (the JATS format)."""
    root = ET.fromstring(xml)
    for el in root.iter():  # drop namespaces for simpler lookups
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    am = root.find(".//front/article-meta")
    meta: dict = {}
    if am is not None:
        meta["title"] = _txt(am.find("title-group/article-title"))
        for aid in am.findall("article-id"):
            if aid.get("pub-id-type") == "doi":
                meta["doi"] = _txt(aid).lower()
        authors = []
        for c in am.findall(".//contrib-group/contrib"):
            if c.get("contrib-type", "author") != "author":
                continue
            n = c.find("name")
            if n is not None:
                authors.append({"family": _txt(n.find("surname")), "given": _txt(n.find("given-names"))})
            elif c.find("collab") is not None:
                authors.append({"family": _txt(c.find("collab")), "given": ""})
        meta["authors"] = authors
        for pd in am.findall("pub-date"):
            y = _txt(pd.find("year"))
            if y.isdigit():
                meta["year"] = int(y)
                break
        meta["volume"] = _txt(am.find("volume")) or None
        meta["issue"] = _txt(am.find("issue")) or None
        fp, lp = _txt(am.find("fpage")), _txt(am.find("lpage"))
        meta["pages"] = f"{fp}-{lp}" if fp and lp else (fp or _txt(am.find("elocation-id")) or None)
    jm = root.find(".//front/journal-meta")
    if jm is not None:
        meta["journal"] = _txt(jm.find(".//journal-title"))
        meta["publisher"] = _txt(jm.find(".//publisher-name")) or None

    blocks: list[Block] = []
    if am is not None:
        for ab in am.findall("abstract"):
            blocks.append(Block(0, "Abstract"))
            for p in ab.iter("p"):
                t = _txt(p)
                if t:
                    blocks.append(Block(0, t))
    body = root.find(".//body")
    if body is not None:
        _walk(body, blocks)
    return meta, blocks


def _walk(el, blocks: list[Block]) -> None:
    for ch in el:
        tag = ch.tag
        if tag == "sec":
            title = _txt(ch.find("title"))
            if title:
                blocks.append(Block(0, title))
            _walk(ch, blocks)
        elif tag == "p":
            t = _txt(ch)
            if t:
                blocks.append(Block(0, t))
        elif tag in ("fig", "table-wrap"):
            cap = _txt(ch.find("caption"))
            if cap:
                blocks.append(Block(0, ("Figure: " if tag == "fig" else "Table: ") + cap))
        elif tag in ("list", "boxed-text", "disp-quote"):
            _walk(ch, blocks)
        elif tag == "list-item":
            _walk(ch, blocks)
