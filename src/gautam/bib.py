"""Read and write BibTeX, and make stable citation keys."""
from __future__ import annotations

import json
import re
import unicodedata

STOP = {"a", "an", "the", "of", "on", "in", "and", "for", "to", "with", "from", "by", "at", "is", "are", "via"}


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def make_citekey(meta: dict, taken=lambda k: False) -> str:
    """dantu2012loop style: first author's family name + year + first real title word."""
    authors = meta.get("authors") or []
    if isinstance(authors, str):
        authors = json.loads(authors or "[]")
    fam = re.sub(r"[^a-z]", "", _ascii(authors[0].get("family", "")).lower()) if authors else ""
    words = [w for w in re.findall(r"[a-z0-9]+", _ascii(meta.get("title") or "").lower()) if w not in STOP]
    base = f"{fam or 'anon'}{meta.get('year') or 'nd'}{words[0] if words else ''}"
    key, i = base, 0
    while taken(key):
        key = base + "abcdefghijklmnopqrstuvwxyz"[i % 26] + ("" if i < 26 else str(i // 26))
        i += 1
    return key


# ---------------- parsing ----------------
def parse_bib(text: str) -> list[dict]:
    """Small, forgiving BibTeX reader. Returns [{'ENTRYTYPE','ID', field: value, ...}]."""
    entries = []
    i, n = 0, len(text)
    while True:
        m = re.compile(r"@(\w+)\s*([{(])").search(text, i)
        if not m:
            break
        etype = m.group(1).lower()
        close = "}" if m.group(2) == "{" else ")"
        j = m.end()
        depth, k = 1, j
        while k < n and depth:
            ch = text[k]
            if ch in "{(" and (ch == "{" or close == ")"):
                depth += 1
            elif ch in "})" and (ch == "}" or close == ")"):
                depth -= 1
            k += 1
        body = text[j:k - 1]
        i = k
        if etype in ("comment", "preamble", "string"):
            continue
        key, _, rest = body.partition(",")
        e = {"ENTRYTYPE": etype, "ID": key.strip()}
        e.update(_parse_fields(rest))
        entries.append(e)
    return entries


def _parse_fields(s: str) -> dict:
    out, i, n = {}, 0, len(s)
    while i < n:
        m = re.compile(r"\s*,?\s*([\w\-:.]+)\s*=\s*").match(s, i)
        if not m:
            break
        name = m.group(1).lower()
        i = m.end()
        if i >= n:
            break
        if s[i] == "{":
            depth, j = 1, i + 1
            while j < n and depth:
                depth += {"{": 1, "}": -1}.get(s[j], 0)
                j += 1
            val = s[i + 1:j - 1]
        elif s[i] == '"':
            j = i + 1
            while j < n and not (s[j] == '"' and s[j - 1] != "\\"):
                j += 1
            val, j = s[i + 1:j], j + 1
        else:
            m2 = re.compile(r"[^,\s]+").match(s, i)
            val, j = (m2.group(0), m2.end()) if m2 else ("", i + 1)
        out[name] = re.sub(r"\s+", " ", val).strip()
        i = j
    return out


def bib_title(e: dict) -> str:
    return re.sub(r"[{}]", "", e.get("title", "")).strip()


def bib_first_author_family(e: dict) -> str:
    a = re.sub(r"[{}]", "", e.get("author", "")).split(" and ")[0].strip()
    if "," in a:
        return a.split(",")[0].strip()
    return a.split()[-1] if a else ""


# ---------------- writing ----------------
TYPE_MAP = {"journal-article": "article", "proceedings-article": "inproceedings", "book-chapter": "incollection",
            "book": "book", "posted-content": "misc", "dissertation": "phdthesis", "report": "techreport"}
_ESC = {"&": r"\&", "%": r"\%", "#": r"\#", "_": r"\_"}


def _esc(s: str) -> str:
    return "".join(_ESC.get(c, c) for c in str(s))


def to_bibtex(p: dict) -> str:
    authors = p.get("authors") or []
    if isinstance(authors, str):
        authors = json.loads(authors or "[]")
    etype = TYPE_MAP.get(p.get("type") or "", "article" if p.get("journal") else "misc")
    fields = [
        ("author", " and ".join(f"{a.get('family', '')}, {a.get('given', '')}".strip(", ") for a in authors)),
        ("title", "{" + _esc(p.get("title") or "") + "}"),
        ("journal" if etype == "article" else "booktitle" if etype in ("inproceedings", "incollection") else
         "howpublished", p.get("journal")),
        ("year", p.get("year")),
        ("volume", p.get("volume")),
        ("number", p.get("issue")),
        ("pages", (p.get("pages") or "").replace("-", "--") if p.get("pages") else None),
        ("publisher", p.get("publisher") if etype in ("book", "incollection") else None),
        ("doi", p.get("doi")),
        ("note", "; ".join(x for x in ("Preprint" if p.get("type") == "posted-content" else "",
                                        "RETRACTED -- check before citing" if p.get("retracted") else "") if x)),
    ]
    lines = [f"@{etype}{{{p.get('citekey')},"]
    for k, v in fields:
        if v not in (None, "", "{}"):
            v = str(v)
            lines.append(f"  {k} = {v if v.startswith('{') else '{' + _esc(v) + '}'},")
    lines.append("}")
    return "\n".join(lines)
