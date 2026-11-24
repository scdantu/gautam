"""Talk to the scholarly registries: Crossref (DOIs), Unpaywall and Europe PMC (free full text)."""
from __future__ import annotations

import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote

import httpx

from . import __version__

import threading

_last_call = 0.0
_rate_lock = threading.Lock()  # shared by parallel lookups: still at most ~5 calls per second in total


def _client(email: str = "") -> httpx.Client:
    ua = f"ProjectGautam/{__version__} (personal research library"
    ua += f"; mailto:{email})" if email else ")"
    return httpx.Client(headers={"User-Agent": ua}, timeout=30, follow_redirects=True)


def _get(url: str, email: str = "", params: dict | None = None, retries: int = 3) -> httpx.Response | None:
    """Polite GET: at most ~5 calls per second, retries on rate limits and server errors."""
    global _last_call
    for attempt in range(retries):
        with _rate_lock:
            wait = 0.2 - (time.time() - _last_call)
            if wait > 0:
                time.sleep(wait)
            _last_call = time.time()
        try:
            with _client(email) as c:
                r = c.get(url, params=params)
        except httpx.HTTPError as e:
            print(f"[gautam] network error {url}: {e}", file=sys.stderr)
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** (attempt + 1))
            continue
        return r
    return None


def clean_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", d, flags=re.I)
    return d.strip().rstrip(".").lower() or None


def norm_title(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode()
    t = re.sub(r"<[^>]+>", " ", t)  # Crossref titles can carry <i> tags
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def title_similarity(a: str, b: str) -> float:
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# ---------------- Crossref ----------------
RETRACTION_TYPES = {"retraction", "withdrawal", "removal", "expression_of_concern", "partial_retraction"}


def crossref_work(doi: str, email: str = "") -> dict | None:
    """Metadata for a DOI, or None if Crossref does not know it. Raises ConnectionError if offline."""
    r = _get(f"https://api.crossref.org/works/{quote(doi, safe='/:;()')}", email)
    if r is None:
        raise ConnectionError("Crossref unreachable")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return parse_crossref(r.json()["message"])


def crossref_search(title: str, email: str = "", author: str = "", rows: int = 3) -> list[dict]:
    params = {"query.bibliographic": title, "rows": rows}
    if author:
        params["query.author"] = author
    r = _get("https://api.crossref.org/works", email, params=params)
    if r is None:
        raise ConnectionError("Crossref unreachable")
    if r.status_code != 200:
        return []
    return [parse_crossref(m) for m in r.json()["message"].get("items", [])]


def parse_crossref(m: dict) -> dict:
    def first(x):
        return (x or [""])[0] if isinstance(x, list) else (x or "")

    year = None
    for k in ("published-print", "published-online", "issued", "created"):
        dp = (m.get(k) or {}).get("date-parts")
        if dp and dp[0] and dp[0][0]:
            year = int(dp[0][0])
            break
    authors = [{"given": a.get("given", ""), "family": a.get("family") or a.get("name", "")}
               for a in m.get("author", [])]
    updates = m.get("updated-by", []) or []
    retraction = [u for u in updates if str(u.get("type", "")).lower().replace("-", "_") in RETRACTION_TYPES]
    title = re.sub(r"<[^>]+>", "", first(m.get("title"))).strip()
    return {
        "doi": (m.get("DOI") or "").lower(),
        "title": title,
        "authors": authors,
        "year": year,
        "journal": first(m.get("container-title")),
        "volume": m.get("volume"),
        "issue": m.get("issue"),
        "pages": m.get("page"),
        "publisher": m.get("publisher"),
        "type": m.get("type"),
        "retracted": int(bool(retraction) or title.upper().startswith(("RETRACTED", "WITHDRAWN"))),
        "retraction_notices": [{"type": u.get("type"), "doi": u.get("DOI")} for u in retraction],
        "meta_source": "crossref",
    }


# ---------------- arXiv (its DOIs live at DataCite, not Crossref) ----------------
def arxiv_meta(arxiv_id: str, email: str = "") -> dict | None:
    import xml.etree.ElementTree as ET

    r = _get("https://export.arxiv.org/api/query", email, params={"id_list": arxiv_id})
    if r is None or r.status_code != 200:
        return None
    ns = {"a": "http://www.w3.org/2005/Atom"}
    e = ET.fromstring(r.content).find("a:entry", ns)
    if e is None or e.find("a:title", ns) is None:
        return None
    authors = []
    for a in e.findall("a:author/a:name", ns):
        parts = (a.text or "").strip().split()
        authors.append({"given": " ".join(parts[:-1]), "family": parts[-1] if parts else ""})
    pub = (e.findtext("a:published", "", ns) or "")[:4]
    return {"doi": f"10.48550/arxiv.{arxiv_id}".lower(), "title": re.sub(r"\s+", " ", e.findtext("a:title", "", ns)).strip(),
            "authors": authors, "year": int(pub) if pub.isdigit() else None, "journal": "arXiv",
            "type": "posted-content", "publisher": "arXiv", "retracted": 0, "meta_source": "arxiv"}


# ---------------- free full text ----------------
def find_open_pdf(doi: str, email: str = "") -> list[tuple[str, str]]:
    """Candidate (source, url) pairs for a free, legal PDF of this DOI, best first."""
    out: list[tuple[str, str]] = []
    d = doi.lower()
    if d.startswith("10.48550/arxiv."):
        out.append(("arXiv", f"https://arxiv.org/pdf/{doi.split('arxiv.', 1)[1]}"))
    if email:
        r = _get(f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}", params={"email": email})
        if r is not None and r.status_code == 200:
            j = r.json()
            locs = [j.get("best_oa_location")] + (j.get("oa_locations") or [])
            for loc in locs:
                if loc and loc.get("url_for_pdf"):
                    out.append((f"Unpaywall ({loc.get('host_type', '')})", loc["url_for_pdf"]))
    r = _get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
             params={"query": f'DOI:"{doi}"', "format": "json", "resultType": "core"})
    if r is not None and r.status_code == 200:
        for res in r.json().get("resultList", {}).get("result", []):
            pmcid = res.get("pmcid")
            if pmcid and res.get("isOpenAccess") == "Y":
                out.append(("Europe PMC", f"https://europepmc.org/articles/{pmcid}?pdf=render"))
            for u in (res.get("fullTextUrlList") or {}).get("fullTextUrl", []):
                if u.get("documentStyle") == "pdf" and u.get("availabilityCode") in ("OA", "F"):
                    out.append(("Europe PMC link", u["url"]))
    seen, uniq = set(), []
    for s, u in out:
        if u not in seen:
            seen.add(u)
            uniq.append((s, u))
    return uniq


def download_pdf(url: str, dest, email: str = "") -> bool:
    """Download and keep only if it really is a PDF (publishers often return a login page instead)."""
    r = _get(url, email)
    if r is None or r.status_code != 200 or not r.content.startswith(b"%PDF"):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return True
