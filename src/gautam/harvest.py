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
from .pdftext import Block, make_chunks

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
        self.field = h.get("field", "TITLE_ABS")  # search titles+abstracts, not full text (far fewer false hits)
        self.year_from = h.get("year_from")
        self.year_to = h.get("year_to")
        self.rate = float(h.get("requests_per_second", 8))
        self._lock = threading.Lock()
        self._last = 0.0

    # ---------------------------------------------------------------- helpers
    def _query(self, q: str) -> str:
        s = f"{self.field}:({q}) AND OPEN_ACCESS:y AND HAS_FT:y"
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

    # ---------------------------------------------------------------- count
    def count(self) -> dict[str, int | str]:
        out = {}
        for name, q in self.topics.items():
            r = self._get(f"{EPMC}/search", {"query": self._query(q), "format": "json", "pageSize": 1,
                                             "resultType": "idlist"})
            if r is None or r.status_code != 200:
                out[name] = f"error {getattr(r, 'status_code', 'network')}"
            else:
                out[name] = int(r.json().get("hitCount", 0))
            log(f"  {name}: {out[name]}")
        return out

    # ---------------------------------------------------------------- collect
    def collect(self, max_per_topic: int | None = None) -> dict:
        added = 0
        for name, q in self.topics.items():
            cursor, n = "*", 0
            log(f"collecting: {name}")
            while True:
                r = self._get(f"{EPMC}/search", {"query": self._query(q), "format": "json", "pageSize": 1000,
                                                 "resultType": "lite", "cursorMark": cursor})
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

    # ---------------------------------------------------------------- fetch
    def fetch(self, workers: int = 8, limit: int | None = None) -> dict:
        rows = self.db.execute("SELECT * FROM candidates WHERE status='todo' ORDER BY year DESC"
                               + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
        prog = Progress("harvest fetch", len(rows), "papers")
        got = dup = fail = 0
        with ThreadPoolExecutor(max_workers=workers) as tp:  # downloads in threads; database writes here
            futs = {}
            for row in rows:
                if row["doi"] and self.lib.store.paper_by_doi(row["doi"]):
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

    def _store(self, row, xml: bytes) -> None:
        meta, blocks = parse_jats(xml)
        meta.setdefault("doi", row["doi"])
        meta["title"] = meta.get("title") or row["title"]
        meta["year"] = meta.get("year") or row["year"]
        meta["journal"] = meta.get("journal") or row["journal"]
        meta.update({"meta_source": "europepmc", "stage": "meta", "type": "journal-article"})
        chunks = make_chunks(blocks)
        if not chunks:
            raise ValueError("full text had no paragraphs")
        if meta.get("doi") and self.lib.store.paper_by_doi(meta["doi"]):
            raise ValueError("duplicate DOI")
        meta["citekey"] = make_citekey(meta, taken=self.lib.store.citekey_taken)
        text = "\n\n".join(b.text for b in blocks)
        self.lib.store.add_paper(f"epmc:{row['pmcid']}", hashlib.sha1(xml).hexdigest(), len(xml), 0.0, meta,
                                 chunks, None, text, 0)


# -------------------------------------------------------------------- JATS XML -> passages
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
