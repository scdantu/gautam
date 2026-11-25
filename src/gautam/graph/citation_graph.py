"""The citation graph, stored next to the library: who cites what."""

import json
import time

import numpy as np

from gautam.io.references import Reference
from gautam.numerics.graph_weights import citation_weight, coverage

SCHEMA = """
CREATE TABLE IF NOT EXISTS refs (
    citing_id INTEGER,      -- library paper that cites
    ref_key TEXT,           -- doi:... or pmid:...
    ref_doi TEXT,
    ref_pmid TEXT,
    resolved_id INTEGER,    -- library paper it points to, once we hold it
    PRIMARY KEY (citing_id, ref_key)
);
CREATE INDEX IF NOT EXISTS refs_key ON refs(ref_key);
CREATE INDEX IF NOT EXISTS refs_doi ON refs(ref_doi);
CREATE INDEX IF NOT EXISTS refs_resolved ON refs(resolved_id);
CREATE TABLE IF NOT EXISTS ref_sources (
    paper_id INTEGER PRIMARY KEY, source TEXT, n INTEGER, at REAL
);
CREATE TABLE IF NOT EXISTS graph_nodes (
    key TEXT PRIMARY KEY,   -- a cited paper not (yet) in the library
    doi TEXT, pmid TEXT,
    cited_by INTEGER, cite REAL, hop INTEGER,
    relevance REAL, weight REAL,
    status TEXT DEFAULT 'candidate',  -- candidate|looked_up|not_found|added
    record TEXT             -- Europe PMC record (JSON) once looked up
);
CREATE INDEX IF NOT EXISTS nodes_status ON graph_nodes(status);
"""


class CitationGraph:
    """Reference lists of library papers and the cited papers still missing."""

    def __init__(self, store):
        """Attach to a library Store (same SQLite file)."""
        self.db = store.db
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(papers)")}
        if "graph_hop" not in cols:
            self.db.execute(
                "ALTER TABLE papers ADD COLUMN graph_hop INTEGER DEFAULT 0")
        self.db.commit()

    # -------------------------------------------------------------- edges
    def add_references(self, paper_id: int, refs: list[Reference],
                       source: str, commit: bool = True) -> int:
        """Record one paper's reference list (and that it has been read)."""
        rows = [(paper_id, r.key, r.doi, r.pmid) for r in refs]
        self.db.executemany(
            "INSERT OR IGNORE INTO refs (citing_id, ref_key, ref_doi, "
            "ref_pmid) VALUES (?,?,?,?)", rows)
        self.db.execute(
            "INSERT OR REPLACE INTO ref_sources VALUES (?,?,?,?)",
            (paper_id, source, len(rows), time.time()))
        if commit:
            self.db.commit()
        return len(rows)

    def papers_needing_references(self, seed_abstracts: bool = False):
        """Library papers whose reference list has not been read yet.

        Abstract-only papers from the keyword harvest (hop 0) are skipped
        unless ``seed_abstracts``: there can be hundreds of thousands, and
        the full-text papers already carry the field's reference lists.
        """
        q = ("SELECT id, path, doi FROM papers WHERE id NOT IN "
             "(SELECT paper_id FROM ref_sources)")
        if not seed_abstracts:
            q += (" AND NOT (coalesce(type, '') = 'abstract' "
                  "AND coalesce(graph_hop, 0) = 0)")
        return self.db.execute(q).fetchall()

    def resolve(self) -> None:
        """Point references at library papers we now hold (by DOI or PMID)."""
        self.db.execute(
            "UPDATE refs SET ref_doi = (SELECT lower(json_extract("
            "n.record, '$.doi')) FROM graph_nodes n WHERE n.key = "
            "refs.ref_key) WHERE ref_doi IS NULL")
        self.db.execute(
            "UPDATE refs SET resolved_id = (SELECT p.id FROM papers p "
            "WHERE p.doi = refs.ref_doi) WHERE resolved_id IS NULL "
            "AND ref_doi IS NOT NULL")
        self.db.execute(
            "UPDATE refs SET resolved_id = (SELECT p.id FROM papers p "
            "WHERE p.path = 'abstract:' || refs.ref_pmid) "
            "WHERE resolved_id IS NULL AND ref_pmid IS NOT NULL")
        self.db.commit()

    def coverage(self) -> dict:
        """How many references point to papers already in the library."""
        total, done = self.db.execute(
            "SELECT count(*), count(resolved_id) FROM refs").fetchone()
        papers = self.db.execute(
            "SELECT count(*) FROM ref_sources").fetchone()[0]
        return {"references": total, "in_library": done,
                "coverage": round(coverage(done, total), 4),
                "papers_with_reference_lists": papers}

    # -------------------------------------------------------------- nodes
    def rebuild_nodes(self, decay: float, max_hops: int) -> int:
        """Recount who cites each missing paper, and how strongly."""
        rows = self.db.execute(
            "SELECT r.ref_key, r.ref_doi, r.ref_pmid, "
            "coalesce(p.graph_hop, 0) FROM refs r "
            "JOIN papers p ON p.id = r.citing_id "
            "WHERE r.resolved_id IS NULL AND coalesce(p.graph_hop, 0) < ?",
            (max_hops,)).fetchall()
        if not rows:
            return 0
        keys = sorted({r[0] for r in rows})
        index = {k: i for i, k in enumerate(keys)}
        target = np.array([index[r[0]] for r in rows])
        hops = np.array([r[3] for r in rows])
        cite = citation_weight(target, hops, len(keys), decay)
        cited_by = np.bincount(target, minlength=len(keys))
        min_hop = np.full(len(keys), 10 ** 6)
        np.minimum.at(min_hop, target, hops)
        ids = {r[0]: (r[1], r[2]) for r in rows}
        self.db.executemany(
            "INSERT INTO graph_nodes (key, doi, pmid, cited_by, cite, hop) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "cited_by=excluded.cited_by, cite=excluded.cite, "
            "hop=excluded.hop",
            [(k, ids[k][0], ids[k][1], int(cited_by[i]), float(cite[i]),
              int(min_hop[i]) + 1) for i, k in enumerate(keys)])
        self.db.commit()
        return len(keys)

    def nodes_to_look_up(self, limit: int):
        """Heaviest-by-citation candidates without a record yet."""
        return self.db.execute(
            "SELECT key, doi, pmid FROM graph_nodes WHERE status="
            "'candidate' ORDER BY cite DESC LIMIT ?", (limit,)).fetchall()

    def set_record(self, key: str, record: dict | None) -> None:
        """Store a looked-up record, or mark the paper as not found."""
        if record is None:
            self.db.execute("UPDATE graph_nodes SET status='not_found' "
                            "WHERE key=?", (key,))
        else:
            self.db.execute("UPDATE graph_nodes SET status='looked_up', "
                            "record=? WHERE key=?", (json.dumps(record), key))

    def looked_up(self, need_relevance: bool = False):
        """Nodes with a record, optionally only those not yet scored."""
        q = "SELECT * FROM graph_nodes WHERE status='looked_up'"
        if need_relevance:
            q += " AND relevance IS NULL"
        return self.db.execute(q).fetchall()

    def set_scores(self, keys, values, column: str) -> None:
        """Write relevance or weight values for many nodes."""
        if column not in ("relevance", "weight"):
            raise ValueError(f"unknown score column {column!r}")
        self.db.executemany(f"UPDATE graph_nodes SET {column}=? WHERE key=?",
                            [(float(v), k)
                             for k, v in zip(keys, values, strict=True)])
        self.db.commit()

    def mark(self, key: str, status: str) -> None:
        """Change a node's status (e.g. to 'added')."""
        self.db.execute("UPDATE graph_nodes SET status=? WHERE key=?",
                        (status, key))

    def top(self, n: int = 50):
        """Heaviest looked-up papers not yet added, for inspection."""
        return self.db.execute(
            "SELECT key, cited_by, hop, relevance, weight, "
            "json_extract(record, '$.title') AS title, "
            "json_extract(record, '$.pubYear') AS year FROM graph_nodes "
            "WHERE status='looked_up' ORDER BY weight DESC LIMIT ?",
            (n,)).fetchall()
