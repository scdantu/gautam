"""Weights for the citation-graph search (best-first, like PageRank).

For a paper p that the library cites but does not hold:

    cite(p)   = sum over library papers q citing p of  decay ** hop(q)
    weight(p) = relevance(p) ** beta  *  (cite(p) / max cite) ** (1 - beta)

hop(q) is how many graph steps q is from the seed library (0 for seeds), so
citations from papers far from your own collection count for less. beta = 0.5
gives relevance and citations an equal say.
"""

import numpy as np


def citation_weight(target, citer_hop, n_targets, decay):
    """Sum ``decay ** hop`` over the citations of each target paper.

    Parameters
    ----------
    target : array of int, shape (E,)
        Index of the cited paper for each citation (edge).
    citer_hop : array of int, shape (E,)
        Graph steps of the citing paper from the seed library.
    n_targets : int
        Number of distinct cited papers.
    decay : float
        Down-weighting per step, in (0, 1].

    Returns
    -------
    numpy.ndarray, shape (n_targets,)

    """
    w = np.power(float(decay), np.asarray(citer_hop, dtype=float))
    return np.bincount(
        np.asarray(target, dtype=np.int64), weights=w, minlength=n_targets
    )


def combined_weight(cite, relevance, beta):
    """Combine citation pull and topical relevance into one weight in [0, 1].

    ``cite`` is scaled by its maximum; ``relevance`` is clipped to [0, 1];
    missing relevance (NaN) counts as 0.
    """
    c = np.asarray(cite, dtype=float)
    r = np.nan_to_num(np.asarray(relevance, dtype=float), nan=0.0)
    r = np.clip(r, 0.0, 1.0)
    top = c.max() if c.size else 0.0
    cn = c / top if top > 0 else c
    return np.power(r, beta) * np.power(cn, 1.0 - beta)


def topk_mean(similarity, k):
    """Mean of the ``k`` largest values in each row of a 2-D array."""
    s = np.asarray(similarity, dtype=float)
    k = max(1, min(int(k), s.shape[1]))
    return np.partition(s, -k, axis=1)[:, -k:].mean(axis=1)


def select(weight, cited_by, relevance, min_citations, min_relevance,
           budget):
    """Return indices of papers to add, heaviest first.

    A paper is eligible if at least ``min_citations`` library papers cite it,
    or at least one does and its relevance is at least ``min_relevance``.
    """
    w = np.asarray(weight, dtype=float)
    n = np.asarray(cited_by)
    r = np.nan_to_num(np.asarray(relevance, dtype=float), nan=0.0)
    ok = (n >= min_citations) | ((n >= 1) & (r >= min_relevance))
    idx = np.flatnonzero(ok)
    order = idx[np.argsort(-w[idx], kind="stable")]
    return order[: int(budget)]


def coverage(n_resolved, n_total):
    """Fraction of references that point to a paper in the library."""
    return float(n_resolved) / float(n_total) if n_total else 0.0
