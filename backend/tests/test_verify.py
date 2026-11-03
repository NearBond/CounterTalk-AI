"""verify.py: every quote checked against the transcript (batch 11; BRIEF D9).

Pure functions, so no database: each case is a few turns and a quote.
"""

import pytest

from countertalk import limits
from countertalk.playbooks import LANGUAGES
from countertalk.stt import meta
from countertalk.verify import (
    NUMBER_WORDS,
    Transcript,
    TurnText,
    normalise,
    number_words,
    verify_quote,
)


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


# --- number words by language (batch T3) ------------------------------------------------------

MARATHI = ("mr", "hi", "en")
# Hindi number words and fractions, as batch 11's normalise() gave them before T3: byte for
# byte, spans included. Hindi's fractions (ढाई, साढ़े, डेढ़, सवा, पौने) were never digits.
HINDI_SAMPLE = (
    "भैया, ढाई हज़ार का माल और साढ़े तीन सौ की स्कीम, डेढ़ लाख बाकी है। "
    "चार हजार पांच सौ, 5 लाख, दो दो पेटी, सवा सौ रुपये, पौने दो हजार, ४,५०० और एक करोड़।"
)
HINDI_TODAY = (
    "भैया ढाई 1000 का माल और साढे 300 की स्कीम डेढ 100000 बाकी है "
    "50504002 पेटी सवा 100 रुपये पौने 2000 4500 और 10000000"
)
HINDI_TODAY_SPANS = (
    (0, 1),
    (1, 2),
    (2, 3),
    (3, 4),
    (6, 7),
    (6, 7),
    (7, 8),
    (8, 9),
    (10, 15),
    (10, 15),
    (10, 15),
    (10, 15),
    (10, 15),
    (16, 17),
    (16, 17),
    (17, 18),
    (19, 20),
    (19, 20),
    (20, 21),
    (21, 22),
    (23, 24),
    (23, 24),
    (24, 25),
    (26, 27),
    (26, 27),
    (27, 28),
    (28, 30),
    (30, 31),
    (32, 38),
    (32, 38),
    (32, 38),
    (32, 38),
    (39, 40),
    (39, 40),
    (40, 41),
    (42, 44),
    (42, 44),
    (42, 44),
    (44, 45),
    (45, 46),
    (46, 47),
    (49, 50),
    (49, 50),
    (50, 51),
    (51, 53),
    (54, 57),
    (54, 57),
    (54, 57),
    (54, 57),
    (54, 57),
    (54, 57),
    (54, 57),
    (58, 59),
    (58, 59),
    (59, 60),
    (60, 61),
    (61, 62),
    (63, 64),
    (63, 64),
    (64, 65),
    (67, 97),
    (67, 97),
    (67, 97),
    (67, 97),
    (67, 97),
    (67, 97),
    (67, 97),
    (67, 97),
    (67, 97),
    (98, 99),
    (98, 99),
    (99, 100),
    (100, 101),
    (101, 102),
    (104, 105),
    (104, 105),
    (105, 106),
    (106, 107),
    (108, 110),
    (108, 110),
    (108, 110),
    (108, 110),
    (111, 112),
    (111, 112),
    (112, 113),
    (113, 114),
    (114, 115),
    (115, 116),
    (118, 119),
    (118, 119),
    (119, 120),
    (120, 121),
    (121, 122),
    (123, 130),
    (123, 130),
    (123, 130),
    (123, 130),
    (123, 130),
    (132, 133),
    (132, 133),
    (134, 135),
    (135, 136),
    (136, 137),
    (138, 139),
    (138, 139),
    (139, 140),
    (141, 149),
    (141, 149),
    (141, 149),
    (141, 149),
    (141, 149),
    (141, 149),
    (141, 149),
    (141, 149),
    (141, 149),
)


def test_a_hindi_visit_is_normalised_as_before() -> None:
    for languages in (("hi", "en"), ("hi",), ("en", "hi")):
        normalised = normalise(HINDI_SAMPLE, languages)

        assert (normalised.text, normalised.spans) == (HINDI_TODAY, HINDI_TODAY_SPANS)


@pytest.mark.parametrize(
    ("said", "normalised"),
    [
        ("अडीच हजार रुपये", "2500 रुपये"),
        ("साडे तीन हजार", "3500"),
        ("साडेतीन हजार", "3500"),
        ("दीड लाख", "150000"),
        ("सव्वा लाख", "125000"),
        ("पावणे दोन हजार", "1750"),
        ("दोन हजार पाचशे पन्नास", "2550"),
        ("पाचशे", "500"),
        ("दीडशे", "150"),
        ("अडीचशे", "250"),
        ("साडेतीनशे", "350"),
        ("साडे तीनशे", "350"),
        ("पंधराशे रुपये", "1500 रुपये"),
        ("बाराशे पन्नास", "1250"),
        ("सव्वाशे", "125"),
        ("पावणेदोनशे", "175"),
        ("शंभर रुपये", "100 रुपये"),
        ("एकशे वीस", "120"),
        ("पाच कोटी", "50000000"),
        ("५ हजार", "5000"),
        ("नव्व्याण्णव पेटी", "99 पेटी"),
        ("चौऱ्याहत्तर", "74"),
        ("चौर्याहत्तर", "74"),
        ("साडे तीन", "साडे 3"),  # a fraction on its own stays words, its number not
        ("दोन हजार, साडेतीन किलो", "2000 साडेतीन किलो"),  # cut back to what is whole
        ("पाचशे, दीड किलो", "500 दीड किलो"),
        ("सव्वा", "सव्वा"),
        ("पावणे", "पावणे"),
    ],
)
def test_marathi_number_words_are_digits_for_a_marathi_visit(said: str, normalised: str) -> None:
    assert normalise(said, MARATHI).text == normalised


def test_a_hindi_visit_doesnt_read_marathi_number_words() -> None:
    # तेरा is "your" in Hindi, 13 in Marathi.
    assert normalise("तेरा माल अडीच हजार", ("hi", "en")).text == "तेरा माल अडीच 1000"
    assert normalise("तेरा माल अडीच हजार", MARATHI).text == "13 माल 2500"


def test_a_quote_in_digits_matches_marathi_words_in_a_marathi_transcript() -> None:
    turns = [TurnText(1, "shopkeeper", 0, 6_000, "अडीच हजार रुपये अजून बाकी आहेत")]

    found = verify_quote("2500 रुपये अजून बाकी", 1, Transcript(turns, MARATHI))

    assert (found.match, found.quote_final) == ("exact", "अडीच हजार रुपये अजून बाकी")
    assert verify_quote("2500 रुपये अजून बाकी", 1, Transcript(turns)).match != "exact"


def test_every_language_has_its_number_words_and_a_name_for_speech_to_text() -> None:
    assert set(NUMBER_WORDS) == set(LANGUAGES) == set(meta.LANGUAGE_NAMES)


def test_no_word_is_two_numbers_or_two_kinds_of_number_word() -> None:
    tables = list(NUMBER_WORDS.values())
    for kind in ("units", "hundreds", "scales"):
        seen: dict[str, object] = {}
        for table in tables:
            for word, value in getattr(table, kind).items():
                assert seen.setdefault(word, value) == value, (kind, word)
    together = number_words(frozenset(LANGUAGES))
    kinds = [set(together.units), set(together.hundreds), set(together.scales), together.hundred]
    for i, one in enumerate(kinds):
        for other in kinds[i + 1 :]:
            assert one & other == set()


@pytest.mark.parametrize(
    ("said", "quoted", "found"),
    [
        ("साडे तीन किलो चिप्स पाठवा", "तीन किलो चिप्स पाठवा", "तीन किलो चिप्स पाठवा"),
        ("दोन हजार, साडेतीन किलो", "दोन हजार", "दोन हजार"),
    ],
)
def test_part_of_a_marathi_amount_can_be_quoted(said: str, quoted: str, found: str) -> None:
    verified = verify_quote(
        quoted, 0, Transcript([TurnText(0, "shopkeeper", 0, 5_000, said)], MARATHI)
    )

    assert (verified.match, verified.quote_final) == ("exact", found)
