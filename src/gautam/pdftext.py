"""Read text out of PDFs, paragraph by paragraph, and cut it into search-sized chunks."""
from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

try:  # keep MuPDF's warnings off stdout (stdout is the channel Claude talks to the MCP server on)
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
except Exception:
    pass

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)
REF_HEAD_RE = re.compile(r"^\s*(references|bibliography|literature cited|references and notes)\s*$", re.I)


@dataclass
class Block:
    page: int  # 1-based
    text: str


@dataclass
class Chunk:
    ord: int
    page: int
    section: str
    text: str


@dataclass
class PdfContent:
    blocks: list[Block]
    first_pages: str
    meta_title: str
    n_pages: int

    @property
    def fulltext(self) -> str:
        return "\n\n".join(b.text for b in self.blocks)


def _clean(t: str) -> str:
    t = t.replace("­", "")
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)  # re-join hyphenated line breaks
    t = re.sub(r"\s*\n\s*", " ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _reading_order(raw: list, width: float) -> list:
    """Two-column pages: read the left column, then the right, inside each band between full-width blocks.
    (Sorting purely top-to-bottom interleaves the two columns line by line.)"""
    spans, cols = [], []
    for b in raw:
        (spans if (b[2] - b[0]) > 0.6 * width else cols).append(b)
    spans.sort(key=lambda b: b[1])
    regions: list[list[list]] = [[[], []] for _ in range(len(spans) + 1)]
    for b in cols:
        r = next((i for i, s in enumerate(spans) if s[1] >= b[1]), len(spans))
        regions[r][0 if (b[0] + b[2]) / 2 < width / 2 else 1].append(b)
    out = []
    for i, (left, right) in enumerate(regions):
        out += sorted(left, key=lambda b: b[1]) + sorted(right, key=lambda b: b[1])
        if i < len(spans):
            out.append(spans[i])
    return out


def _title_from_fonts(page) -> str:
    """Guess the title as the largest text in the top part of page 1."""
    try:
        spans = [s for blk in page.get_text("dict")["blocks"] for ln in blk.get("lines", [])
                 for s in ln["spans"] if len(s["text"].strip()) > 1 and s["bbox"][1] < page.rect.height * 0.6]
    except Exception:
        return ""
    if not spans:
        return ""
    big = max(s["size"] for s in spans)
    t = " ".join(s["text"].strip() for s in spans if s["size"] >= big - 0.5)
    t = re.sub(r"\s+", " ", t).strip()
    return t if 15 <= len(t) <= 300 else ""


BAD_TITLE = re.compile(r"^(microsoft word|untitled|title|doi|\d)|\.(docx?|pdf|tex)$|moesm|^[\w.\-]+$", re.I)


def good_title(t: str) -> bool:
    return bool(t) and len(t) >= 15 and not BAD_TITLE.search(t.strip())


def read_pdf(path) -> PdfContent:
    doc = pymupdf.open(path)
    blocks: list[Block] = []
    first = []
    font_title = ""
    try:
        for i, page in enumerate(doc):
            if i < 2:
                first.append(page.get_text("text"))
            if i == 0:
                font_title = _title_from_fonts(page)
            raw = [b for b in page.get_text("blocks") if b[6] == 0]  # 0 = text block, 1 = image
            for b in _reading_order(raw, page.rect.width):
                txt = _clean(b[4])
                if len(txt) < 3:
                    continue
                blocks.append(Block(page=i + 1, text=txt))
        meta_title = ((doc.metadata or {}).get("title") or "").strip()
        n_pages = doc.page_count
    finally:
        doc.close()
    blocks = _drop_repeated_lines(blocks, n_pages)
    blocks = _drop_reference_list(blocks)
    blocks = [b for b in blocks if not _is_debris(b.text) and not BOILER.match(b.text)]
    title = meta_title if good_title(meta_title) else font_title
    return PdfContent(blocks, "\n".join(first), title, n_pages)


BOILER = re.compile(r"^\s*(acknowledg|author contributions|competing (financial )?interests|conflicts? of interest|"
                    r"reprints and permissions|correspondence and requests|reporting summary|publisher.s note|"
                    r"open access this article|additional information|supplementary information is available|"
                    r"peer review information|©|copyright ©)", re.I)


def _is_debris(t: str) -> bool:
    """Axis labels, table cells, equation fragments: few real words."""
    toks = t.split()
    if len(toks) < 5:
        return False
    words = sum(1 for w in toks if sum(c.isalpha() for c in w) >= 3)
    return words / len(toks) < 0.4


_REF_PAT = re.compile(r"\b(19|20)\d{2}[a-z]?\b|\bet al\b|\bdoi\b|https?://|\b\d+\s*[:,]\s*\d+\s*[–-]\s*\d+")


def _ref_like(t: str) -> bool:
    """Looks like part of a reference list: dense with years, 'et al', DOIs, volume:pages."""
    n = len(t.split())
    if n < 8:
        return False
    hits = len(_REF_PAT.findall(t))
    numbered = len(re.findall(r"(^|\s)\d{1,3}\.\s+[A-Z][a-zA-Z\-]+,?\s+[A-Z]\.", t))
    return hits / n > 0.08 and hits >= 3 or numbered >= 2


def _drop_repeated_lines(blocks: list[Block], n_pages: int) -> list[Block]:
    """Running headers/footers (journal name, page furniture) repeat on many pages: drop them."""
    if n_pages < 4:
        return blocks
    counts: dict[str, int] = {}
    for b in blocks:
        if len(b.text) < 120:
            key = re.sub(r"\d+", "#", b.text.lower())
            counts[key] = counts.get(key, 0) + 1
    limit = max(3, n_pages // 2)
    return [b for b in blocks if len(b.text) >= 120 or counts.get(re.sub(r"\d+", "#", b.text.lower()), 0) < limit]


def _drop_reference_list(blocks: list[Block]) -> list[Block]:
    """Remove reference lists, which pollute search: cut at a References heading in the back half, and drop
    any other block in the back 60% that reads like references (many journals print no heading)."""
    cut = None
    for i, b in enumerate(blocks):
        if REF_HEAD_RE.match(b.text) and i > len(blocks) * 0.5:
            cut = i
    if cut is not None:
        blocks = blocks[:cut]
    start = int(len(blocks) * 0.4)
    return [b for i, b in enumerate(blocks) if i < start or not _ref_like(b.text)]


ARXIV_RE = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})(v\d+)?", re.I)
SUPP_NAME = re.compile(r"(moesm|_esm\b|\.sapp|[_\-\s]si[_\-.]|supplement|supporting)", re.I)


def find_arxiv(text: str, filename: str = "") -> str | None:
    m = ARXIV_RE.search(text) or re.match(r"^(\d{4}\.\d{4,5})(v\d+)?\.pdf$", filename)
    return m.group(1) if m else None


def is_supplement(text: str, filename: str) -> bool:
    head = text[:600].lower()
    return bool(SUPP_NAME.search(filename)) or "supplementary information" in head[:200] or \
        "supporting information" in head[:200] or head.lstrip().startswith(("supplementary", "supporting"))


def find_doi(text: str) -> str | None:
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u2010", "-")
    for m in DOI_RE.finditer(text):
        doi = m.group(1).rstrip(".,;)]}'")
        if doi.lower().endswith((".pdf", ".html")):
            doi = doi.rsplit(".", 1)[0]
        return doi.lower()
    return None


def extract_job(path: str) -> dict:
    """Run in a worker process: hash, read and chunk one PDF. Only plain data goes back."""
    import hashlib
    import os
    import sys
    import time

    sys.stdout = sys.stderr  # worker processes must never write to the MCP channel

    t0 = time.time()
    try:
        st = os.stat(path)
        h = hashlib.sha1()
        with open(path, "rb") as f:  # reading also downloads a cloud-only OneDrive file
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        content = read_pdf(path)
        chunks = make_chunks(content.blocks)
        return {"path": path, "ok": True, "sha": h.hexdigest(), "size": st.st_size, "mtime": st.st_mtime,
                "content": content, "chunks": chunks, "seconds": time.time() - t0}
    except Exception as e:
        return {"path": path, "ok": False, "error": f"{type(e).__name__}: {e}", "seconds": time.time() - t0}


def _looks_like_heading(t: str) -> bool:
    words = t.split()
    return 0 < len(words) <= 10 and not t.endswith((".", ",", ";", ":")) and t[0].isalpha() and len(t) < 90


def make_chunks(blocks: list[Block], target_words: int = 220, max_words: int = 380, overlap_words: int = 40) -> list[Chunk]:
    """Group paragraphs into ~1-2 paragraph chunks; each chunk remembers its page and nearest heading."""
    chunks: list[Chunk] = []
    section = ""
    words: list[str] = []  # words in the current chunk (may start with overlap from the previous one)
    new = 0  # how many of them are new (not overlap)
    page = 1

    def flush(keep_overlap: bool = True):
        nonlocal words, new
        if new > 0 and len(words) >= 15:
            chunks.append(Chunk(len(chunks), page, section, " ".join(words)))
        words = words[-overlap_words:] if (keep_overlap and new > 0 and overlap_words) else []
        new = 0

    for b in blocks:
        if _looks_like_heading(b.text):
            flush(keep_overlap=False)
            section = b.text
            continue
        w = b.text.split()
        if new > 0 and new + len(w) > max_words:
            flush()
        if new == 0:
            page = b.page
        while len(w) > max_words:  # very long paragraph: split it
            words += w[:max_words]
            new += max_words
            w = w[max_words:]
            flush()
            page = b.page
        words += w
        new += len(w)
        if new >= target_words:
            flush()
    flush(keep_overlap=False)
    return chunks
