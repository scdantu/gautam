"""Higher-level jobs: check a .bib, fetch what is missing, export BibTeX, store one-page summaries."""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import quote, quote_plus

from . import scholarly
from .bib import bib_first_author_family, bib_title, make_citekey, parse_bib, to_bibtex
from .library import Library, log


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M")


# ------------------------------------------------------------------ DOI verifier
def verify_bib(lib: Library, bib_path: str, write_fixed: bool = True) -> dict:
    """Check every entry against Crossref. Each problem comes with a suggested fix."""
    path = Path(bib_path).expanduser()
    entries = parse_bib(path.read_text(errors="replace"))
    email = lib.cfg.email
    results = []
    for e in entries:
        res = {"key": e["ID"], "status": "ok", "problems": [], "fixes": {}}
        title, doi = bib_title(e), scholarly.clean_doi(e.get("doi"))
        try:
            cr = scholarly.crossref_work(doi, email) if doi else None
            if doi and cr is None:
                res["problems"].append(f"DOI {doi} does not exist in Crossref (typo, or a fabricated reference).")
            if cr is None and title:
                hits = scholarly.crossref_search(title, email, author=bib_first_author_family(e), rows=3)
                best = max(hits, key=lambda h: scholarly.title_similarity(h["title"], title), default=None)
                if best and scholarly.title_similarity(best["title"], title) >= 0.9:
                    res["fixes"]["doi"] = best["doi"]
                    res["problems"].append(f"{'Wrong' if doi else 'Missing'} DOI. Suggested: {best['doi']}")
                    cr = best
                elif doi or not title:
                    pass
                else:
                    res["problems"].append("No DOI, and no confident match in Crossref. Check this reference exists.")
            if cr:
                sim = scholarly.title_similarity(cr["title"], title) if title else 1.0
                if title and sim < 0.85:
                    res["problems"].append(f"Title does not match the DOI's record (similarity {sim:.2f}). "
                                           f"Crossref says: \"{cr['title']}\"")
                    res["fixes"]["title"] = cr["title"]
                y = e.get("year", "").strip()
                if cr.get("year") and y.isdigit() and abs(int(y) - cr["year"]) > 1:
                    res["problems"].append(f"Year {y} but Crossref says {cr['year']}.")
                    res["fixes"]["year"] = str(cr["year"])
                fam = bib_first_author_family(e)
                cr_fam = (cr["authors"][0]["family"] if cr.get("authors") else "") or ""
                if fam and cr_fam and scholarly.norm_title(fam) != scholarly.norm_title(cr_fam):
                    res["problems"].append(f"First author '{fam}' but Crossref says '{cr_fam}'.")
                if cr.get("retracted"):
                    notes = ", ".join(n.get("doi") or "" for n in cr.get("retraction_notices", []))
                    res["problems"].append(f"RETRACTED or withdrawn. Notice: {notes or 'see publisher page'}. "
                                           f"Remove or replace this citation.")
                    res["status"] = "retracted"
                res["in_library"] = bool(lib.store.paper_by_doi(cr["doi"]))
        except ConnectionError:
            res["problems"].append("Could not reach Crossref; not checked.")
            res["status"] = "unchecked"
        if res["problems"] and res["status"] == "ok":
            res["status"] = "check"
        results.append(res)

    counts = {s: sum(r["status"] == s for r in results) for s in ("ok", "check", "retracted", "unchecked")}
    lines = [f"# DOI check: {path.name}", "", f"{len(results)} references: {counts['ok']} fine, "
             f"{counts['check']} need a look, {counts['retracted']} retracted, {counts['unchecked']} not checked.", ""]
    for r in results:
        if r["problems"]:
            lines.append(f"## {r['key']} ({r['status']})")
            lines += [f"- {p}" for p in r["problems"]]
            lines.append("")
    report = lib.cfg.reports_dir / f"doi-check-{path.stem}-{_stamp()}.md"
    report.write_text("\n".join(lines))

    fixed_path = None
    if write_fixed and any(r["fixes"] for r in results):
        fixed_path = path.with_name(path.stem + ".checked.bib")
        fixed_path.write_text(_apply_fixes(path.read_text(errors="replace"), entries, results))
    return {"counts": counts, "report": str(report), "fixed_bib": str(fixed_path) if fixed_path else None,
            "problems": [r for r in results if r["problems"]]}


def _apply_fixes(text: str, entries: list[dict], results: list[dict]) -> str:
    """Write a corrected copy: original entries, with DOI/year/title fixes applied. The original is never touched."""
    out = []
    for e, r in zip(entries, results):
        e = dict(e)
        for k, v in r["fixes"].items():
            e[k] = v
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in e.items() if k not in ("ENTRYTYPE", "ID"))
        out.append(f"@{e['ENTRYTYPE']}{{{e['ID']},\n{body}\n}}")
    return "\n\n".join(out) + "\n"


# ------------------------------------------------------------------ fetch missing papers
def fetch_missing(lib: Library, bib_path: str, index_after: bool = True) -> dict:
    """For each reference not yet in the library: get a free legal PDF if one exists, else list library links."""
    path = Path(bib_path).expanduser()
    entries = parse_bib(path.read_text(errors="replace"))
    email = lib.cfg.email
    got, have, need, nodoi = [], [], [], []
    for e in entries:
        doi = scholarly.clean_doi(e.get("doi"))
        title = bib_title(e)
        if not doi and title:
            try:
                hits = scholarly.crossref_search(title, email, author=bib_first_author_family(e), rows=1)
                if hits and scholarly.title_similarity(hits[0]["title"], title) >= 0.9:
                    doi = hits[0]["doi"]
            except ConnectionError:
                pass
        if not doi:
            nodoi.append({"key": e["ID"], "title": title})
            continue
        if lib.store.paper_by_doi(doi):
            have.append(e["ID"])
            continue
        dest = lib.cfg.inbox / f"{e['ID']}.pdf"
        if dest.exists():
            have.append(e["ID"])
            continue
        ok_src = None
        for src, url in scholarly.find_open_pdf(doi, email):
            if scholarly.download_pdf(url, dest, email):
                ok_src = src
                break
        if ok_src:
            got.append({"key": e["ID"], "doi": doi, "source": ok_src})
            log(f"downloaded {e['ID']} from {ok_src}")
        else:
            need.append({"key": e["ID"], "doi": doi, "title": title})

    lines = [f"# Papers to download by hand: {path.name}", "",
             "Click each link while signed in to the Brunel library, save the PDF into the `inbox` folder "
             "(any file name), then ask Claude to update the library.", ""]
    for n in need:
        lines.append(f"- [ ] **{n['key']}**: {n['title']}")
        if lib.cfg.proxy_prefix:
            lines.append(f"  - Brunel library: {lib.cfg.proxy_prefix}{quote('https://doi.org/' + n['doi'], safe='')}")
        lines.append(f"  - Publisher: https://doi.org/{n['doi']}")
    for n in nodoi:
        lines.append(f"- [ ] **{n['key']}** (no DOI found): {n['title']}  "
                     f"https://scholar.google.com/scholar?q={quote_plus(n['title'])}")
    todo_file = lib.cfg.inbox / f"to-download-{path.stem}.md"
    if need or nodoi:
        todo_file.write_text("\n".join(lines) + "\n")
    idx = lib.update().summary() if (index_after and got) else None
    return {"already_in_library": len(have), "downloaded_free": got, "need_manual": len(need) + len(nodoi),
            "manual_list": str(todo_file) if (need or nodoi) else None, "index": idx}


# ------------------------------------------------------------------ BibTeX export
def export_bib(lib: Library, refs: list[str] | None = None, out_name: str | None = None) -> dict:
    """BibTeX for the given papers (citekeys, DOIs or titles), or the whole library if refs is empty."""
    rows, missing = [], []
    if refs:
        for ref in refs:
            r = lib.store.find_paper(ref)
            (rows.append(r) if r else missing.append(ref))
    else:
        rows = lib.store.all_papers()
    seen, entries = set(), []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        entries.append(to_bibtex(dict(r)))
    text = "\n\n".join(entries) + "\n"
    out = lib.cfg.bib_dir / (out_name or f"gautam-{_stamp()}.bib")
    if not out.suffix:
        out = out.with_suffix(".bib")
    out.write_text(text)
    weak = [r["citekey"] for r in rows if r["meta_source"] not in ("crossref", "arxiv", "europepmc")]
    return {"file": str(out), "n": len(entries), "not_found": missing, "unverified_details": weak,
            "retracted": [r["citekey"] for r in rows if r["retracted"]], "bibtex": text}


# ------------------------------------------------------------------ one-page summaries
SUMMARY_TEMPLATE = """\
# {title}
**{authors_short} ({year})**, *{journal}*. DOI: {doi}  ·  key: `{citekey}`

## The point in one sentence
<the simplest true sentence: what they found>

## Key points
1. <finding, with the number that supports it and the page>
2. ...
3. ...

## How they did it
<system, method, timescale or sample size, controls>

## Numbers worth remembering
- <value, units, page>

## Limits and doubts
- <assumption or weak point>  → <what would settle it>

## Why it matters for my work
<link to protein dynamics, loops, allostery, β-lactamases, PDUs; or "none">

## Quotable sentence
> <one short sentence, under 15 words, with page>
"""


def _authors_short(p) -> str:
    a = json.loads(p["authors"] or "[]")
    if not a:
        return "Unknown"
    fam = a[0].get("family", "")
    return fam if len(a) == 1 else f"{fam} & {a[1].get('family', '')}" if len(a) == 2 else f"{fam} et al."


def paper_card(lib: Library, ref: str) -> dict | None:
    p = lib.store.find_paper(ref)
    if p is None:
        return None
    summ = lib.cfg.summaries_dir / f"{p['citekey']}.md"
    return {"id": p["id"], "citekey": p["citekey"], "title": p["title"], "authors": json.loads(p["authors"] or "[]"),
            "year": p["year"], "journal": p["journal"], "doi": p["doi"], "pages_in_pdf": p["n_pages"],
            "path": p["path"], "details_checked_with_crossref": p["meta_source"] == "crossref",
            "retracted": bool(p["retracted"]), "summary_file": str(summ) if summ.exists() else None}


def paper_text(lib: Library, ref: str, max_chars: int = 80000, start: int = 0) -> dict | None:
    p = lib.store.find_paper(ref)
    if p is None:
        return None
    t = lib.store.fulltext(p["id"])
    return {"citekey": p["citekey"], "title": p["title"], "chars_total": len(t), "start": start,
            "text": t[start:start + max_chars], "more": start + max_chars < len(t),
            "summary_template": SUMMARY_TEMPLATE.format(
                title=p["title"], authors_short=_authors_short(p), year=p["year"] or "n.d.",
                journal=p["journal"] or "", doi=p["doi"] or "none", citekey=p["citekey"])}


def save_summary(lib: Library, ref: str, markdown: str) -> str | None:
    p = lib.store.find_paper(ref)
    if p is None:
        return None
    out = lib.cfg.summaries_dir / f"{p['citekey']}.md"
    out.write_text(markdown.strip() + "\n")
    return str(out)
