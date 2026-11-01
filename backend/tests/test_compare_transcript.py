"""scripts/compare_transcript.py: mapping speech-to-text's labels to roles by time (batch 6)."""

from scripts.compare_transcript import Span, map_labels, speaker_share

REFERENCE = [
    Span("rep", 0, 1_000, ""),
    Span("shopkeeper", 1_000, 2_000, ""),
    Span("shopkeeper", 2_000, 3_000, ""),
    Span("rep", 3_000, 4_000, ""),
]


def test_each_chunks_labels_map_to_the_role_they_overlap_most() -> None:
    # A is the rep in chunk 0 and the shopkeeper in chunk 1: labels never match across chunks.
    turns = [
        Span("c0:A", 0, 1_100, ""),
        Span("c0:B", 1_100, 2_000, ""),
        Span("c1:A", 2_000, 3_000, ""),
        Span("c1:B", 3_000, 4_000, ""),
        Span("c1:C", 9_000, 9_500, ""),  # overlaps nothing, so it maps to nothing
    ]

    assert map_labels(turns, REFERENCE) == {
        "c0:A": "rep",
        "c0:B": "shopkeeper",
        "c1:A": "shopkeeper",
        "c1:B": "rep",
    }


def test_the_share_counts_speech_time_on_the_mapped_role() -> None:
    turns = [Span("c0:A", 0, 1_500, ""), Span("c0:B", 1_500, 4_000, "")]
    mapping = map_labels(turns, REFERENCE)

    right, total = speaker_share(turns, REFERENCE, mapping)

    # c0:A is the rep (1,000 ms of rep against 500 of shopkeeper); c0:B the shopkeeper (1,500
    # against 1,000 of rep).
    assert mapping == {"c0:A": "rep", "c0:B": "shopkeeper"}
    assert (right, total) == (1_000 + 1_500, 4_000)


def test_cross_talk_counts_once() -> None:
    # Both speak from 1,000 to 1,500: a perfect split still scores the whole of it.
    reference = [Span("rep", 0, 1_500, ""), Span("shopkeeper", 1_000, 2_000, "")]
    turns = [Span("c0:A", 0, 1_000, ""), Span("c0:B", 1_000, 2_000, "")]

    right, total = speaker_share(turns, reference, map_labels(turns, reference))

    assert (right, total) == (2_000, 2_000)
