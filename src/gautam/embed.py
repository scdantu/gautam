"""Meaning fingerprints (embeddings). The model loads only when first needed."""
from __future__ import annotations

import hashlib
import re
import sys

import numpy as np

QUERY_PROMPT = ("Instruct: Given a question about scientific research, retrieve passages from research papers "
                "that answer it\nQuery: ")


def _norm(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.maximum(n, 1e-12)


class STEmbedder:
    """Qwen3-Embedding (or any sentence-transformers model) on the Mac GPU when available."""

    def __init__(self, model: str, dim: int, device: str = "auto", batch_size: int = 16):
        self.model_name, self.dim, self.device, self.batch_size = model, dim, device, batch_size
        self._m = None

    def _model(self):
        if self._m is None:
            import torch
            from sentence_transformers import SentenceTransformer

            dev = self.device
            if dev == "auto":
                dev = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
            print(f"[gautam] loading {self.model_name} on {dev} (first time downloads ~1.2 GB)", file=sys.stderr)
            kw = {"torch_dtype": torch.float16} if dev in ("mps", "cuda") else {}
            try:  # newer sentence-transformers renamed tokenizer_kwargs -> processor_kwargs
                self._m = SentenceTransformer(self.model_name, device=dev, model_kwargs=kw,
                                              processor_kwargs={"padding_side": "left"})
            except TypeError:
                self._m = SentenceTransformer(self.model_name, device=dev, model_kwargs=kw,
                                              tokenizer_kwargs={"padding_side": "left"})
            # passages are ~250 words (~400 tokens); a lower cap keeps GPU memory small
            self._m.max_seq_length = 512
            self._dev = dev
        return self._m

    def _free(self) -> None:
        try:
            import torch
            if self._dev == "mps":
                torch.mps.empty_cache()
            elif self._dev == "cuda":
                torch.cuda.empty_cache()
        except Exception:
            pass

    def _encode(self, texts, prompt=None) -> np.ndarray:
        """Encode in small slices, emptying the GPU cache after each; on 'out of memory' halve the batch and retry."""
        m = self._model()
        texts = list(texts)
        out, i, bs = [], 0, self.batch_size
        slice_n = max(64, bs * 4)
        while i < len(texts):
            part = texts[i:i + slice_n]
            try:
                v = m.encode(part, batch_size=bs, prompt=prompt, convert_to_numpy=True, show_progress_bar=False)
            except RuntimeError as e:
                if "out of memory" not in str(e).lower():
                    raise
                self._free()
                if bs > 1:
                    bs = max(1, bs // 2)
                    slice_n = max(8, slice_n // 2)
                    print(f"[gautam] GPU memory full: retrying with batch size {bs}", file=sys.stderr)
                    continue
                if self._dev != "cpu":
                    print("[gautam] GPU memory still full: switching to CPU (slower, but works)", file=sys.stderr)
                    m.to("cpu")
                    self._dev = "cpu"
                    m = self._m
                    continue
                raise
            out.append(np.asarray(v, dtype=np.float32)[:, : self.dim])
            i += len(part)
            self._free()
        return _norm(np.vstack(out))  # Qwen3 is trained so the first `dim` numbers still work on their own

    def embed_docs(self, texts) -> np.ndarray:
        return self._encode(texts)

    def embed_query(self, text: str) -> np.ndarray:
        return self._encode([text], prompt=QUERY_PROMPT)[0]


class HashEmbedder:
    """Offline stand-in for tests: word-hashing, no model download. Not for real use."""

    def __init__(self, dim: int = 256, **_):
        self.dim = dim

    def _one(self, t: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for w in re.findall(r"[a-z0-9]+", t.lower()):
            h = int(hashlib.md5(w.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 64) & 1 else -1.0
        return v

    def embed_docs(self, texts) -> np.ndarray:
        return _norm(np.vstack([self._one(t) for t in texts])) if texts else np.zeros((0, self.dim), np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return _norm(self._one(text))


def make_embedder(cfg):
    if cfg.backend == "hash":
        return HashEmbedder(dim=cfg.dim)
    return STEmbedder(cfg.model, cfg.dim, cfg.device, cfg.batch_size)
