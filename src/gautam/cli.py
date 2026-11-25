"""Command line: `uv run gautam <command>`. Use this for the first big index."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .config import PROJECT_ROOT


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="gautam", description="Project Gautam: your local paper library")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create gautam.toml from the example")
    p = sub.add_parser("index", help="index new/changed PDFs")
    p.add_argument("--limit", type=int, help="at most this many files this run (e.g. 50 for batches)")
    p.add_argument("--workers", type=int, help="PDFs read in parallel (default: up to 4)")
    p.add_argument("--stage", choices=["read", "meta", "embed", "all"], default="all",
                   help="run just one stage (default: all three, in order)")
    p.add_argument("--offline", action="store_true", help="skip Crossref lookups (fill in later with refresh)")
    sub.add_parser("status", help="what is indexed and what is waiting")
    p = sub.add_parser("search", help="search the library")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=8)
    p = sub.add_parser("verify", help="check a .bib file against Crossref")
    p.add_argument("bib")
    p = sub.add_parser("fetch", help="download free copies of papers in a .bib that you do not have")
    p.add_argument("bib")
    p = sub.add_parser("export", help="write BibTeX")
    p.add_argument("refs", nargs="*", help="citekeys/DOIs; none = whole library")
    p.add_argument("-o", "--out")
    p = sub.add_parser("eval", help="score search quality on known-answer questions")
    p.add_argument("questions", nargs="?", default=str(PROJECT_ROOT / "tests" / "eval_pduarticles.json"))
    p.add_argument("--modes", default="vector,keyword,hybrid")
    sub.add_parser("archive", help="keep papers' text but stop tracking their PDFs (then the PDFs can be deleted)")
    p = sub.add_parser("harvest", help="open-access full text from Europe PMC for your topics")
    p.add_argument("step", choices=["count", "collect", "fetch", "todo", "download", "ingest", "abstracts"],
                   help="count/collect/fetch as before; todo = write the list of IDs still needed; "
                        "download = network only, saves XML files (login node or laptop); "
                        "ingest = read those files into the library (inside a job)")
    p.add_argument("--scope", choices=["open_access", "all"], default="open_access",
                   help="count: open-access full text only (default) or every record incl. abstract-only")
    p.add_argument("--set", dest="which", choices=["all", "topics", "authors", "reviews"], default="all",
                   help="which searches: all = keyword topics + key authors (default); reviews = open-access reviews")
    p.add_argument("--reviews", action="store_true", help="shortcut for --set reviews")
    p.add_argument("--to", default="harvest_xml", help="folder for download")
    p.add_argument("--from", dest="src", default="harvest_xml", help="folder for ingest")
    p.add_argument("--ids", help="file with PMC IDs to download (from `harvest todo`); default: ask the library")
    p.add_argument("--out", default="harvest_todo.txt", help="file for `harvest todo`")
    p.add_argument("--workers", type=int, help="parallel downloads (fetch): default 3 for ncbi, 8 for epmc")
    p.add_argument("--source", choices=["both", "ncbi", "epmc"],
                   help="fetch: ncbi (default, batches of 100) or epmc (one by one). "
                        "download: both (default: NCBI, then Europe PMC for the rest), ncbi or epmc only")
    p.add_argument("--limit", type=int, help="at most this many papers (fetch) or per topic (collect)")
    p = sub.add_parser("graph", help="grow the library along its citation graph (weighted, best-first)")
    p.add_argument("step", choices=["refs", "score", "top", "add", "round", "coverage", "settings"],
                   help="refs = read reference lists; score = weigh missing papers; top = show the heaviest; "
                        "add = add them; round = refs+score+add repeated; coverage = how complete; "
                        "settings = list the [graph] settings")
    p.add_argument("--xml", help="folder of downloaded full-text XML to read reference lists from (e.g. harvest_xml)")
    p.add_argument("--offline", action="store_true", help="do not fetch reference lists online")
    p.add_argument("--no-crossref", action="store_true", help="skip Crossref reference lists (for PDFs)")
    p.add_argument("-n", type=int, default=50, help="how many to show (top)")
    p.add_argument("--rounds", type=int, default=3, help="most rounds (round)")
    p.add_argument("--budget", type=int, help="papers per round (default: [graph] budget)")
    p = sub.add_parser("refresh", help="fill in/re-check paper details from Crossref")
    p.add_argument("--all", action="store_true", help="re-check every paper (catches new retractions)")
    a = ap.parse_args(argv)
    import logging
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="[gautam] %(message)s")

    if a.cmd == "graph" and a.step == "settings":
        from .graph.settings import GraphSettings
        print(GraphSettings.describe())
        return

    if a.cmd == "init":
        dst = PROJECT_ROOT / "gautam.toml"
        if dst.exists():
            print(f"{dst} already exists")
        else:
            shutil.copy(PROJECT_ROOT / "gautam.example.toml", dst)
            print(f"Created {dst}. Add your PDF folder(s) and email, then run: uv run gautam index --limit 20")
        return

    if getattr(a, "reviews", False):
        a.which = "reviews"

    from . import tools
    from .library import Library

    L = Library()
    if a.cmd == "index":
        print(L.update(limit=a.limit, online=not a.offline, workers=a.workers,
                       stages=("read", "meta", "embed") if a.stage == "all" else (a.stage,)).summary())
        print(json.dumps(L.store.stats()))
    elif a.cmd == "status":
        todo, gone, dirs = L.scan()
        print(json.dumps({**L.store.stats(), "waiting": len(todo), "to_remove": len(gone), "folders": dirs}, indent=2))
    elif a.cmd == "search":
        for i, h in enumerate(L.search(a.query, k=a.k), 1):
            flag = " RETRACTED" if h["retracted"] else ""
            print(f"\n[{i}] {h['citekey']} p.{h['page']} ({h['year']}){flag}  {h['title']}\n    {h['text'][:400]}...")
    elif a.cmd == "verify":
        r = tools.verify_bib(L, a.bib)
        print(json.dumps(r["counts"]), "\nReport:", r["report"], "\nCorrected copy:", r["fixed_bib"])
    elif a.cmd == "fetch":
        r = tools.fetch_missing(L, a.bib)
        print(json.dumps({k: v for k, v in r.items()}, indent=2))
    elif a.cmd == "export":
        r = tools.export_bib(L, a.refs or None, a.out)
        print(f"Wrote {r['n']} entries to {r['file']}")
        if r["not_found"]:
            print("Not found:", ", ".join(r["not_found"]), file=sys.stderr)
    elif a.cmd == "eval":
        import time as _t

        from . import evaluate
        r = evaluate.run(L, a.questions, modes=tuple(a.modes.split(",")))
        print(evaluate.report(r))
        out = L.cfg.reports_dir / f"eval-{_t.strftime('%Y%m%d-%H%M')}.json"
        out.write_text(json.dumps(r, indent=1))
        print(f"\nFull results: {out}")
    elif a.cmd == "graph":
        _graph(L, a)
    elif a.cmd == "archive":
        print(L.archive())
    elif a.cmd == "harvest":
        from .harvest import Harvester
        H = Harvester(L)
        if a.step == "abstracts":
            print(json.dumps(H.abstracts(max_per_topic=a.limit, which=a.which), indent=2))
        elif a.step == "count":
            c = H.count(scope=a.scope, which=a.which)
            print(json.dumps(c, indent=2))
            nums = [v for v in c.values() if isinstance(v, int)]
            print(f"Sum over topics: {sum(nums)} (overlaps are removed at collect, so the real total is lower)")
        elif a.step == "collect":
            print(json.dumps(H.collect(max_per_topic=a.limit, which=a.which), indent=2))
        elif a.step == "todo":
            ids = H.todo_ids()
            Path(a.out).write_text("\n".join(ids) + "\n")
            print(f"{len(ids)} IDs written to {a.out}")
        elif a.step == "download":
            ids = Path(a.ids).read_text().split() if a.ids else None
            print(json.dumps(H.download(a.to, ids=ids, source=a.source or "both", workers=a.workers or 3), indent=2))
        elif a.step == "ingest":
            print(json.dumps(H.ingest(a.src), indent=2))
        else:
            if a.source in (None, "ncbi", "both"):
                print(json.dumps(H.fetch_ncbi(workers=a.workers or 3, limit=a.limit), indent=2))
            else:
                print(json.dumps(H.fetch(workers=a.workers or 8, limit=a.limit), indent=2))
    elif a.cmd == "refresh":
        print(L.refresh_metadata(only_missing=not a.all))


def _graph(L, a) -> None:
    """Run one `gautam graph` step and print its result."""
    from .graph import GraphExpander, GraphSettings
    from .harvest import Harvester

    settings = GraphSettings.from_mapping(L.cfg.raw.get("graph"))
    if a.budget:
        settings = settings.copy(budget=a.budget)
    G = GraphExpander(L, Harvester(L), settings)
    online, crossref = not a.offline, not a.no_crossref
    if a.step == "refs":
        out = G.collect_references(a.xml, online, crossref)
    elif a.step == "score":
        out = G.score()
    elif a.step == "add":
        out = G.add()
    elif a.step == "round":
        out = G.run(a.rounds, a.xml, online, crossref)
    elif a.step == "coverage":
        out = G.graph.coverage()
    else:  # top
        print(f"{'weight':>6} {'cited':>5} {'hop':>3} {'relev':>5}  year  title")
        for r in G.graph.top(a.n):
            print(f"{r['weight'] or 0:6.3f} {r['cited_by']:5d} {r['hop']:3d} "
                  f"{r['relevance'] or 0:5.2f}  {r['year'] or '':4}  {(r['title'] or r['key'])[:90]}")
        return
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
