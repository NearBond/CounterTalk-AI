"""verify.py: every quote checked against the transcript (batch 11; BRIEF D9).

Pure functions, so no database: each case is a few turns and a quote.
"""

import pytest

from countertalk import limits
from countertalk.verify import Transcript, TurnText, normalise, verify_quote


def turns(*texts: str, length_ms: int = 4_000) -> Transcript:
    """Turns 0, 1, 2, … each `length_ms` long, back to back, the shopkeeper's."""
    return Transcript(
        [
            TurnText(i, "shopkeeper", i * length_ms, (i + 1) * length_ms, text)
            for i, text in enumerate(texts)
        ]
    )


@pytest.mark.parametrize(
    ("text", "normalised"),
    [
        ("नमस्ते   जी।", "नमस्ते जी"),  # punctuation and runs of spaces
        ("Kurkure का PACK", "kurkure का pack"),  # Latin lowercased, matras kept
        ("दो\u200dपैकेट\u200b", "दोपैकेट"),  # zero-width joiner and space dropped
        ("ज़्यादा फ़्लेवर", "ज्यादा फ्लेवर"),  # nukta folded
        ("हाँ पाँच", "हां 5"),  # chandrabindu folded to anusvara; a number word
        ("चार हज़ार पाँच सौ रुपये", "4500 रुपये"),
        ("₹4,500", "4500"),  # a comma between digits goes; the symbol is a space
        ("४५०० रुपये", "4500 रुपये"),  # Devanagari digits
        ("दो लाख", "200000"),
        ("तीन करोड़ बीस लाख", "32000000"),
        ("सौ", "100"),
        ("नहीं नहीं नहीं आया", "नहीं आया"),  # a word repeated straight after itself
        ("ji,haan", "ji haan"),  # punctuation between letters is a space
        ("5 लाख", "500000"),  # a digit before a scale word joins it
        ("दस हज़ार हज़ार रुपये", "10000 रुपये"),  # a doubled word, collapsed before the numbers
        ("दो दो पैकेट", "2 पैकेट"),
        ("4.5 लाख", "4 500000"),  # only a comma joins digits: 4.5 isn't 45
        ("10-15 पैकेट", "10 15 पैकेट"),
    ],
)
def test_normalising(text: str, normalised: str) -> None:
    assert normalise(text).text == normalised


@pytest.mark.parametrize(
    "text",
    ["हाँ, चार हज़ार पाँच सौ — नहीं नहीं!", "Kurkure\u200d का ₹4,500 pack।", "ज़्यादा  फ़्लेवर"],
)
def test_every_normalised_character_maps_back_into_the_original(text: str) -> None:
    found = normalise(text)

    assert len(found.spans) == len(found.text)
    for start, end in found.spans:
        assert 0 <= start < end <= len(text)
    assert [start for start, _ in found.spans] == sorted(start for start, _ in found.spans)


def test_an_exact_quote_keeps_the_transcripts_own_text_and_offsets() -> None:
    transcript = turns("नमस्ते जी।", "हाँ, माल आया। पर लेट।")

    found = verify_quote("हाँ माल आया", 1, transcript)

    text = transcript.turns[1].text
    assert (found.match, found.turn_id, found.quote_final) == ("exact", 1, "हाँ, माल आया")
    assert text[found.char_start : found.char_end] == found.quote_final


@pytest.mark.parametrize(
    ("quote", "turn_text", "final"),
    [
        # Mixed scripts and case.
        ("KURKURE का pack", "Kurkure का Pack नहीं है", "Kurkure का Pack"),
        # A zero-width joiner in the transcript.
        ("दो पैकेट", "दो\u200d पैकेट दो", "दो\u200d पैकेट"),
        # The model dropped a doubled word the transcript has: still exact, and the transcript's
        # words are kept (batch 6: Meta doubles about 5 words a minute).
        ("नहीं आया", "माल नहीं नहीं आया", "नहीं नहीं आया"),
        # Number words against digits, both ways.
        ("4500 रुपये", "चार हज़ार पाँच सौ रुपये बाकी", "चार हज़ार पाँच सौ रुपये"),
        ("चार हजार पांच सौ रुपये", "बस 4,500 रुपये", "4,500 रुपये"),
        # Nukta and chandrabindu written either way.
        ("ज्यादा हां", "ज़्यादा हाँ जी", "ज़्यादा हाँ"),
    ],
    ids=[
        "mixed-scripts",
        "zero-width",
        "doubled-word",
        "words-to-digits",
        "digits-to-words",
        "nukta",
    ],
)
def test_exact_matches_through_the_normalising(quote: str, turn_text: str, final: str) -> None:
    found = verify_quote(quote, 0, turns(turn_text))

    assert (found.match, found.quote_final) == ("exact", final)


def test_a_quote_in_a_neighbouring_turn_moves_its_turn_there() -> None:
    transcript = turns("एक", "दो बात", "कूलर ठंडा नहीं कर रहा", "तीन", "चार")

    found = verify_quote("कूलर ठंडा नहीं कर रहा", 4, transcript)

    assert (found.match, found.turn_id, found.speaker_role) == ("exact", 2, "shopkeeper")


def test_a_quote_found_far_away_is_still_found() -> None:
    transcript = turns("कूलर ठंडा नहीं कर रहा", *(["कुछ और"] * 8))

    assert verify_quote("कूलर ठंडा नहीं कर रहा", 8, transcript).turn_id == 0


def test_a_tie_between_turns_goes_to_the_one_nearest_the_cited_turn() -> None:
    transcript = turns("माल आया", "a", "b", "माल आया", "c", "d")

    assert verify_quote("माल आया", 4, transcript).turn_id == 3


def test_a_spelling_slip_is_a_fuzzy_match_replaced_by_the_transcripts_text() -> None:
    turn = "देखो सीधी बात कहता हूँ बुरा मत मानना मैं सोच रहा हूँ किसी दूसरे डिस्ट्रीब्यूटर से माल लूँ"
    quote = "देखो सीधी बात कहता हु बुरा मत मानना मैं सोच रहा हु किसी दूसरे डिस्ट्रीब्यूटर से माल लूँ"

    found = verify_quote(quote, 0, turns(turn))

    assert (found.match, found.quote_final) == ("fuzzy", turn)
    assert found.score >= limits.QUOTE_FUZZY_MIN_SCORE


@pytest.mark.parametrize(
    "quote",
    [
        "The distributor's salesman hasn't come for two weeks.",  # a translation
        "सेल्समैन बहुत दिनों से आया ही नहीं है भाई",  # a paraphrase
        "",
        "   ",
    ],
    ids=["translation", "paraphrase", "empty", "blank"],
)
def test_a_translation_a_paraphrase_or_nothing_is_rejected(quote: str) -> None:
    transcript = turns("वो दो हफ्ते से नहीं आया", "ऑर्डर लेने कोई नहीं आता")

    found = verify_quote(quote, 0, transcript)

    assert (found.match, found.quote_final, found.char_start, found.start_ms) == (
        "none",
        None,
        None,
        None,
    )
    # The cited turn and its role, kept as the extraction gave them.
    assert (found.turn_id, found.speaker_role) == (0, "shopkeeper")


def test_a_quote_across_two_turns_is_never_matched_by_joining_them() -> None:
    transcript = turns("पिछले हफ्ते का माल", "अभी तक नहीं आया")

    assert verify_quote("पिछले हफ्ते का माल अभी तक नहीं आया", 0, transcript).match == "none"


def test_a_short_quote_is_never_a_fuzzy_match() -> None:
    # partial_ratio scores a few characters high almost anywhere.
    # "माल आयी" is a slip of "माल आया", but too short to trust a fuzzy score for.
    assert verify_quote("माल आयी", 0, turns("सारा माल आया")).match == "none"


@pytest.mark.parametrize(
    ("turn_start_ms", "char_start", "start_ms"),
    [
        # 1.5 s before the quote, into the turn before if need be (decided 1 Nov, D1): a
        # speech-to-text turn can start a little after the speech, clipping the first syllable.
        (10_000, 0, 8_500),
        (10_000, 10, 9_500),  # 1 s in
        (10_000, 30, 11_500),  # 3 s in
        (1_000, 0, 0),  # never before the recording
    ],
)
def test_playback_starts_a_little_before_the_quote(
    turn_start_ms: int, char_start: int, start_ms: int
) -> None:
    # A 60-character turn, 6 s long: each character is 0.1 s.
    quote = "माल नहीं आया"
    before = ("x" * (char_start - 1) + " ") if char_start else ""
    text = before + quote + " " + "y" * (60 - char_start - len(quote) - 1)
    end_ms = turn_start_ms + 6_000
    transcript = Transcript([TurnText(0, "shopkeeper", turn_start_ms, end_ms, text)])

    found = verify_quote("माल नहीं आया", 0, transcript)

    assert (found.char_start, found.start_ms) == (char_start, start_ms)


def test_verifying_twice_gives_the_same_result() -> None:
    transcript = turns("नमस्ते", "हाँ, माल आया। पर लेट।")

    assert verify_quote("माल आया", 0, transcript) == verify_quote("माल आया", 0, transcript)


@pytest.mark.parametrize(
    ("quote", "turn_text"),
    [
        ("4,500 रुपये बाकी", "बस 14,500 रुपये बाकी है"),  # inside a longer number
        ("पांच सौ रुपये बाकी", "चार हज़ार पांच सौ रुपये बाकी"),  # 500 isn't 4500
        ("45 लाख का माल", "4.5 लाख का माल"),
    ],
    ids=["inside-a-number", "part-of-an-amount", "decimal-point"],
)
def test_a_different_amount_is_never_an_exact_match(quote: str, turn_text: str) -> None:
    assert verify_quote(quote, 0, turns(turn_text)).match != "exact"


def test_an_exact_match_is_whole_words() -> None:
    # Cut mid-word, it's fuzzy at best: the transcript's whole word is what's kept.
    found = verify_quote("पहले वाला डीलर बहुत अच्छा", 0, turns("सच में पहले वाला डीलर बहुत अच्छाई से"))

    assert found.match != "exact"


def test_a_fuzzy_match_takes_whole_words_and_no_more() -> None:
    turn = "पहले वाला डीलर बहुत अच्छा था अब वाला कुछ नहीं करता"

    found = verify_quote("वाला डीलर बहुता अच्छा", 0, turns(turn))

    assert (found.match, found.quote_final) == ("fuzzy", "वाला डीलर बहुत अच्छा")


def test_a_quote_longer_than_any_turn_is_never_cut_to_fit_one() -> None:
    transcript = turns("पिछले हफ्ते का सारा माल अभी तक नहीं आया", "जी")

    found = verify_quote("पिछले हफ्ते का सारा माल अभी तक नहीं आया जी", 0, transcript)

    assert found.match == "none"
