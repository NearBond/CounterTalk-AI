"""countertalk/reporting.py: counts shown with every bucket (R10)."""

from collections import Counter

from countertalk.reporting import buckets, log_buckets


def test_every_named_bucket_shows_a_zero_too_and_any_other_follows() -> None:
    counts = Counter({"linked": 2, "competitor": 1, "dropped": 0})

    assert log_buckets(counts, ("linked", "unknown")) == "linked=2 unknown=0 competitor=1"
    assert buckets(counts, ("linked", "unknown")) == "linked 2, unknown 0, competitor 1"
