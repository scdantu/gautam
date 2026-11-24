"""Load settings from gautam.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _expand(p: str, base: Path) -> Path:
    q = Path(os.path.expanduser(p))
    return q if q.is_absolute() else (base / q)


@dataclass
class Config:
    root: Path
    library_dirs: list[Path]
    inbox: Path
    data_dir: Path
    backend: str = "sentence-transformers"
    model: str = "Qwen/Qwen3-Embedding-0.6B"
    dim: int = 512
    device: str = "auto"
    batch_size: int = 8
    email: str = ""
    proxy_prefix: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gautam.sqlite"

    @property
    def summaries_dir(self) -> Path:
        return self.data_dir / "summaries"

    @property
    def bib_dir(self) -> Path:
        return self.data_dir / "bib"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    def all_dirs(self) -> list[Path]:
        return [*self.library_dirs, self.inbox]

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.inbox, self.summaries_dir, self.bib_dir, self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("GAUTAM_CONFIG") or PROJECT_ROOT / "gautam.toml")
    raw: dict = {}
    if path.exists():
        raw = tomllib.loads(path.read_text())
    base = path.parent
    lib = raw.get("library", {})
    emb = raw.get("embedding", {})
    cfg = Config(
        root=base,
        library_dirs=[_expand(d, base) for d in lib.get("dirs", [])],
        inbox=_expand(lib.get("inbox", "inbox"), base),
        data_dir=_expand(raw.get("data", {}).get("dir", "data"), base),
        backend=emb.get("backend", "sentence-transformers"),
        model=emb.get("model", "Qwen/Qwen3-Embedding-0.6B"),
        dim=int(emb.get("dim", 512)),
        device=emb.get("device", "auto"),
        batch_size=int(emb.get("batch_size", 8)),
        email=raw.get("contact", {}).get("email", ""),
        proxy_prefix=raw.get("library_proxy", {}).get("prefix", ""),
        raw=raw,
    )
    cfg.ensure_dirs()
    return cfg
