"""Weighted, best-first growth of the library along its citation graph.

One round:
  1. read the reference lists of papers that have none yet
  2. count, for each cited paper we lack, its citation pull (decayed by step)
  3. look up the heaviest candidates (title, abstract, open access?)
  4. score their relevance: closeness of their abstract to your library
  5. add the heaviest eligible ones (abstract now; open-access full text
     queued for `gautam harvest download` / `ingest`)
Rounds repeat until reference coverage stops rising.
"""

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from gautam import scholarly
from gautam.errors import GraphError
from gautam.graph.citation_graph import CitationGraph
from gautam.graph.settings import GraphSettings
from gautam.io.references import (
    CrossrefReferenceClient,
    EpmcRecordClient,
    EpmcReferenceClient,
    JatsReferenceReader,
    NcbiReferenceClient,
    make_reference,
)
from gautam.numerics.graph_weights import combined_weight, select, topk_mean

log = logging.getLogger("gautam.graph")


class GraphExpander:
    """Grow a Library along its citation graph, heaviest papers first."""

    def __init__(self, library, harvester,
                 settings: GraphSettings | None = None):
        """``library``: gautam Library; ``harvester``: gautam Harvester."""
        self.lib = library
        self.harvester = harvester
        self.settings = settings or GraphSettings.from_mapping(
            library.cfg.raw.get("graph"))
        self.graph = CitationGraph(library.store)
        get = harvester._get
        self.epmc_refs = EpmcReferenceClient(get)
        self.crossref_refs = CrossrefReferenceClient(
            lambda url, params: harvester._get(url, params, timeout=60))
        self.records = EpmcRecordClient(get, workers=8)
        key = library.cfg.raw.get("harvest", {}).get("ncbi_api_key", "")
        self._ncbi_gap = 1 / 9.0 if key else 1 / 2.5
        self._ncbi_lock, self._ncbi_last = threading.Lock(), 0.0
        self.ncbi_refs = NcbiReferenceClient(self._ncbi_post)

    # ------------------------------------------------------ 1. references
    def collect_references(self, xml_dir=None, online: bool = True,
                           crossref: bool = True, workers: int = 4,
                           seed_abstracts: bool = False) -> dict:
        """Read reference lists for papers that have none yet, in bulk.

        Order, cheapest first: (1) XML files already on disk, parsed on many
        cores; (2) papers with a PMC ID: full text downloaded from NCBI 100
        at a time, then parsed; (3) abstract-only papers: NCBI reference
        lists 100 per request; (4) what is left: Europe PMC or Crossref,
        one paper per request, ``workers`` at a time.
        """
        g = self.graph
        need = {r["id"]: r
                for r in g.papers_needing_references(seed_abstracts)}
        counts = {"from_xml": 0, "ncbi_batch": 0, "one_by_one": 0}
        counts["from_xml"] = self._refs_from_xml(xml_dir, need)
        if online and need:
            folder = xml_dir or "harvest_xml"
            pmc = [r["path"].removeprefix("epmc:") for r in need.values()
                   if r["path"].startswith("epmc:PMC")]
            if pmc:
                log.info("downloading %d full texts for their references",
                         len(pmc))
                self.harvester.download(folder, ids=pmc, source="ncbi")
                counts["from_xml"] += self._refs_from_xml(folder, need)
            counts["ncbi_batch"] = self._refs_from_ncbi(need)
            counts["one_by_one"] = self._refs_one_by_one(need, crossref,
                                                         workers)
        g.resolve()
        return {**counts, **g.coverage()}

    def _refs_from_xml(self, folder, need: dict) -> int:
        """Add references from XML files for papers in ``need`` (in place)."""
        if not folder:
            return 0
        by_pmc = {r["path"].removeprefix("epmc:PMC"): pid
                  for pid, r in need.items()
                  if r["path"].startswith("epmc:PMC")}
        if not by_pmc:
            return 0
        cores = max(1, min(32, (os.cpu_count() or 2) - 1))
        n = 0
        for pmc, refs in JatsReferenceReader().read_folder(folder, cores):
            pid = by_pmc.pop(pmc, None)
            if pid is not None:
                self.graph.add_references(pid, refs, "jats", commit=False)
                need.pop(pid, None)
                n += 1
        self.lib.store.db.commit()
        return n

    def _refs_from_ncbi(self, need: dict) -> int:
        """Batch reference lists for abstract-only papers (by PMID)."""
        by_pmid = {r["path"][9:]: pid for pid, r in need.items()
                   if r["path"].startswith("abstract:")
                   and r["path"][9:].isdigit()}
        if not by_pmid:
            return 0
        log.info("NCBI reference lists for %d papers (100 per request)",
                 len(by_pmid))
        found = self.ncbi_refs.references(list(by_pmid))
        for pmid, refs in found.items():
            pid = by_pmid.get(pmid)
            if pid is not None:
                self.graph.add_references(pid, refs, "ncbi", commit=False)
                need.pop(pid, None)
        self.lib.store.db.commit()
        return len(found)

    def _refs_one_by_one(self, need: dict, crossref: bool,
                         workers: int) -> int:
        """Europe PMC / Crossref, one paper per request, several at once."""
        jobs = [(pid, self._ref_request(r, crossref))
                for pid, r in need.items()]
        jobs = [(pid, job) for pid, job in jobs if job]
        if not jobs:
            return 0
        log.info("reading %d reference lists one by one", len(jobs))
        n = 0
        with ThreadPoolExecutor(max_workers=workers) as tp:
            futs = {tp.submit(job): pid for pid, job in jobs}
            for k, f in enumerate(as_completed(futs), 1):
                refs, source = f.result()
                if refs is not None:
                    self.graph.add_references(futs[f], refs, source,
                                              commit=False)
                    n += 1
                if k % 500 == 0:
                    self.lib.store.db.commit()
                    log.info("  reference lists: %d/%d", k, len(jobs))
        self.lib.store.db.commit()
        return n

    def _ref_request(self, paper, crossref: bool):
        """Return a no-argument job giving (references, source), or None."""
        path, doi = paper["path"], paper["doi"]
        if path.startswith("epmc:PMC"):
            pmc = path.removeprefix("epmc:")
            return lambda: (self.epmc_refs.references("PMC", pmc), "epmc")
        if path.startswith("abstract:") and path[9:].isdigit():
            pmid = path[9:]
            return lambda: (self.epmc_refs.references("MED", pmid), "epmc")
        if doi and crossref:
            return lambda: (self.crossref_refs.references(doi), "crossref")
        return None

    def _ncbi_post(self, url, data):
        """POST to NCBI within its per-second limit (shared by threads)."""
        with self._ncbi_lock:
            wait = self._ncbi_gap - (time.time() - self._ncbi_last)
            if wait > 0:
                time.sleep(wait)
            self._ncbi_last = time.time()
        key = self.lib.cfg.raw.get("harvest", {}).get("ncbi_api_key", "")
        payload = {**data, "tool": "gautam",
                   "email": self.lib.cfg.email or ""}
        if key:
            payload["api_key"] = key
        for attempt in range(4):
            try:
                with scholarly._client(self.lib.cfg.email, timeout=120) as c:
                    r = c.post(url, data=payload)
                if r.status_code == 200:
                    return r
            except Exception as e:  # noqa: BLE001 - retry any network fault
                log.warning("NCBI error (%s); retry %d/4", e, attempt + 1)
            time.sleep(5 * 2 ** attempt)
        return None

    # ------------------------------------------------------ 2-4. scoring
    def score(self) -> dict:
        """Recount citation pull, look up the heaviest, score relevance."""
        s, g = self.settings, self.graph
        n_nodes = g.rebuild_nodes(s.decay, s.max_hops)
        todo = g.nodes_to_look_up(s.lookup_factor * s.budget)
        refs = [make_reference(r["doi"], r["pmid"]) for r in todo]
        found = self.records.lookup([r for r in refs if r])
        for row in todo:
            g.set_record(row["key"], found.get(row["key"]))
        self.lib.store.db.commit()
        rows = g.looked_up(need_relevance=True)
        if rows:
            texts = []
            for r in rows:
                rec = json.loads(r["record"])
                texts.append(f"{rec.get('title') or ''}. "
                             f"{rec.get('abstractText') or ''}"[:4000])
            g.set_scores([r["key"] for r in rows], self._relevance(texts),
                         "relevance")
        rows = g.looked_up()
        if rows:
            w = combined_weight([r["cite"] for r in rows],
                                [r["relevance"] for r in rows], s.beta)
            g.set_scores([r["key"] for r in rows], w, "weight")
        return {"missing_papers": n_nodes, "looked_up_now": len(found),
                "scored": len(rows)}

    def _relevance(self, texts: list[str]) -> np.ndarray:
        """Mean similarity of each text to its k closest library passages."""
        emb = self.lib.embedder.embed_docs(texts)
        _, mat = self.lib.store.matrix()
        if not len(mat):
            raise GraphError("the library has no fingerprints yet; run "
                             "`gautam index --stage embed` first")
        dev = getattr(self.lib.embedder, "_dev", "cpu")
        if dev in ("cuda", "mps"):
            try:
                return self._relevance_gpu(emb, mat, dev)
            except Exception as e:  # noqa: BLE001 - fall back to the CPU
                log.warning("GPU relevance unavailable (%s); using CPU", e)
        out = np.empty(len(emb))
        for i in range(0, len(emb), 64):
            sims = np.asarray(mat, dtype=np.float32) @ emb[i:i + 64].T
            out[i:i + 64] = topk_mean(sims.T, self.settings.knn)
        return out

    def _relevance_gpu(self, emb, mat, dev) -> np.ndarray:
        import torch

        m = torch.from_numpy(np.ascontiguousarray(mat, dtype=np.float16))
        m = m.to(dev)
        out = []
        for i in range(0, len(emb), 256):
            e = torch.from_numpy(emb[i:i + 256].astype(np.float16)).to(dev)
            top = torch.topk(e @ m.T, self.settings.knn, dim=1).values
            out.append(top.float().mean(dim=1).cpu().numpy())
        return np.concatenate(out)

    # ------------------------------------------------------ 5. adding
    def add(self, budget: int | None = None) -> dict:
        """Add the heaviest eligible papers (abstract now, full text later)."""
        s, g = self.settings, self.graph
        rows = g.looked_up()
        if not rows:
            return {"added": 0, "queued_full_text": 0}
        idx = select([r["weight"] or 0 for r in rows],
                     [r["cited_by"] for r in rows],
                     [r["relevance"] for r in rows],
                     s.min_citations, s.min_relevance, budget or s.budget)
        added = queued = 0
        db = self.lib.store.db
        for i in idx:
            row = rows[int(i)]
            rec = json.loads(row["record"])
            if self.harvester._store_abstract(rec, "graph"):
                key = f"abstract:{rec.get('pmid') or rec.get('id') or ''}"
                p = self.lib.store.paper_by_path(key)
                if p is None and rec.get("doi"):
                    p = self.lib.store.paper_by_doi(rec["doi"])
                if p is not None:
                    db.execute("UPDATE papers SET graph_hop=? WHERE id=?",
                               (row["hop"], p["id"]))
                added += 1
            if rec.get("isOpenAccess") == "Y" and rec.get("pmcid"):
                db.execute(
                    "INSERT OR IGNORE INTO candidates (pmcid, doi, title, "
                    "year, topics) VALUES (?,?,?,?,?)",
                    (rec["pmcid"].upper(), (rec.get("doi") or "").lower()
                     or None, rec.get("title"), rec.get("pubYear"), "graph"))
                queued += 1
            g.mark(row["key"], "added")
        db.commit()
        g.resolve()
        return {"added": added, "queued_full_text": queued}

    # ------------------------------------------------------ rounds
    def run(self, rounds: int = 3, xml_dir=None, online: bool = True,
            crossref: bool = True) -> list[dict]:
        """Repeat rounds until coverage gain falls below ``stop_gain``."""
        history = []
        before = self.collect_references(xml_dir, online, crossref)
        for k in range(1, rounds + 1):
            scored = self.score()
            added = self.add()
            after = self.collect_references(None, online, crossref)
            gain = after["coverage"] - before["coverage"]
            step = {"round": k, **scored, **added,
                    "coverage_before": before["coverage"],
                    "coverage_after": after["coverage"],
                    "gain": round(gain, 4)}
            log.info("round %d: %s", k, step)
            history.append(step)
            if added["added"] == 0 or gain < self.settings.stop_gain:
                break
            before = after
        return history
