"""scripts/compare_transcript.py: mapping speech-to-text's labels to roles by time (batch 6)."""

import json
from pathlib import Path

import pytest

from countertalk.db import session_scope
from countertalk.models import SpeakerRole
from scripts.compare_transcript import Span, main, map_labels, role_agreement, speaker_share
from tests.conftest import give_transcript
from tests.test_visits import make_org, new_visit

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


def test_roles_saved_and_by_overlap_side_by_side_with_the_share_that_agree() -> None:
    turns = [
        Span("c0:A", 0, 1_000, ""),  # rep by overlap, saved rep
        Span("c0:B", 1_000, 3_000, ""),  # shopkeeper by overlap, saved rep
        Span("c0:A", 3_000, 4_000, ""),
        Span("c1:C", 9_000, 9_500, ""),  # overlaps nothing; saved other
        Span("c1:D", 9_500, 9_600, ""),  # no saved role
    ]
    saved = {"c0:A": "rep", "c0:B": "rep", "c1:C": "other"}

    assert role_agreement(turns, REFERENCE, saved) == [
        "Roles, saved and by overlap:",
        "  c0:A    saved rep         overlap rep                 agree",
        "  c0:B    saved rep         overlap shopkeeper          differ",
        "  c1:C    saved other       overlap (overlaps nothing)  differ",
        "  c1:D    saved (none)      overlap (overlaps nothing)  differ",
        "  agree: 1 of 4 labels, 43.5% of speech time",  # 2,000 of 4,600 ms
    ]


def test_roles_print_only_labels_and_roles_never_words(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    org = make_org()
    visit_id = new_visit(org)
    lines = (("c0:A", 0, "Namaste Asha ji."), ("c0:B", 2_000, "Haan bolo."))
    transcript_id = give_transcript(visit_id, lines)
    with session_scope() as session:
        session.add_all(
            SpeakerRole(
                transcript_id=transcript_id,
                speaker_label=label,
                role=role,
                confidence=0.9,
                playbook_id="fmcg-kirana-visit",
                playbook_version=1,
            )
            for label, role in (("c0:A", "rep"), ("c0:B", "shopkeeper"))
        )
    reference = tmp_path / "rp99.turns.json"
    turns = [
        {"role": "rep", "start_ms": 0, "end_ms": 1, "text": "Namaste Asha ji."},
        {"role": "shopkeeper", "start_ms": 2_000, "end_ms": 2_001, "text": "Haan bolo."},
    ]
    reference.write_text(json.dumps({"turns": turns}), encoding="utf-8")

    assert main([visit_id, str(reference), "--roles"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "Roles, saved and by overlap:",
        "  c0:A    saved rep         overlap rep                 agree",
        "  c0:B    saved shopkeeper  overlap shopkeeper          agree",
        "  agree: 2 of 2 labels, 100.0% of speech time",
    ]


def test_roles_of_a_visit_with_none_saved_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = new_visit(make_org())
    give_transcript(visit_id)
    reference = tmp_path / "rp99.turns.json"
    reference.write_text(json.dumps({"turns": []}), encoding="utf-8")

    assert main([visit_id, str(reference), "--roles"]) == 1

    assert capsys.readouterr().err == f"visit {visit_id} has no saved roles\n"


@pytest.mark.parametrize(
    ("content", "why"),
    [
        (None, "it can't be read"),
        ("not json", "it isn't JSON"),
        ('{"turns": [{}]}', "a turn lacks role"),
    ],
    ids=["missing", "not-json", "missing-field"],
)
def test_a_reference_it_cant_read_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str | None, why: str
) -> None:
    visit_id = new_visit(make_org())
    give_transcript(visit_id)
    reference = tmp_path / "rp99.turns.json"
    if content is not None:
        reference.write_text(content, encoding="utf-8")

    assert main([visit_id, str(reference)]) == 1

    assert capsys.readouterr().err == f"can't use {reference}: {why}\n"
