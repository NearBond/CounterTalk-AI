"""Quote verification (batch 11; BRIEF D9): every quote is checked against the transcript, and
replaced with the transcript's own text.

Both sides are normalised: NFC; zero-width characters and nukta gone; punctuation a space (a
comma between digits joins them); chandrabindu as anusvara; spaces collapsed; Latin lowercased;
a word repeated straight after itself once (Meta doubles about 5 words a minute); then
Devanagari digits and number words as digits ("चार हज़ार पाँच सौ" and "4 हजार 5 सौ" are
"4500"): Hindi's by default, or those of the org pack's languages (batch T3: Marathi's too, such
as "अडीच हजार" 2500 and "पाचशे" 500). Every normalised character keeps the span of the original
it came from, so a match maps back exactly.

A quote lies in one turn: turns are searched one by one, never joined, and one shorter than the
quote never matches. Exact first, on whole words, nearest the cited turn first (so the cited
turn, then 2 either side, then the rest); then a fuzzy match (rapidfuzz partial_ratio_alignment,
QUOTE_FUZZY_MIN_SCORE or more, widened to whole words) over the same turns, the best score
winning and a tie going to the nearest. Everything here is pure: the same quote and turns always
give the same result (§9.4).
"""

import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from functools import cache
from typing import Literal

from rapidfuzz import fuzz

from countertalk import limits
from countertalk.playbooks import DEFAULT_LANGUAGES

ZERO_WIDTH = frozenset("\u200b\u200c\u200d\ufeff")
NUKTA = "\u093c"
CHANDRABINDU, ANUSVARA = "\u0901", "\u0902"
DEVANAGARI_DIGITS = {chr(0x0966 + n): str(n) for n in range(10)}

# Hindi number words, 1 to 99, with the spellings speech-to-text writes. Folded like the text
# (no nukta, chandrabindu as anusvara) before use.
_UNIT_WORDS = {
    1: "एक",
    2: "दो",
    3: "तीन",
    4: "चार",
    5: "पाँच पांच",
    6: "छह छः छे",
    7: "सात",
    8: "आठ",
    9: "नौ",
    10: "दस",
    11: "ग्यारह",
    12: "बारह",
    13: "तेरह",
    14: "चौदह",
    15: "पंद्रह पन्द्रह",
    16: "सोलह",
    17: "सत्रह",
    18: "अठारह",
    19: "उन्नीस",
    20: "बीस",
    21: "इक्कीस",
    22: "बाईस",
    23: "तेईस",
    24: "चौबीस",
    25: "पच्चीस",
    26: "छब्बीस",
    27: "सत्ताईस",
    28: "अट्ठाईस",
    29: "उनतीस",
    30: "तीस",
    31: "इकतीस इकत्तीस",
    32: "बत्तीस",
    33: "तैंतीस",
    34: "चौंतीस",
    35: "पैंतीस",
    36: "छत्तीस",
    37: "सैंतीस",
    38: "अड़तीस",
    39: "उनतालीस",
    40: "चालीस",
    41: "इकतालीस",
    42: "बयालीस",
    43: "तैंतालीस",
    44: "चवालीस",
    45: "पैंतालीस",
    46: "छियालीस",
    47: "सैंतालीस",
    48: "अड़तालीस",
    49: "उनचास",
    50: "पचास",
    51: "इक्यावन",
    52: "बावन",
    53: "तिरेपन",
    54: "चौवन",
    55: "पचपन",
    56: "छप्पन",
    57: "सत्तावन",
    58: "अट्ठावन",
    59: "उनसठ",
    60: "साठ",
    61: "इकसठ",
    62: "बासठ",
    63: "तिरेसठ",
    64: "चौंसठ",
    65: "पैंसठ",
    66: "छियासठ",
    67: "सड़सठ",
    68: "अड़सठ",
    69: "उनहत्तर",
    70: "सत्तर",
    71: "इकहत्तर",
    72: "बहत्तर",
    73: "तिहत्तर",
    74: "चौहत्तर",
    75: "पचहत्तर",
    76: "छिहत्तर",
    77: "सतहत्तर",
    78: "अठहत्तर",
    79: "उन्यासी उनासी",
    80: "अस्सी",
    81: "इक्यासी",
    82: "बयासी",
    83: "तिरासी",
    84: "चौरासी",
    85: "पचासी",
    86: "छियासी",
    87: "सत्तासी",
    88: "अट्ठासी",
    89: "नवासी",
    90: "नब्बे",
    91: "इक्यानवे",
    92: "बानवे",
    93: "तिरानवे",
    94: "चौरानवे",
    95: "पचानवे",
    96: "छियानवे",
    97: "सत्तानवे",
    98: "अट्ठानवे",
    99: "निन्यानवे",
}
_HUNDRED = "सौ"
_SCALE_WORDS = {1_000: "हज़ार हजार", 100_000: "लाख", 10_000_000: "करोड़ करोड"}

# Marathi number words, 1 to 99, as CLDR's Marathi spell-out rules write them (cldr
# common/rbnf/mr.xml, read 3 Nov 2026), with ऱ्य beside र्य where a word has it.
_MARATHI_UNIT_WORDS = {
    1: "एक",
    2: "दोन",
    3: "तीन",
    4: "चार",
    5: "पाच",
    6: "सहा",
    7: "सात",
    8: "आठ",
    9: "नऊ",
    10: "दहा",
    11: "अकरा",
    12: "बारा",
    13: "तेरा",
    14: "चौदा",
    15: "पंधरा",
    16: "सोळा",
    17: "सतरा",
    18: "अठरा",
    19: "एकोणीस",
    20: "वीस",
    21: "एकवीस",
    22: "बावीस",
    23: "तेवीस",
    24: "चोवीस",
    25: "पंचवीस",
    26: "सव्वीस",
    27: "सत्तावीस",
    28: "अठ्ठावीस",
    29: "एकोणतीस",
    30: "तीस",
    31: "एकतीस",
    32: "बत्तीस",
    33: "तेहतीस",
    34: "चौतीस",
    35: "पस्तीस",
    36: "छत्तीस",
    37: "सदतीस",
    38: "अडतीस",
    39: "एकोणचाळीस",
    40: "चाळीस",
    41: "एक्केचाळीस",
    42: "बेचाळीस",
    43: "त्रेचाळीस",
    44: "चव्वेचाळीस",
    45: "पंचेचाळीस",
    46: "सेहेचाळीस",
    47: "सत्तेचाळीस",
    48: "अठ्ठेचाळीस",
    49: "एकोणपन्नास",
    50: "पन्नास",
    51: "एक्कावन्न",
    52: "बावन्न",
    53: "त्रेपन्न",
    54: "चौपन्न",
    55: "पंचावन्न",
    56: "छप्पन्न",
    57: "सत्तावन्न",
    58: "अठ्ठावन्न",
    59: "एकोणसाठ",
    60: "साठ",
    61: "एकसष्ट",
    62: "बासष्ट",
    63: "त्रेसष्ट",
    64: "चौसष्ट",
    65: "पासष्ट",
    66: "सहासष्ट",
    67: "सदुसष्ट",
    68: "अडुसष्ट",
    69: "एकोणसत्तर",
    70: "सत्तर",
    71: "एकाहत्तर",
    72: "बाहत्तर",
    73: "त्र्याहत्तर",
    74: "चौर्याहत्तर चौऱ्याहत्तर",
    75: "पंच्याहत्तर",
    76: "शहात्तर",
    77: "सत्त्याहत्तर",
    78: "अठ्ठ्याहत्तर",
    79: "एकोणऐंशी",
    80: "ऐंशी",
    81: "एक्क्याऐंशी",
    82: "ब्याऐंशी",
    83: "त्र्याऐंशी",
    84: "चौर्याऐंशी चौऱ्याऐंशी",
    85: "पंच्याऐंशी",
    86: "शहाऐंशी",
    87: "सत्त्याऐंशी",
    88: "अठ्ठ्याऐंशी",
    89: "एकोणनव्वद",
    90: "नव्वद",
    91: "एक्याण्णव",
    92: "ब्याण्णव",
    93: "त्र्याण्णव",
    94: "चौर्याण्णव चौऱ्याण्णव",
    95: "पंच्याण्णव",
    96: "शहाण्णव",
    97: "सत्त्याण्णव",
    98: "अठ्ठ्याण्णव",
    99: "नव्व्याण्णव",
}
# 100 said alone (शंभर), and a number of hundreds as one word: the unit, then शे (पाचशे 500).
_MARATHI_HUNDRED, _MARATHI_HUNDREDS_SUFFIX = "शंभर", "शे"
_MARATHI_SCALE_WORDS = {1_000: "हजार", 100_000: "लाख", 10_000_000: "कोटी"}
# Not in CLDR: the fractions said before an amount. दीड and अडीच stand alone (दीड हजार 1500);
# साडे, सव्वा and पावणे change the number after them (साडे तीन 3½, सव्वा दोन 2¼, पावणे दोन
# 1¾), written apart or as one word; सव्वा alone is 1¼ (सव्वा लाख 125000).
_MARATHI_FRACTIONS = {"दीड": Fraction(3, 2), "अडीच": Fraction(5, 2), "सव्वा": Fraction(5, 4)}
_MARATHI_PREFIXES = {"साडे": Fraction(1, 2), "सव्वा": Fraction(1, 4), "पावणे": Fraction(-1, 4)}

Span = tuple[int, int]


@dataclass(frozen=True)
class Normalised:
    text: str
    # For each character of `text`, the span of the original it came from.
    spans: tuple[Span, ...]


def _fold(ch: str) -> str:
    """One character of NFC text, folded: "" for one that goes, " " for a space."""
    if ch in ZERO_WIDTH or ch == NUKTA:
        return ""
    if ch == CHANDRABINDU:
        return ANUSVARA
    if ch in DEVANAGARI_DIGITS:
        return DEVANAGARI_DIGITS[ch]
    if ch.isspace():
        return " "
    return ch.lower()


def _folded_word(word: str) -> str:
    return "".join(_fold(ch) for ch in unicodedata.normalize("NFC", word))


def _table(words: dict[int, str]) -> dict[str, Fraction]:
    return {_folded_word(w): Fraction(n) for n, forms in words.items() for w in forms.split()}


@dataclass(frozen=True)
class NumberWords:
    """One language's number words, folded like the text (batch T3: they follow the languages
    of the visit's org pack)."""

    # 1 to 99, and a fraction said alone (दीड).
    units: dict[str, Fraction] = field(default_factory=dict)
    # Multiplies what's before it, or is 100 alone: सौ, शंभर.
    hundred: frozenset[str] = frozenset()
    # A number of hundreds as one word: पाचशे 500, अडीचशे 250.
    hundreds: dict[str, Fraction] = field(default_factory=dict)
    scales: dict[str, Fraction] = field(default_factory=dict)
    # Joined to the number after it: साडे तीन is 3½.
    prefixes: dict[str, Fraction] = field(default_factory=dict)


def _marathi() -> NumberWords:
    whole = _table(_MARATHI_UNIT_WORDS)
    prefixes = {_folded_word(w): offset for w, offset in _MARATHI_PREFIXES.items()}
    units = whole | {_folded_word(w): value for w, value in _MARATHI_FRACTIONS.items()}
    units |= {p + w: n + offset for p, offset in prefixes.items() for w, n in whole.items()}
    suffix = _folded_word(_MARATHI_HUNDREDS_SUFFIX)
    # Any number below 100 of hundreds: पाचशे 500, पंधराशे 1500, साडेतीनशे 350.
    hundreds = {w + suffix: n * 100 for w, n in units.items() if n < 100}
    return NumberWords(
        units=units,
        hundred=frozenset({_folded_word(_MARATHI_HUNDRED)}),
        hundreds=hundreds,
        scales=_table(_MARATHI_SCALE_WORDS),
        prefixes=prefixes,
    )


HINDI = NumberWords(
    units=_table(_UNIT_WORDS),
    hundred=frozenset({_folded_word(_HUNDRED)}),
    scales=_table(_SCALE_WORDS),
)
# By language code: every one an org pack may name (playbooks.LANGUAGES) has an entry.
NUMBER_WORDS = {"hi": HINDI, "en": NumberWords(), "mr": _marathi()}


@cache
def number_words(languages: frozenset[str]) -> NumberWords:
    """The number words of these languages together. No word is two numbers (a test checks
    every pair of languages)."""
    tables = [NUMBER_WORDS[code] for code in sorted(languages)]
    return NumberWords(
        units={w: n for table in tables for w, n in table.units.items()},
        hundred=frozenset().union(*(table.hundred for table in tables)),
        hundreds={w: n for table in tables for w, n in table.hundreds.items()},
        scales={w: n for table in tables for w, n in table.scales.items()},
        prefixes={w: n for table in tables for w, n in table.prefixes.items()},
    )


def _clusters(text: str) -> Iterator[Span]:
    """A base character and the combining marks after it. NFC joins nothing across these in
    Devanagari or Latin, whose compositions all go through combining marks."""
    start = 0
    for end in range(1, len(text) + 1):
        if end == len(text) or not unicodedata.combining(text[end]):
            yield start, end
            start = end


def _is_punctuation(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "PS"


COMMA, PUNCTUATION = "\x01", "\x00"  # markers, until it's known what's either side


def _characters(text: str) -> list[tuple[str, Span]]:
    """Folded characters with their spans, punctuation as " " (a comma between digits gone),
    and spaces collapsed."""
    folded: list[tuple[str, Span]] = []
    for start, end in _clusters(text):
        for ch in unicodedata.normalize("NFC", text[start:end]):
            if ch == ",":
                out = COMMA
            else:
                out = PUNCTUATION if _is_punctuation(ch) else _fold(ch)
            folded.extend((piece, (start, end)) for piece in out)
    # The next character that isn't a marker, for each position: one pass from the end.
    after: list[str] = [""] * len(folded)
    following = ""
    for i in range(len(folded) - 1, -1, -1):
        after[i] = following
        if folded[i][0] not in (COMMA, PUNCTUATION):
            following = folded[i][0]
    chars: list[tuple[str, Span]] = []
    for i, (ch, span) in enumerate(folded):
        if ch in (COMMA, PUNCTUATION):
            before = chars[-1][0] if chars else ""
            if ch == COMMA and before.isdigit() and after[i].isdigit():
                continue  # "4,500" is 4500; "4.5" stays two numbers
            ch = " "
        if ch == " " and (not chars or chars[-1][0] == " "):
            continue
        chars.append((ch, span))
    while chars and chars[-1][0] == " ":
        chars.pop()
    return chars


# A word, and the span of the original behind each of its characters.
Word = tuple[str, tuple[Span, ...]]


def _words(chars: list[tuple[str, Span]]) -> list[Word]:
    words: list[Word] = []
    current: list[tuple[str, Span]] = []
    for ch, span in [*chars, (" ", (0, 0))]:
        if ch == " ":
            if current:
                words.append(("".join(c for c, _ in current), tuple(sp for _, sp in current)))
            current = []
        else:
            current.append((ch, span))
    return words


def _token(words: list[Word], j: int, table: NumberWords) -> tuple[str, int]:
    """The word at j as a number word, and how many words it takes: a fraction word with the
    number after it ("साडे तीन" reads as साडेतीन), else the word as it is."""
    word = words[j][0]
    if word in table.prefixes and j + 1 < len(words):
        joined = word + words[j + 1][0]
        if joined in table.units or joined in table.hundreds:
            return joined, 2
    return word, 1


def _numbers(words: list[Word], table: NumberWords) -> list[Word]:
    """Runs of number words as digits: "चार हजार पांच सौ" is 4500, "अडीच हजार" 2500. Each digit
    maps to the whole run. A run that comes to a fraction is cut back to its longest start that
    is whole ("दोन हजार साडेतीन" is 2000, then साडेतीन); words that never come to one stay as
    they are ("साडे तीन" is साडे 3): a decimal point is a space here, so its digits would never
    match."""
    out: list[Word] = []
    i = 0
    while i < len(words):
        total = current = Fraction(0)
        j = i
        whole: tuple[int, Fraction] | None = None  # where the longest whole run ends, and its value
        while j < len(words):
            word, width = _token(words, j, table)
            unit = table.units.get(word)
            if unit is None and word.isascii() and word.isdigit() and int(word) < 100:
                # A digit counts as a unit only before what it multiplies: "5 लाख".
                follows = words[j + 1][0] if j + 1 < len(words) else ""
                multiplies = follows in table.hundred or follows in table.scales
                unit = Fraction(int(word)) if multiplies else None
            if unit is not None and current % 100 == 0:
                current += unit
            elif word in table.hundred and current < 100:
                current = (current or 1) * 100
            elif word in table.hundreds and current == 0:
                current = table.hundreds[word]
            elif word in table.scales:
                total += (current or 1) * table.scales[word]
                current = Fraction(0)
            else:
                break
            j += width
            if (total + current).denominator == 1:
                whole = (j, total + current)
        if whole is not None:
            end, value = whole
            digits = str(value.numerator)
            run = (words[i][1][0][0], words[end - 1][1][-1][1])
            out.append((digits, (run,) * len(digits)))
            i = end
        else:
            out.append(words[i])
            i += 1
    return out


def _once(words: list[Word]) -> list[Word]:
    """A word said twice in a row, kept once, its last character stretched over the repeat."""
    kept: list[Word] = []
    for word, spans in words:
        if kept and kept[-1][0] == word:
            last = kept[-1][1]
            kept[-1] = (word, (*last[:-1], (last[-1][0], spans[-1][1])))
        else:
            kept.append((word, spans))
    return kept


def normalise(text: str, languages: Sequence[str] = DEFAULT_LANGUAGES) -> Normalised:
    """With the number words of `languages` (an org pack's); linking keeps the default."""
    table = number_words(frozenset(languages))
    # Repeats first, so a doubled number word ("हजार हजार") isn't added into the amount; then
    # again, for a number said twice ("दो दो").
    kept = _once(_numbers(_once(_words(_characters(text))), table))
    out: list[str] = []
    out_spans: list[Span] = []
    for n, (word, spans) in enumerate(kept):
        if n:
            out.append(" ")
            out_spans.append(spans[0])  # never an edge of a match: those are trimmed
        out.append(word)
        out_spans.extend(spans)
    return Normalised("".join(out), tuple(out_spans))


@dataclass(frozen=True)
class TurnText:
    turn_id: int
    role: str | None
    start_ms: int
    end_ms: int
    text: str


class Transcript:
    """A transcript's turns, normalised once for all its quotes, with the number words of its
    org pack's languages."""

    def __init__(
        self, turns: Sequence[TurnText], languages: Sequence[str] = DEFAULT_LANGUAGES
    ) -> None:
        self.turns = list(turns)
        self.languages = tuple(languages)
        self.normalised = [normalise(turn.text, self.languages) for turn in self.turns]
        self.index = {turn.turn_id: i for i, turn in enumerate(self.turns)}


@dataclass(frozen=True)
class Verified:
    match: Literal["exact", "fuzzy", "none"]
    # Where it was found; for none, the cited turn and its role as they were.
    turn_id: int | None
    speaker_role: str | None
    quote_final: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    start_ms: int | None = None
    score: float = 0.0


def _order(transcript: Transcript, at: int | None) -> list[int]:
    """Turns nearest the cited one (at index `at`) first, the earlier of two equally near."""
    count = len(transcript.turns)
    if at is None:
        return list(range(count))
    return sorted(range(count), key=lambda i: (abs(i - at), i))


def verify_quote(quote: str, cited_turn: int | None, transcript: Transcript) -> Verified:
    at = transcript.index.get(cited_turn) if cited_turn is not None else None
    cited_role = transcript.turns[at].role if at is not None else None
    unmatched = Verified("none", cited_turn if at is not None else None, cited_role)
    wanted = normalise(quote, transcript.languages).text
    if not wanted:
        return unmatched
    order = _order(transcript, at)
    for i in order:
        position = _whole_words(transcript.normalised[i].text, wanted)
        if position is not None:
            return _found("exact", i, position, position + len(wanted), 100.0, transcript)
    if len(wanted) < limits.QUOTE_FUZZY_MIN_CHARS:
        return unmatched
    best: tuple[float, int, int, int] | None = None
    for i in order:
        text = transcript.normalised[i].text
        if len(wanted) > len(text):
            continue  # it can't lie in this turn: cut to fit, words would be lost
        aligned = fuzz.partial_ratio_alignment(wanted, text)
        if best is None or aligned.score > best[0]:
            best = (aligned.score, i, aligned.dest_start, aligned.dest_end)
            if aligned.score == 100:
                break  # nothing nearer can beat it
    if best is None or best[0] < limits.QUOTE_FUZZY_MIN_SCORE:
        return unmatched
    score, i, start, end = best
    start, end = _widened(transcript.normalised[i].text, start, end)
    return _found("fuzzy", i, start, end, score, transcript)


def _whole_words(text: str, wanted: str) -> int | None:
    """Where `wanted` is in `text` as whole words: 4500 isn't inside 14500."""
    position = text.find(wanted)
    while position >= 0:
        end = position + len(wanted)
        if (position == 0 or text[position - 1] == " ") and (end == len(text) or text[end] == " "):
            return position
        position = text.find(wanted, position + 1)
    return None


def _widened(text: str, start: int, end: int) -> tuple[int, int]:
    """A fuzzy alignment stretched to whole words: from an edge on a space, the word beside it
    isn't taken in."""
    while start < end and text[start] == " ":
        start += 1
    while end > start and text[end - 1] == " ":
        end -= 1
    while start > 0 and text[start - 1] != " ":
        start -= 1
    while end < len(text) and text[end] != " ":
        end += 1
    return start, end


def playback_start(turn_start_ms: int, into_ms: float) -> int:
    """When to start playing a quote that begins into_ms into its turn: QUOTE_LEAD_IN_MS
    before it, into the turn before if need be (decided 1 Nov, D1): a speech-to-text turn can
    start a little after the speech, and a second of the other speaker is harmless context.
    Batch 14's rep-added finding uses it too (R1)."""
    return max(0, round(turn_start_ms + into_ms - limits.QUOTE_LEAD_IN_MS))


def _found(
    match: Literal["exact", "fuzzy"],
    i: int,
    start: int,
    end: int,
    score: float,
    transcript: Transcript,
) -> Verified:
    """The match mapped back to the turn's own text, and when to start playing it."""
    turn, normalised = transcript.turns[i], transcript.normalised[i]
    char_start, char_end = normalised.spans[start][0], normalised.spans[end - 1][1]
    length = max(len(turn.text), 1)
    into = char_start / length * (turn.end_ms - turn.start_ms)
    start_ms = playback_start(turn.start_ms, into)
    return Verified(
        match=match,
        turn_id=turn.turn_id,
        speaker_role=turn.role,
        quote_final=turn.text[char_start:char_end],
        char_start=char_start,
        char_end=char_end,
        start_ms=start_ms,
        score=score,
    )
