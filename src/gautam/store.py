"""The library database: one SQLite file holding papers, chunks, fingerprints and keyword index."""
from __future__ import annotations

import json
import sqlite3
import time
import zlib
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE,
    sha1 TEXT,
    size INTEGER,
    mtime REAL,
    doi TEXT,
    title TEXT,
    authors TEXT,          -- JSON list of {"given":..,"family":..}
    year INTEGER,
    journal TEXT,
    volume TEXT, issue TEXT, pages TEXT, publisher TEXT, type TEXT,
    citekey TEXT UNIQUE,
    pdf_title TEXT,        -- title found inside the PDF (used for Crossref title search)
    stage TEXT DEFAULT 'done',  -- read -> meta -> done (fingerprinted)
    meta_source TEXT,      -- crossref | pdf | none
    retracted INTEGER DEFAULT 0,
    n_pages INTEGER,
    n_chunks INTEGER,
    fulltext BLOB,         -- zlib-compressed text, so summaries need no PDF
    added REAL
);
CREATE INDEX IF NOT EXISTS papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS papers_sha1 ON papers(sha1);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER REFERENCES papers(id) ON DELETE CASCADE,
    ord INTEGER,
    page INTEGER,
    section TEXT,
    text TEXT,
    emb BLOB               -- float16 vector
);
CREATE INDEX IF NOT EXISTS chunks_paper ON chunks(paper_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, section, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, section) VALUES (new.id, new.text, new.section);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, section) VALUES ('delete', old.id, old.text, old.section);
END;

CREATE TABLE IF NOT EXISTS skipped (   -- duplicate PDFs: remembered so they are not re-read every run
    path TEXT PRIMARY KEY, size INTEGER, mtime REAL, reason TEXT
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

PAPER_FIELDS = ("doi", "title", "authors", "year", "journal", "volume", "issue", "pages",
                "publisher", "type", "citekey", "meta_source", "retracted", "pdf_title", "stage")


class Store:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        # WAL is fastest on a laptop disk; shared cluster filesystems (Lustre) want the plain journal
        import os
        self.db.execute("PRAGMA journal_mode=" + ("DELETE" if os.environ.get("GAUTAM_NO_WAL") else "WAL"))
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(papers)")}
        for c, ddl in (("pdf_title", "TEXT"), ("stage", "TEXT DEFAULT 'done'")):
            if c not in cols:  # upgrade an index made by an earlier version
                self.db.execute(f"ALTER TABLE papers ADD COLUMN {c} {ddl}")
        self.db.commit()
        self._mat: np.ndarray | None = None
        self._mat_ids: np.ndarray | None = None
        self._mat_version = None

    # ---------- settings ----------
    def get_setting(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_setting(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()

    # ---------- papers ----------
    def paper_by_path(self, path: str):
        return self.db.execute("SELECT * FROM papers WHERE path=?", (path,)).fetchone()

    def paper_by_sha1(self, sha1: str):
        return self.db.execute("SELECT * FROM papers WHERE sha1=?", (sha1,)).fetchone()

    def paper_by_doi(self, doi: str):
        return self.db.execute("SELECT * FROM papers WHERE lower(doi)=lower(?)", (doi,)).fetchone()

    def has_fulltext(self, doi: str) -> bool:
        """True if this DOI is in the library with more than its abstract."""
        r = self.paper_by_doi(doi)
        return r is not None and (r["type"] or "") != "abstract"

    def find_paper(self, ref: str | int):
        """Find by id, citekey, DOI or (last resort) title words."""
        ref = str(ref).strip()
        if ref.isdigit():
            r = self.db.execute("SELECT * FROM papers WHERE id=?", (int(ref),)).fetchone()
            if r:
                return r
        r = self.db.execute("SELECT * FROM papers WHERE citekey=?", (ref,)).fetchone()
        if r:
            return r
        doi = ref.lower().removeprefix("https://doi.org/").removeprefix("doi:")
        r = self.paper_by_doi(doi)
        if r:
            return r
        return self.db.execute("SELECT * FROM papers WHERE title LIKE ? ORDER BY year DESC",
                               (f"%{ref}%",)).fetchone()

    def all_papers(self):
        return self.db.execute("SELECT * FROM papers ORDER BY year DESC, citekey").fetchall()

    def citekey_taken(self, key: str, exclude_id: int | None = None) -> bool:
        r = self.db.execute("SELECT id FROM papers WHERE citekey=?", (key,)).fetchone()
        return bool(r) and r[0] != exclude_id

    def add_paper(self, path: str, sha1: str, size: int, mtime: float, meta: dict,
                  chunks: list, embs: np.ndarray, fulltext: str, n_pages: int, commit: bool = True) -> int:
        cols = {k: meta.get(k) for k in PAPER_FIELDS}
        if isinstance(cols.get("authors"), list):
            cols["authors"] = json.dumps(cols["authors"])
        cur = self.db.execute(
            f"INSERT INTO papers (path, sha1, size, mtime, n_pages, n_chunks, fulltext, added, {', '.join(cols)}) "
            f"VALUES (?,?,?,?,?,?,?,?,{','.join('?' * len(cols))})",
            (path, sha1, size, mtime, n_pages, len(chunks), zlib.compress(fulltext.encode()), time.time(),
             *cols.values()),
        )
        pid = cur.lastrowid
        e16 = embs.astype(np.float16) if embs is not None else None
        self.db.executemany(
            "INSERT INTO chunks (paper_id, ord, page, section, text, emb) VALUES (?,?,?,?,?,?)",
            [(pid, c.ord, c.page, c.section, c.text, e16[i].tobytes() if e16 is not None else None)
             for i, c in enumerate(chunks)],
        )
        if commit:
            self.db.commit()
        self._mat_version = None
        return pid

    def update_meta(self, pid: int, meta: dict) -> None:
        cols = {k: meta[k] for k in PAPER_FIELDS if k in meta}
        if isinstance(cols.get("authors"), list):
            cols["authors"] = json.dumps(cols["authors"])
        if not cols:
            return
        self.db.execute(f"UPDATE papers SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), pid))
        self.db.commit()

    def move_paper(self, pid: int, path: str, size: int, mtime: float) -> None:
        self.db.execute("UPDATE papers SET path=?, size=?, mtime=? WHERE id=?", (path, size, mtime, pid))
        self.db.commit()

    def delete_paper(self, pid: int) -> None:
        self.db.execute("DELETE FROM chunks WHERE paper_id=?", (pid,))
        self.db.execute("DELETE FROM papers WHERE id=?", (pid,))
        self.db.commit()
        self._mat_version = None

    def papers_in_stage(self, stage: str):
        return self.db.execute("SELECT * FROM papers WHERE stage=?", (stage,)).fetchall()

    def count_unembedded(self) -> int:
        return self.db.execute("SELECT count(*) FROM chunks WHERE emb IS NULL").fetchone()[0]

    def unembedded_chunks(self, limit: int):
        return self.db.execute(
            "SELECT c.id, c.section, c.text, p.title FROM chunks c JOIN papers p ON p.id=c.paper_id "
            "WHERE c.emb IS NULL ORDER BY c.id LIMIT ?", (limit,)).fetchall()

    def set_embeddings(self, ids: list[int], embs) -> None:
        e16 = np.asarray(embs).astype(np.float16)
        self.db.executemany("UPDATE chunks SET emb=? WHERE id=?", [(e16[i].tobytes(), cid) for i, cid in enumerate(ids)])
        self.db.commit()

    def mark_embedded_papers_done(self) -> None:
        self.db.execute("UPDATE papers SET stage='done' WHERE stage!='done' AND id NOT IN "
                        "(SELECT paper_id FROM chunks WHERE emb IS NULL)")
        self.db.commit()

    def skip_file(self, path: str, size: int, mtime: float, reason: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO skipped VALUES (?,?,?,?)", (path, size, mtime, reason))
        self.db.commit()

    def is_skipped(self, path: str, size: int, mtime: float) -> bool:
        r = self.db.execute("SELECT size, mtime FROM skipped WHERE path=?", (path,)).fetchone()
        return bool(r) and r[0] == size and abs((r[1] or 0) - mtime) <= 1

    def fulltext(self, pid: int) -> str:
        r = self.db.execute("SELECT fulltext FROM papers WHERE id=?", (pid,)).fetchone()
        return zlib.decompress(r[0]).decode() if r and r[0] else ""

    def stats(self) -> dict:
        n_p = self.db.execute("SELECT count(*) FROM papers").fetchone()[0]
        n_c = self.db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        n_doi = self.db.execute("SELECT count(*) FROM papers WHERE doi IS NOT NULL").fetchone()[0]
        n_cr = self.db.execute("SELECT count(*) FROM papers WHERE meta_source='crossref'").fetchone()[0]
        n_ret = self.db.execute("SELECT count(*) FROM papers WHERE retracted=1").fetchone()[0]
        n_unemb = self.count_unembedded()
        size_mb = self.path.stat().st_size / 1e6 if self.path.exists() else 0
        return {"papers": n_p, "chunks": n_c, "with_doi": n_doi, "crossref_checked": n_cr,
                "retracted": n_ret, "passages_waiting_for_fingerprint": n_unemb, "db_size_mb": round(size_mb, 1)}

    # ---------- vectors ----------
    def matrix(self) -> tuple[np.ndarray, np.ndarray]:
        """All fingerprints as one float16 matrix. Cached in memory, and on disk next to the database
        (vectors.npy, opened without loading it all), so even millions of passages open in seconds."""
        version = tuple(self.db.execute("SELECT count(*), count(emb), coalesce(max(id),0) FROM chunks").fetchone())
        if self._mat is not None and self._mat_version == version:
            return self._mat_ids, self._mat
        vec, idf, stamp = (self.path.with_name("vectors.npy"), self.path.with_name("vector_ids.npy"),
                           self.path.with_name("vectors.version"))
        try:
            if stamp.exists() and stamp.read_text() == repr(version):
                self._mat = np.load(vec, mmap_mode="r")
                self._mat_ids = np.load(idf)
                self._mat_version = version
                return self._mat_ids, self._mat
        except Exception:
            pass
        ids, parts = [], []
        for cid, emb in self.db.execute("SELECT id, emb FROM chunks WHERE emb IS NOT NULL ORDER BY id"):
            ids.append(cid)
            parts.append(np.frombuffer(emb, dtype=np.float16))
        self._mat_ids = np.array(ids, dtype=np.int64)
        self._mat = np.vstack(parts) if parts else np.zeros((0, 1), dtype=np.float16)
        try:
            np.save(vec, self._mat)
            np.save(idf, self._mat_ids)
            stamp.write_text(repr(version))
        except OSError:
            pass
        self._mat_version = version
        return self._mat_ids, self._mat

    def chunk_rows(self, ids: list[int]):
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        rows = self.db.execute(
            f"SELECT c.id, c.paper_id, c.page, c.section, c.text, p.citekey, p.title, p.year, p.doi, p.journal, "
            f"p.path, p.retracted, p.type FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE c.id IN ({q})", ids
        ).fetchall()
        return {r["id"]: r for r in rows}

    def fts(self, query: str, limit: int = 50) -> list[int]:
        try:
            rows = self.db.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [r[0] for r in rows]
