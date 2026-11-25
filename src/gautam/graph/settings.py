"""Settings for the citation-graph search (``[graph]`` in gautam.toml)."""

import difflib
from dataclasses import dataclass, field, fields, replace

from gautam.errors import SettingsError


def _f(default, help_text):
    return field(default=default, metadata={"help": help_text})


@dataclass(frozen=True)
class GraphSettings:
    """All knobs of the weighted graph search, with checks."""

    decay: float = _f(0.5, "weight kept per step away from your library "
                           "(0-1]; 0.5 halves it each step")
    beta: float = _f(0.5, "say of relevance vs citations: 0 = citations only,"
                          " 1 = relevance only, 0.5 = equal")
    min_citations: int = _f(3, "a paper cited by at least this many library "
                               "papers is eligible regardless of topic")
    min_relevance: float = _f(0.6, "a paper cited at least once is eligible "
                                   "if its relevance reaches this (0-1)")
    budget: int = _f(20000, "most papers added per round")
    max_hops: int = _f(2, "how many steps out from your library to go")
    lookup_factor: int = _f(3, "look up this many x budget candidates "
                               "(heaviest by citations) to score relevance")
    knn: int = _f(10, "relevance = mean similarity to the k closest "
                      "passages already in the library")
    stop_gain: float = _f(0.01, "stop when a round raises reference "
                                "coverage by less than this")

    def __post_init__(self):
        """Check every value; the message names the bad value and the fix."""
        checks = {
            "decay": 0 < self.decay <= 1,
            "beta": 0 <= self.beta <= 1,
            "min_citations": self.min_citations >= 1,
            "min_relevance": 0 <= self.min_relevance <= 1,
            "budget": self.budget >= 1,
            "max_hops": self.max_hops >= 1,
            "lookup_factor": self.lookup_factor >= 1,
            "knn": self.knn >= 1,
            "stop_gain": 0 <= self.stop_gain < 1,
        }
        for name, ok in checks.items():
            if not ok:
                f = next(x for x in fields(self) if x.name == name)
                raise SettingsError(
                    f"[graph] {name} = {getattr(self, name)!r} is out of range"
                    f": {f.metadata['help']} (default {f.default!r})")

    @classmethod
    def from_mapping(cls, raw: dict | None) -> "GraphSettings":
        """Build from the ``[graph]`` table, rejecting unknown names."""
        raw = dict(raw or {})
        names = [f.name for f in fields(cls)]
        for key in raw:
            if key not in names:
                near = difflib.get_close_matches(key, names, n=1)
                hint = f"; did you mean {near[0]!r}?" if near else ""
                raise SettingsError(f"[graph] unknown setting {key!r}{hint}")
        return cls(**raw)

    def copy(self, **changes) -> "GraphSettings":
        """Return a checked copy with some values changed."""
        return replace(self, **changes)

    @classmethod
    def describe(cls) -> str:
        """One line per setting: name, default, meaning (for help/docs)."""
        return "\n".join(f"{f.name:14} {f.default!s:>7}  {f.metadata['help']}"
                         for f in fields(cls))
