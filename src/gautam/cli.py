"""Command line: `uv run gautam <command>`. Use this for the first big index."""
from __future__ import annotations

import argparse
import json
import shutil
import sys

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
    p.add_argument("step", choices=["count", "collect", "fetch"])
    p.add_argument("--workers", type=int, default=8, help="parallel downloads (fetch)")
    p.add_argument("--limit", type=int, help="at most this many papers (fetch) or per topic (collect)")
    p = sub.add_parser("refresh", help="fill in/re-check paper details from Crossref")
    p.add_argument("--all", action="store_true", help="re-check every paper (catches new retractions)")
    a = ap.parse_args(argv)

    if a.cmd == "init":
        dst = PROJECT_ROOT / "gautam.toml"
        if dst.exists():
            print(f"{dst} already exists")
        else:
            shutil.copy(PROJECT_ROOT / "gautam.example.toml", dst)
            print(f"Created {dst}. Add your PDF folder(s) and email, then run: uv run gautam index --limit 20")
        return

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
    elif a.cmd == "archive":
        print(L.archive())
    elif a.cmd == "harvest":
        from .harvest import Harvester
        H = Harvester(L)
        if a.step == "count":
            c = H.count()
            print(json.dumps(c, indent=2))
            nums = [v for v in c.values() if isinstance(v, int)]
            print(f"Sum over topics: {sum(nums)} (overlaps are removed at collect, so the real total is lower)")
        elif a.step == "collect":
            print(json.dumps(H.collect(max_per_topic=a.limit), indent=2))
        else:
            print(json.dumps(H.fetch(workers=a.workers, limit=a.limit), indent=2))
    elif a.cmd == "refresh":
        print(L.refresh_metadata(only_missing=not a.all))


if __name__ == "__main__":
    main()
