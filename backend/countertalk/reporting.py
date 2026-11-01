"""Numbers for people to read (CODING_GUIDELINES §8.5 R10): a share always names what it's a
share of, and a count of outcomes shows every bucket, an unexpected one too. Every percentage
the scripts and the app print goes through here (a test scans for any other)."""

from collections.abc import Mapping, Sequence


def share(part: int, whole: int, of: str) -> str:
    """'3/4 (75%) of matched insights'; with nothing to divide, '0/0 of matched insights'."""
    shown = f"{part}/{whole}" + (f" ({part / whole:.0%})" if whole else "")
    return f"{shown} of {of}"


def percent(part: float, whole: float, of: str) -> str:
    """'97.3% of speech time', for amounts rather than counts; '0.0%' with nothing to divide."""
    return f"{part / whole if whole else 0:.1%} of {of}"


def _named(counts: Mapping[str, int], names: Sequence[str]) -> list[tuple[str, int]]:
    """Each named bucket in order, a zero too, then any other found: none is ever dropped."""
    extra = sorted(name for name, count in counts.items() if name not in names and count)
    return [(name, counts.get(name, 0)) for name in [*names, *extra]]


def buckets(counts: Mapping[str, int], names: Sequence[str]) -> str:
    """'exact 3, fuzzy 0, none 1' for people (_named's buckets)."""
    return ", ".join(f"{name} {count}" for name, count in _named(counts, names))


def log_buckets(counts: Mapping[str, int], names: Sequence[str]) -> str:
    """'exact=3 fuzzy=0 none=1' for a log line: the same buckets, none dropped (added in batch
    14, when a log line that picked its counts by hand left one out)."""
    return " ".join(f"{name}={count}" for name, count in _named(counts, names))
