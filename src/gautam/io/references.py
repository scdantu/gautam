"""Read reference lists: from full-text XML files, Europe PMC and Crossref.

Network clients take a ``get(url, params)`` callable (with the caller's
rate limit and retries) so this module never decides how to talk to a server.
"""

import gzip
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from multiprocessing import get_context
from pathlib import Path

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
ELINK = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
_DOI_IN_URL = re.compile(r"10\.\d{4,9}/\S+", re.I)


@dataclass(frozen=True)
class Reference:
    """One cited work, identified by DOI and/or PubMed ID."""

    doi: str | None = None
    pmid: str | None = None

    @property
    def key(self) -> str | None:
        """Stable identifier: ``doi:...`` if known, else ``pmid:...``."""
        if self.doi:
            return f"doi:{self.doi}"
        return f"pmid:{self.pmid}" if self.pmid else None


def make_reference(doi=None, pmid=None) -> Reference | None:
    """Clean a DOI/PMID pair into a Reference (None if neither is usable)."""
    d = (doi or "").strip().lower()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", d).rstrip(".")
    p = str(pmid or "").strip()
    ref = Reference(d if d.startswith("10.") else None,
                    p if p.isdigit() else None)
    return ref if ref.key else None


def _tag(el) -> str:
    return el.tag.split("}")[-1] if isinstance(el.tag, str) else ""


class JatsReferenceReader:
    """References from JATS full-text XML, keyed by PMC number."""

    def read_bytes(self, xml: bytes) -> dict[str, list[Reference]]:
        """Parse one XML document (an article or an article set)."""
        root = ET.fromstring(xml)
        out: dict[str, list[Reference]] = {}
        for art in root.iter():
            if _tag(art) != "article":
                continue
            pmc = self._pmc_number(art)
            if pmc:
                out[pmc] = self._references(art)
        return out

    def read_folder(self, folder: Path, workers: int = 1):
        """Yield ``(pmc_number, references)`` for every ``*.xml.gz`` file.

        With ``workers > 1`` files are parsed in parallel processes.
        """
        files = sorted(Path(folder).expanduser().glob("*.xml.gz"))
        if workers <= 1 or len(files) < 4:
            for f in files:
                yield from _read_file(f).items()
            return
        ctx = get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pp:
            for out in pp.map(_read_file, files, chunksize=4):
                yield from out.items()

    @staticmethod
    def _pmc_number(art) -> str | None:
        for el in art.iter():
            if _tag(el) == "article-id" and el.get("pub-id-type") in (
                    "pmc", "pmcid", "pmcaid"):
                t = (el.text or "").strip().upper().removeprefix("PMC")
                if t.isdigit():
                    return t
        return None

    @staticmethod
    def _references(art) -> list[Reference]:
        refs = []
        for ref in art.iter():
            if _tag(ref) != "ref":
                continue
            doi = pmid = None
            for el in ref.iter():
                t = _tag(el)
                kind = el.get("pub-id-type")
                if t == "pub-id" and kind == "doi":
                    doi = (el.text or "").strip()
                elif t == "pub-id" and kind == "pmid":
                    pmid = (el.text or "").strip()
                elif t == "ext-link" and not doi:
                    href = " ".join(v for k, v in el.attrib.items()
                                    if k.endswith("href"))
                    m = _DOI_IN_URL.search(href or el.text or "")
                    doi = m.group(0) if m else None
            r = make_reference(doi, pmid)
            if r:
                refs.append(r)
        return refs


def _read_file(path: Path) -> dict[str, list[Reference]]:
    """Parse one gzipped XML file (top level, so worker processes can)."""
    try:
        data = gzip.decompress(Path(path).read_bytes())
        return JatsReferenceReader().read_bytes(data)
    except (OSError, ET.ParseError):
        return {}


class NcbiReferenceClient:
    """Reference lists for many PubMed papers per request (NCBI elink).

    NCBI holds reference lists for papers whose full text is in PMC; papers
    it returns nothing for should fall back to Europe PMC.
    """

    def __init__(self, post, batch: int = 100):
        """``post(url, data)`` returns a response or None."""
        self._post = post
        self.batch = batch

    def references(self, pmids: list[str]) -> dict[str, list[Reference]]:
        """Return ``{citing_pmid: [Reference, ...]}`` for PMIDs NCBI knows."""
        out: dict[str, list[Reference]] = {}
        for i in range(0, len(pmids), self.batch):
            part = pmids[i:i + self.batch]
            data = {"dbfrom": "pubmed", "db": "pubmed", "retmode": "json",
                    "linkname": "pubmed_pubmed_refs", "id": part}
            r = self._post(ELINK, data)
            if r is None or r.status_code != 200:
                continue
            for ls in r.json().get("linksets", []):
                ids = [str(x) for x in ls.get("ids", [])]
                links = []
                for db in ls.get("linksetdbs", []):
                    if db.get("linkname") == "pubmed_pubmed_refs":
                        links = [str(x) for x in db.get("links", [])]
                if ids and links:
                    out[ids[0]] = [Reference(None, p) for p in links]
        return out


class EpmcReferenceClient:
    """Reference lists from Europe PMC (works for abstracts and full text)."""

    def __init__(self, get):
        """``get(url, params)`` returns a response or None."""
        self._get = get

    def references(self, source: str, ext_id: str) -> list[Reference] | None:
        """Return references of one record (``MED`` + PMID, or ``PMC``).

        Returns None when the server could not be reached.
        """
        out, page = [], 1
        while True:
            r = self._get(f"{EPMC}/{source}/{ext_id}/references",
                          {"format": "json", "pageSize": 1000, "page": page})
            if r is None:
                return None
            if r.status_code != 200:
                return out
            j = r.json()
            items = (j.get("referenceList") or {}).get("reference", [])
            for x in items:
                pm = x.get("id") if x.get("source") == "MED" else None
                ref = make_reference(x.get("doi"), pm)
                if ref:
                    out.append(ref)
            if len(items) < 1000:
                return out
            page += 1


class CrossrefReferenceClient:
    """Reference lists that publishers deposit with Crossref (by DOI)."""

    def __init__(self, get):
        """``get(url, params)`` returns a response or None."""
        self._get = get

    def references(self, doi: str) -> list[Reference] | None:
        """Return references with DOIs for one paper; None if unreachable."""
        r = self._get(f"https://api.crossref.org/works/{doi}", None)
        if r is None:
            return None
        if r.status_code != 200:
            return []
        items = r.json().get("message", {}).get("reference", []) or []
        return [x for x in (make_reference(i.get("DOI")) for i in items) if x]


class EpmcRecordClient:
    """Look up many papers at once (title, abstract, IDs, open access)."""

    def __init__(self, get, batch: int = 50, workers: int = 8):
        """``get(url, params)`` returns a response or None (thread-safe)."""
        self._get = get
        self.batch = batch
        self.workers = workers

    def lookup(self, refs: list[Reference]) -> dict[str, dict]:
        """Return Europe PMC 'core' records keyed by Reference.key.

        Batches of ``batch`` papers, ``workers`` requests in flight at once
        (the caller's ``get`` keeps the overall rate polite).
        """
        dois = [r for r in refs if r.doi]
        pmids = [r for r in refs if not r.doi and r.pmid]
        jobs = []
        for i in range(0, len(dois), self.batch):
            q = " OR ".join(f'DOI:"{r.doi}"'
                            for r in dois[i:i + self.batch])
            jobs.append(("doi", q))
        for i in range(0, len(pmids), self.batch):
            q = "(" + " OR ".join(f"EXT_ID:{r.pmid}"
                                  for r in pmids[i:i + self.batch])
            jobs.append(("pmid", q + ") AND SRC:MED"))
        out: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as tp:
            results = tp.map(lambda j: (j[0], self._search(j[1])), jobs)
            for kind, recs in results:
                for rec in recs:
                    if kind == "doi" and rec.get("doi"):
                        out[f"doi:{rec['doi'].lower()}"] = rec
                    elif kind == "pmid" and rec.get("pmid"):
                        out[f"pmid:{rec['pmid']}"] = rec
        return out

    def _search(self, query: str) -> list[dict]:
        r = self._get(f"{EPMC}/search",
                      {"query": query, "format": "json", "pageSize": 1000,
                       "resultType": "core"})
        if r is None or r.status_code != 200:
            return []
        return r.json().get("resultList", {}).get("result", [])
