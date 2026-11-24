"""Project Gautam as an MCP server, so Claude can search and manage your paper library."""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from . import tools
from .library import Library, log

INSTRUCTIONS = """\
Project Gautam is Sarath's personal library of research papers (full text, indexed locally).
- For questions about the literature, call search_papers first and answer from the returned passages,
  citing [citekey, page]. Say plainly when the library has nothing relevant; do not fill gaps from memory
  without saying so. Flag any passage marked RETRACTED.
- search_papers checks for a few new PDFs itself. For many new files call update_library (in batches).
- One-page summary: get_paper_text, fill its summary_template from the text only, then save_summary.
- For a manuscript or grant: export_bib. To check a reference list: verify_bib. To complete a
  reference list from a .bib: fetch_missing (free legal copies only; the rest become a list of library links).
"""

mcp = FastMCP("gautam", instructions=INSTRUCTIONS)
_lib: Library | None = None


def lib() -> Library:
    global _lib
    if _lib is None:
        _lib = Library()
    return _lib


def _fmt_hits(hits: list[dict]) -> str:
    if not hits:
        return "No matching passages in the library."
    out = []
    for i, h in enumerate(hits, 1):
        flag = "  **RETRACTED**" if h["retracted"] else ""
        sec = f" · {h['section']}" if h["section"] else ""
        where = f", p.{h['page']}" if h["page"] else ""
        out.append(f"[{i}] [{h['citekey']}{where}] {h['title']} ({h['year']}){flag}\n"
                   f"    DOI: {h['doi'] or 'none'}{sec}\n    {h['text']}")
    return "\n\n".join(out)


@mcp.tool()
def search_papers(query: str, k: int = 8, year_from: int | None = None, year_to: int | None = None) -> str:
    """Find the passages in the local paper library that best answer a question.
    Combines meaning-based and exact-keyword search. Returns passages with [citekey, page]."""
    L = lib()
    todo, _, _ = L.scan()
    note = ""
    if todo:
        if len(todo) <= 5:
            L.update(workers=1)
        else:
            note = f"\n\n(Note: {len(todo)} new PDFs are not indexed yet; call update_library.)"
    return _fmt_hits(L.search(query, k=k, year_from=year_from, year_to=year_to)) + note


@mcp.tool()
def update_library(limit: int = 25, workers: int = 4) -> str:
    """Index new, changed, moved or deleted PDFs, reading `workers` PDFs in parallel.
    Works in batches of `limit` files; call again if more remain."""
    return lib().update(limit=limit, workers=workers).summary()


@mcp.tool()
def library_status() -> str:
    """How many papers and passages are indexed, how many are waiting, and where things are stored."""
    L = lib()
    todo, gone, dirs = L.scan()
    s = L.store.stats()
    s.update({"waiting_to_index": len(todo), "to_remove": len(gone), "folders": dirs,
              "summaries": len(list(L.cfg.summaries_dir.glob("*.md")))})
    return json.dumps(s, indent=2)


@mcp.tool()
def list_papers(title_contains: str = "", limit: int = 50) -> str:
    """List papers in the library (citekey, year, title), optionally filtered by words in the title."""
    rows = [r for r in lib().store.all_papers()
            if title_contains.lower() in (r["title"] or "").lower()][:limit]
    return "\n".join(f"{r['citekey']} | {r['year']} | {r['title']} | {r['doi'] or 'no DOI'}"
                     + (" | RETRACTED" if r["retracted"] else "") for r in rows) or "No papers found."


@mcp.tool()
def get_paper(ref: str) -> str:
    """Details of one paper (by citekey, DOI or title words), including its summary if one exists."""
    card = tools.paper_card(lib(), ref)
    if card is None:
        return f"No paper matching '{ref}'."
    if card["summary_file"]:
        card["summary"] = open(card["summary_file"]).read()
    return json.dumps(card, indent=2)


@mcp.tool()
def get_paper_text(ref: str, start: int = 0, max_chars: int = 80000) -> str:
    """Full text of one paper (by citekey, DOI or title), plus the one-page summary template.
    If 'more' is true, call again with start = start + max_chars."""
    t = tools.paper_text(lib(), ref, max_chars=max_chars, start=start)
    return json.dumps(t, indent=1) if t else f"No paper matching '{ref}'."


@mcp.tool()
def save_summary(ref: str, markdown: str) -> str:
    """Save a one-page summary (markdown) for a paper. Overwrites any earlier summary."""
    out = tools.save_summary(lib(), ref, markdown)
    return f"Saved to {out}" if out else f"No paper matching '{ref}'."


@mcp.tool()
def export_bib(refs: list[str] | None = None, file_name: str | None = None) -> str:
    """Write BibTeX for the given papers (citekeys, DOIs or titles), or the whole library if refs is empty."""
    r = tools.export_bib(lib(), refs, file_name)
    head = f"Wrote {r['n']} entries to {r['file']}."
    if r["not_found"]:
        head += f"\nNot in library: {', '.join(r['not_found'])}"
    if r["unverified_details"]:
        head += f"\nDetails not checked against Crossref (check by hand): {', '.join(r['unverified_details'])}"
    if r["retracted"]:
        head += f"\nRETRACTED: {', '.join(r['retracted'])}"
    return head + "\n\n" + r["bibtex"][:20000]


@mcp.tool()
def verify_bib(bib_path: str) -> str:
    """Check every reference in a .bib file against Crossref: fake or broken DOIs, missing DOIs,
    title/year/author mismatches and retractions. Writes a report and a corrected copy (*.checked.bib)."""
    r = tools.verify_bib(lib(), bib_path)
    return json.dumps(r, indent=1)[:30000]


@mcp.tool()
def fetch_missing(bib_path: str) -> str:
    """For each reference in a .bib file that is not in the library, download a free legal PDF
    (arXiv, Unpaywall, Europe PMC) into the inbox and index it. Others go on a to-download list with library links."""
    return json.dumps(tools.fetch_missing(lib(), bib_path), indent=1)


@mcp.tool()
def refresh_metadata(only_missing: bool = True) -> str:
    """Fill in paper details from Crossref, or with only_missing=False re-check all papers (catches new retractions)."""
    return lib().refresh_metadata(only_missing=only_missing)


def main() -> None:
    log("Project Gautam MCP server starting")
    mcp.run()


if __name__ == "__main__":
    main()
