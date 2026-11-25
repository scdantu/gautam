"""The library: indexing new PDFs (only what changed) and hybrid search."""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

import numpy as np
from multiprocessing import get_context

from . import scholarly
from .bib import make_citekey
from .config import Config, load_config
from .embed import make_embedder
from .pdftext import extract_job, find_arxiv, find_doi, is_supplement
from .store import Store


def log(msg: str) -> None:
    print(f"[gautam] {msg}", file=sys.stderr, flush=True)  # never stdout: that is the MCP channel


def sha1_file(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


_STOP = set("""a an and are as at be been but by can could did do does for from had has have how if in into is it
its may more most no not of on or our so such than that the their them then there these they this those to
was we were what when where which while who why will with would using used use between versus about over under
via its also both each other only same some very""".split())


class Progress:
    """Prints 'stage: 37/120 (31%) · 1.8/s · ~46s left · last item', at most every few seconds."""

    def __init__(self, label: str, total: int, unit: str = "files", every: float = 3.0):
        self.label, self.total, self.unit, self.every = label, total, unit, every
        self.n, self.t0, self.last = 0, time.time(), 0.0

    def step(self, k: int = 1, item: str = "") -> None:
        self.n += k
        now = time.time()
        if now - self.last < self.every and self.n < self.total:
            return
        self.last = now
        el = now - self.t0
        rate = self.n / el if el > 0 else 0
        left = (self.total - self.n) / rate if rate > 0 else 0
        pct = 100 * self.n / self.total if self.total else 100
        eta = f"~{_dur(left)} left" if self.n < self.total else f"done in {_dur(el)}"
        msg = f"{self.label}: {self.n}/{self.total} {self.unit} ({pct:.0f}%) · {rate:.1f}/s · {eta}"
        if item:
            msg += f" · {item[:60]}"
        log(msg)


def _dur(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s" if sec >= 60 else f"{sec}s"


@dataclass
class IndexReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    pending: int = 0  # new files left for next run (when a limit is set)
    seconds: float = 0.0
    meta_found: int = 0
    duplicates: list[str] = field(default_factory=list)
    embedded: int = 0
    t_extract: float = 0.0
    t_meta: float = 0.0
    t_embed: float = 0.0

    def summary(self) -> str:
        s = (f"Added {len(self.added)}, updated {len(self.updated)}, moved {len(self.moved)}, "
             f"removed {len(self.removed)}, duplicates dropped {len(self.duplicates)}, "
             f"failed {len(self.failed)} in {self.seconds:.0f}s.")
        if self.added or self.updated or self.embedded or self.meta_found:
            s += (f"\nStage times: 1 reading PDFs {self.t_extract:.0f}s; 2 Crossref {self.t_meta:.0f}s "
                  f"({self.meta_found} found); 3 fingerprinting {self.t_embed:.0f}s ({self.embedded} passages).")
        if self.pending:
            s += f" {self.pending} new PDFs still waiting (run update again)."
        if self.failed:
            s += "\nFailed: " + "; ".join(f"{Path(p).name}: {e}" for p, e in self.failed[:10])
        return s


class Library:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or load_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = make_embedder(self.cfg)
        self._check_model()

    def _check_model(self) -> None:
        sig = {"backend": self.cfg.backend, "model": self.cfg.model, "dim": self.cfg.dim}
        old = self.store.get_setting("embedding")
        if old is None:
            self.store.set_setting("embedding", sig)
        elif old != sig and self.store.stats()["chunks"]:
            raise RuntimeError(f"The index was built with {old} but settings now say {sig}. "
                               f"Either restore the old settings or delete {self.cfg.db_path} and re-index.")

    # ------------------------------------------------------------ scanning
    def scan(self) -> tuple[list[Path], list, list[str]]:
        """Quick look (file sizes and dates only): which PDFs are new or changed, which have gone."""
        seen: set[str] = set()
        todo: list[Path] = []
        reachable_dirs = []
        for d in self.cfg.all_dirs():
            if not d.exists():
                log(f"folder not found, skipping: {d}")
                continue
            reachable_dirs.append(str(d))
            for root, _dirs, files in os.walk(d):
                for f in files:
                    if f.lower().endswith(".pdf") and not f.startswith("."):
                        p = Path(root) / f
                        seen.add(str(p))
                        try:
                            st = p.stat()
                        except OSError:
                            continue
                        row = self.store.paper_by_path(str(p))
                        if row is None and self.store.is_skipped(str(p), st.st_size, st.st_mtime):
                            continue
                        if row is None or row["size"] != st.st_size or abs((row["mtime"] or 0) - st.st_mtime) > 1:
                            todo.append(p)
        gone = [r for r in self.store.all_papers()
                if r["path"] not in seen and any(r["path"].startswith(d) for d in reachable_dirs)]
        return todo, gone, reachable_dirs

    # ------------------------------------------------------------ indexing
    def update(self, limit: int | None = None, online: bool = True, workers: int | None = None,
               stages: tuple[str, ...] = ("read", "meta", "embed")) -> IndexReport:
        """Three stages, each finished for every paper before the next starts, each saved as it goes:
        1. read: text out of all new/changed PDFs (parallel); 2. meta: Crossref details (parallel lookups);
        3. embed: fingerprint every passage still lacking one, in big GPU batches.
        Interrupt at any point and the next run carries on where it stopped."""
        t0 = time.time()
        rep = IndexReport()
        if "read" in stages:
            self._stage_read(rep, limit, workers)
        if "meta" in stages and online:
            self._stage_meta(rep)
        if "embed" in stages:
            self._stage_embed(rep)
        rep.seconds = time.time() - t0
        self.store.set_setting("last_update", time.time())
        return rep

    # ---- stage 1: read
    def _stage_read(self, rep: IndexReport, limit, workers) -> None:
        t = time.time()
        todo, gone, _ = self.scan()
        gone_ids = {r["id"] for r in gone}
        if limit is not None and len(todo) > limit:
            rep.pending = len(todo) - limit
            todo = todo[:limit]
        workers = workers or max(1, min(4, (os.cpu_count() or 2) - 1))
        seen_sha: set[str] = set()
        if todo:
            log(f"stage 1/3: reading {len(todo)} PDFs with {workers} workers")
        prog = Progress("stage 1/3 read", len(todo), "PDFs")

        def handle(r: dict) -> None:
            prog.step(item=Path(r["path"]).name)
            _handle(r)

        def _handle(r: dict) -> None:
            p = r["path"]
            if not r["ok"]:
                rep.failed.append((p, r["error"]))
                log(f"failed {Path(p).name}: {r['error']}")
                return
            sha, existing = r["sha"], self.store.paper_by_path(p)
            same = self.store.paper_by_sha1(sha)
            if same is not None and same["path"] != p:
                if same["id"] in gone_ids or not os.path.exists(same["path"]):
                    # moved/renamed, or the index came from another machine / PDFs were archived: relink
                    self.store.move_paper(same["id"], p, r["size"], r["mtime"])
                    gone_ids.discard(same["id"])
                    rep.moved.append(p)
                    return
                if existing is None:
                    log(f"duplicate of {Path(same['path']).name}, skipping {Path(p).name}")
                    self.store.skip_file(p, r["size"], r["mtime"], f"same file as {same['path']}")
                    return
            if existing is not None and existing["sha1"] == sha:  # only the date changed
                self.store.move_paper(existing["id"], p, r["size"], r["mtime"])
                return
            if sha in seen_sha:
                log(f"duplicate within this batch, skipping {Path(p).name}")
                return
            seen_sha.add(sha)
            if not r["chunks"]:
                rep.failed.append((p, "no readable text (scanned image PDF? needs OCR)"))
                return
            c = r["content"]
            pdf_title = c.meta_title  # already checked: not "Microsoft Word - ...", not a file name
            name = Path(p).name
            doi = find_doi(c.first_pages)
            arx = find_arxiv(c.first_pages, name)
            if not doi and arx:
                doi = f"10.48550/arxiv.{arx}"
            meta = {"doi": doi, "title": pdf_title or Path(p).stem.replace("_", " "),
                    "authors": [], "year": None, "meta_source": "pdf", "pdf_title": pdf_title, "stage": "read"}
            if is_supplement(c.first_pages, name):
                # supplementary file: keep it searchable, but don't let it pose as (or duplicate) the main paper
                meta.update({"doi": None, "type": "supplement", "stage": "meta", "pdf_title": "",
                             "title": "Supplementary information: " + (pdf_title or Path(p).stem)})
            if existing is not None:
                self.store.delete_paper(existing["id"])
            meta["citekey"] = make_citekey(meta, taken=self.store.citekey_taken)
            self.store.add_paper(p, sha, r["size"], r["mtime"], meta, r["chunks"], None, c.fulltext, c.n_pages)
            (rep.updated if existing is not None else rep.added).append(p)

        if workers == 1 or len(todo) <= 2:
            for p in todo:
                handle(extract_job(str(p)))
        else:
            with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pp:
                for f in as_completed([pp.submit(extract_job, str(p)) for p in todo]):
                    handle(f.result())
        for r in gone:
            if r["id"] in gone_ids:
                self.store.delete_paper(r["id"])
                rep.removed.append(r["path"])
        rep.t_extract = time.time() - t

    def archive(self) -> str:
        """Mark every PDF-based paper as 'archived' so its PDF can be deleted while its text stays searchable.
        If the same PDF turns up again later (e.g. on your Mac), it is re-linked, not re-read."""
        rows = [r for r in self.store.all_papers()
                if not r["path"].startswith(("archived:", "epmc:"))]
        for r in rows:
            self.store.db.execute("UPDATE papers SET path=? WHERE id=?", (f"archived:{r['path']}", r["id"]))
        self.store.db.commit()
        return f"Archived {len(rows)} papers. Their PDFs can now be deleted; text and fingerprints are kept."

    # ---- stage 2: details from Crossref
    def _lookup(self, row) -> dict | None:
        doi = row["doi"]
        if doi and doi.startswith("10.48550/arxiv."):
            m = scholarly.arxiv_meta(doi.split("arxiv.", 1)[1], self.cfg.email)
            if m:
                return m
        elif doi:
            cr = scholarly.crossref_work(doi, self.cfg.email)
            # a DOI read from a PDF sometimes carries a trailing path (10.1042/bcj20200477/888170/...): trim it
            while cr is None and doi.count("/") >= 2:
                doi = doi.rsplit("/", 1)[0]
                cr = scholarly.crossref_work(doi, self.cfg.email)
            if cr:
                return cr
        title = row["pdf_title"] if "pdf_title" in row.keys() else row["title"]
        if title:
            hits = scholarly.crossref_search(title, self.cfg.email, rows=1)
            if hits and scholarly.title_similarity(hits[0]["title"], title) > 0.9:
                return hits[0]
        return None

    def _stage_meta(self, rep: IndexReport, rows=None) -> int:
        t = time.time()
        rows = rows if rows is not None else self.store.papers_in_stage("read")
        if not rows:
            return 0
        log(f"stage 2/3: looking up {len(rows)} papers in Crossref")
        prog = Progress("stage 2/3 Crossref", len(rows), "papers")
        n = 0
        with ThreadPoolExecutor(max_workers=4) as tp:  # lookups in threads; database writes stay here
            futs = {tp.submit(self._lookup, r): r for r in rows}
            for f in as_completed(futs):
                r = futs[f]
                try:
                    cr = f.result()
                except ConnectionError:
                    log("Crossref unreachable; details will be filled in on the next update")
                    continue
                except Exception as e:
                    log(f"lookup failed for {r['citekey']}: {e}")
                    cr = None
                meta = dict(cr) if cr else {}
                if cr and cr.get("doi"):
                    other = self.store.paper_by_doi(cr["doi"])
                    if other is not None and other["id"] != r["id"]:
                        # same paper twice (e.g. two copies of a PDF, or a PDF already harvested): keep one
                        keep, drop = (other, r) if (other["n_chunks"] or 0) >= (r["n_chunks"] or 0) else (r, other)
                        log(f"duplicate DOI {cr['doi']}: keeping {Path(keep['path']).name}, "
                            f"dropping {Path(drop['path']).name}")
                        self.store.delete_paper(drop["id"])
                        rep.duplicates.append(drop["path"])
                        if not drop["path"].startswith(("epmc:", "archived:")):
                            self.store.skip_file(drop["path"], drop["size"], drop["mtime"],
                                                 f"same DOI as {keep['path']}")
                        prog.step(item=f"duplicate: {cr['title'] or ''}")
                        if drop["id"] == r["id"]:
                            continue
                if cr:
                    meta["citekey"] = make_citekey(cr, taken=lambda k: self.store.citekey_taken(k, r["id"]))
                    rep.meta_found += 1
                meta["stage"] = "meta"
                self.store.update_meta(r["id"], meta)
                n += 1
                prog.step(item=("found: " + (cr["title"] or "")) if cr else f"not found: {r['title'] or ''}")
        rep.t_meta = time.time() - t
        return n

    # ---- stage 3: fingerprints, in big batches
    def _stage_embed(self, rep: IndexReport, batch: int = 64) -> None:
        t = time.time()
        total = self.store.count_unembedded()
        if not total:
            return
        log(f"stage 3/3: fingerprinting {total} passages (model loads first, ~30s)")
        prog = Progress("stage 3/3 fingerprint", total, "passages")
        done = 0
        while True:
            rows = self.store.unembedded_chunks(batch)
            if not rows:
                break
            texts = [f"{r['title'] or ''}\n{r['section'] or ''}\n{r['text']}" for r in rows]  # context helps
            embs = self.embedder.embed_docs(texts)
            self.store.set_embeddings([r["id"] for r in rows], embs)
            done += len(rows)
            rep.embedded += len(rows)
            prog.step(len(rows), item=rows[-1]["title"] or "")
        self.store.mark_embedded_papers_done()
        rep.t_embed = time.time() - t

    def refresh_metadata(self, only_missing: bool = True) -> str:
        """Fill in or re-check details from Crossref (e.g. after an offline index, or to catch new retractions)."""
        rows = [r for r in self.store.all_papers() if not (only_missing and r["meta_source"] == "crossref")]
        before = {r["id"]: r["retracted"] for r in rows}
        rep = IndexReport()
        self._stage_meta(rep, rows)
        newly = sum(1 for r in self.store.all_papers() if r["id"] in before and r["retracted"] and not before[r["id"]])
        return f"Checked {len(rows)} papers; details found for {rep.meta_found}; {newly} newly flagged as retracted."

    # ------------------------------------------------------------ search
    def _scores(self, mat, q):
        """Similarity of the question to every passage. Uses the GPU when the model is on one: the fingerprints
        are copied there once (about 0.8 GB per million passages) and each search is then a single fast step."""
        dev = getattr(self.embedder, "_dev", "cpu")
        if dev in ("mps", "cuda"):
            try:
                import torch
                key = (id(mat), mat.shape)
                if getattr(self, "_tkey", None) != key:
                    self._tmat = torch.from_numpy(np.ascontiguousarray(mat, dtype=np.float16)).to(dev)
                    self._tkey = key
                qt = torch.from_numpy(q.astype(np.float16)).to(dev)
                return (self._tmat @ qt).float().cpu().numpy()
            except Exception as e:  # fall back to the CPU below
                log(f"GPU search unavailable ({e}); using CPU")
        sims = np.empty(len(mat), dtype=np.float32)
        step = 200_000  # score in slices so a big library never needs a big block of memory
        for a in range(0, len(mat), step):
            sims[a:a + step] = np.asarray(mat[a:a + step], dtype=np.float32) @ q
        return sims

    def search(self, query: str, k: int = 8, year_from: int | None = None, year_to: int | None = None,
               per_paper: int = 2, mode: str = "hybrid") -> list[dict]:
        """Meaning search + keyword search, merged by rank (reciprocal rank fusion).
        mode = "hybrid" (both), "vector" (meaning only) or "keyword" (exact words only)."""
        ids, mat = self.store.matrix()
        ranks: dict[int, float] = {}
        cos: dict[int, float] = {}
        if len(ids) and mode in ("hybrid", "vector"):
            q = self.embedder.embed_query(query).astype(np.float32)
            sims = self._scores(mat, q)
            top = np.argpartition(-sims, min(100, len(sims) - 1))[:100]
            top = top[np.argsort(-sims[top])]
            for r, i in enumerate(top):
                ranks[int(ids[i])] = ranks.get(int(ids[i]), 0) + 1 / (60 + r)
                cos[int(ids[i])] = float(sims[i])
        words = [w for w in re.findall(r"[\w\-]+", query) if len(w) > 1 and w.lower() not in _STOP]
        if words and mode in ("hybrid", "keyword"):
            fts_q = " OR ".join('"' + w.replace('"', "") + '"' for w in words)
            for r, cid in enumerate(self.store.fts(fts_q, 100)):
                ranks[cid] = ranks.get(cid, 0) + 1 / (60 + r)
        order = sorted(ranks, key=ranks.get, reverse=True)
        rows = self.store.chunk_rows(order[:300])
        out, per = [], {}
        for cid in order:
            r = rows.get(cid)
            if r is None:
                continue
            if year_from and (r["year"] or 0) < year_from or year_to and (r["year"] or 9999) > year_to:
                continue
            if per.get(r["paper_id"], 0) >= per_paper:
                continue
            per[r["paper_id"]] = per.get(r["paper_id"], 0) + 1
            out.append({"citekey": r["citekey"], "title": r["title"], "year": r["year"], "doi": r["doi"],
                        "journal": r["journal"], "page": r["page"], "section": r["section"], "text": r["text"],
                        "retracted": bool(r["retracted"]), "score": round(ranks[cid] * 1000, 2),
                        "similarity": round(cos[cid], 3) if cid in cos else None,
                        "abstract_only": r["type"] == "abstract",
                        "path": r["path"]})
            if len(out) >= k:
                break
        return out
