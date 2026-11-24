"""Measure search quality with known-answer questions: `gautam eval tests/eval_pduarticles.json`.

For each question we know which paper(s) hold the answer. We ask: is a right paper the 1st result? In the
top 3? Top 5? (Hit@1/3/5.) MRR is the average of 1/rank of the first right paper (1 = always first,
0.5 = usually second). For 'nothing should match' questions we record how similar the best passage was,
to see whether a cut-off can tell "no answer here" apart from a real hit.
"""
from __future__ import annotations

import json
import statistics as st
import time
from pathlib import Path

from .library import Library


def run(lib: Library, qfile: str, k: int = 10, modes=("vector", "keyword", "hybrid")) -> dict:
    spec = json.loads(Path(qfile).read_text())
    qs, files = spec["questions"], spec.get("files", {})
    papers = lib.store.all_papers()
    # a target is found by its citekey, or by its PDF's file name (citekeys can differ between machines)
    by_file = {Path(r["path"].removeprefix("archived:")).name: r["citekey"] for r in papers}
    have = {r["citekey"] for r in papers}
    alias = {t: (t if t in have else by_file.get(files.get(t, ""), t)) for q in qs for t in q["targets"]}
    for q in qs:
        q["targets"] = [alias[t] for t in q["targets"]]
    missing = sorted({t for q in qs for t in q["targets"]} - have)
    out = {"n_questions": len(qs), "targets_not_in_index": missing, "modes": {}, "misses": {}}
    for mode in modes:
        ranks, keyhits, neg_top, pos_top, t0 = [], [], [], [], time.time()
        by_type: dict[str, list] = {}
        misses = []
        for q in qs:
            hits = lib.search(q["q"], k=k, mode=mode, per_paper=1)
            if q["type"] == "N":
                if hits and hits[0].get("similarity") is not None:
                    neg_top.append(hits[0]["similarity"])
                continue
            targets = set(q["targets"]) & have
            if not targets:
                continue
            rank = next((i + 1 for i, h in enumerate(hits) if h["citekey"] in targets), None)
            ranks.append(rank)
            by_type.setdefault(q["type"], []).append(rank)
            if rank and hits[rank - 1].get("similarity") is not None:
                pos_top.append(hits[rank - 1]["similarity"])
            if q.get("key"):
                keyhits.append(any(h["citekey"] in targets and q["key"].lower() in h["text"].lower()
                                   for h in hits[:5]))
            if rank is None or rank > 3:
                misses.append({"q": q["q"][:80], "want": sorted(targets), "rank": rank,
                               "got": [h["citekey"] for h in hits[:3]]})
        n = len(ranks)

        def hit(m):
            return round(sum(1 for r in ranks if r and r <= m) / n, 2) if n else None

        res = {"questions_scored": n, "hit@1": hit(1), "hit@3": hit(3), "hit@5": hit(5),
               "MRR": round(sum(1 / r for r in ranks if r) / n, 2) if n else None,
               "passage_has_key@5": round(sum(keyhits) / len(keyhits), 2) if keyhits else None,
               "seconds_per_query": round((time.time() - t0) / len(qs), 2),
               "hit@3_by_type": {t: round(sum(1 for r in v if r and r <= 3) / len(v), 2) for t, v in by_type.items()}}
        if mode != "keyword" and pos_top:
            res["similarity_right_answers"] = {"min": min(pos_top), "median": round(st.median(pos_top), 3)}
        if neg_top:
            res["similarity_no_answer_questions"] = {"max": max(neg_top), "median": round(st.median(neg_top), 3)}
        out["modes"][mode] = res
        out["misses"][mode] = misses
    return out


def report(r: dict) -> str:
    lines = [f"{r['n_questions']} questions."]
    if r["targets_not_in_index"]:
        lines.append(f"(Not in this index, skipped: {', '.join(r['targets_not_in_index'])})")
    lines.append("\nmode      hit@1  hit@3  hit@5  MRR   key-in-passage@5  s/query")
    for m, x in r["modes"].items():
        lines.append(f"{m:9} {x['hit@1']!s:6} {x['hit@3']!s:6} {x['hit@5']!s:6} {x['MRR']!s:5} "
                     f"{x['passage_has_key@5']!s:17} {x['seconds_per_query']}")
    lines.append("\nhit@3 by question type (P paraphrase, K named term, H hard/conceptual):")
    for m, x in r["modes"].items():
        lines.append(f"  {m:9} " + "  ".join(f"{t}: {v}" for t, v in sorted(x["hit@3_by_type"].items())))
    h = r["modes"].get("hybrid") or r["modes"].get("vector")
    if h and "similarity_right_answers" in h and "similarity_no_answer_questions" in h:
        lines.append(f"\nSimilarity of right answers: median {h['similarity_right_answers']['median']}, "
                     f"lowest {h['similarity_right_answers']['min']}. "
                     f"Best match for 'nothing here' questions: median "
                     f"{h['similarity_no_answer_questions']['median']}, highest {h['similarity_no_answer_questions']['max']}.")
    for m, ms in r["misses"].items():
        if ms:
            lines.append(f"\nMisses ({m}, right paper not in top 3):")
            lines += [f"  - rank {x['rank']}: {x['q']}...  want {x['want']}, got {x['got']}" for x in ms]
    return "\n".join(lines)
