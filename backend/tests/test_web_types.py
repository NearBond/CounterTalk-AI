"""web/src/types/api.ts mirrors the API's schemas (CODING_GUIDELINES §2.11, §4.4).

The types are written by hand, so this checks every mirrored interface has exactly the
schema's fields: a field added, removed or renamed on one side only fails here.
"""

import re
from dataclasses import fields, is_dataclass

import pytest
from pydantic import BaseModel

from countertalk import limits, narrative, storage, summaries
from countertalk.api.ask import AskIn, AskOut, ChipOut
from countertalk.api.auth import MeOut, SignInIn
from countertalk.api.catalog import (
    AliasIn,
    AliasOut,
    ItemChange,
    ItemIn,
    ItemOut,
    NameReviewOut,
    NewItem,
    Resolved,
    ResolveIn,
)
from countertalk.api.health import HealthOut
from countertalk.api.insights import (
    ChoiceOut,
    InsightChange,
    InsightOut,
    NewInsight,
    NewNote,
    NoteOut,
    OrderItemOut,
    QuoteOut,
    ReportOut,
    RouteOut,
    RouteSeen,
    RouteTo,
    TaskOut,
    TeamFor,
    TeamsChoice,
)
from countertalk.api.issues import (
    FindingOut,
    IssueChange,
    IssueDetailOut,
    IssueOut,
    MemberOut,
    MergeIn,
    NamedOut,
    OptionsOut,
    OtherIssueOut,
    PeriodOut,
    SuggestionOut,
    TeamOut,
)
from countertalk.api.me import CountsOut, HomeOut, ShopFixedOut
from countertalk.api.shops import FixedOut, ShopOut
from countertalk.api.summaries import (
    DrillOut,
    DrillRepOut,
    DrillShopOut,
    DrillVisitOut,
    SummaryAsk,
    SummaryOut,
    SummaryPageOut,
)
from countertalk.api.visits import (
    CompleteIn,
    ReviewOut,
    SegmentOut,
    TranscriptOut,
    TurnOut,
    VisitIn,
    VisitOut,
)
from countertalk.config import BACKEND_DIR

TYPES = BACKEND_DIR.parent / "web" / "src" / "types" / "api.ts"

MIRRORS: dict[str, type[BaseModel]] = {
    "Health": HealthOut,
    "Me": MeOut,
    "CatalogAlias": AliasOut,
    "CatalogItem": ItemOut,
    "CatalogNames": NewItem,
    "NewCatalogItem": ItemIn,
    "CatalogItemChange": ItemChange,
    "NewAlias": AliasIn,
    "NameReview": NameReviewOut,
    "ResolveName": ResolveIn,
    "Resolved": Resolved,
    "SignIn": SignInIn,
    "Shop": ShopOut,
    "FixedItem": FixedOut,
    "ShopFixed": ShopFixedOut,
    "MyCounts": CountsOut,
    "MyHome": HomeOut,
    "Visit": VisitOut,
    "NewVisit": VisitIn,
    "CompleteVisit": CompleteIn,
    "Segment": SegmentOut,
    "Transcript": TranscriptOut,
    "Turn": TurnOut,
    "Quote": QuoteOut,
    "Route": RouteOut,
    "Insight": InsightOut,
    "InsightChange": InsightChange,
    "TeamsChoice": TeamsChoice,
    "NewInsight": NewInsight,
    "NewNote": NewNote,
    "Note": NoteOut,
    "Report": ReportOut,
    "Task": TaskOut,
    "OrderItem": OrderItemOut,
    "Choice": ChoiceOut,
    "Review": ReviewOut,
    "Team": TeamOut,
    "Named": NamedOut,
    "InboxPeriod": PeriodOut,
    "AskIn": AskIn,
    "AskChip": ChipOut,
    "AskAnswer": AskOut,
    "InboxOptions": OptionsOut,
    "Issue": IssueOut,
    "Member": MemberOut,
    "IssueDetail": IssueDetailOut,
    "IssueChange": IssueChange,
    "Finding": FindingOut,
    "RouteSeen": RouteSeen,
    "RouteTo": RouteTo,
    "TeamFor": TeamFor,
    "Summary": SummaryOut,
    "SummaryAsk": SummaryAsk,
    "SummaryPage": SummaryPageOut,
    "Suggestion": SuggestionOut,
    "OtherIssue": OtherIssueOut,
    "MergeIn": MergeIn,
    "Drill": DrillOut,
    "DrillRep": DrillRepOut,
    "DrillShop": DrillShopOut,
    "DrillVisit": DrillVisitOut,
}


def interface_fields(source: str, name: str) -> set[str]:
    found = re.search(rf"export interface {name} \{{(.*?)\n\}}", source, re.DOTALL)
    assert found is not None, f"types/api.ts has no interface {name}"
    return set(re.findall(r"^\s+(\w+)\??:", found.group(1), re.MULTILINE))


@pytest.mark.parametrize("name", MIRRORS)
def test_the_web_type_has_exactly_the_schemas_fields(name: str) -> None:
    source = TYPES.read_text(encoding="utf-8")

    assert interface_fields(source, name) == set(MIRRORS[name].model_fields)


# Inside the summaries' JSON (batch 15b): the numbers' dataclasses and the narrative's TypedDicts.
SHAPES: dict[str, type] = {
    "SummaryPeriod": summaries.Period,
    "Measures": summaries.Measures,
    "IssueNumbers": summaries.IssueNumbers,
    "Totals": summaries.Totals,
    "TeamNumbers": summaries.TeamNumbers,
    "TeamWork": summaries.TeamWork,
    "CauseNumbers": summaries.CauseNumbers,
    "CategoryArea": summaries.CategoryArea,
    "CompetitorCount": summaries.CompetitorCount,
    "OrgNumbers": summaries.OrgNumbers,
    "Citation": narrative.Citation,
    "Sentence": narrative.Sentence,
    "IssueWords": narrative.IssueWords,
    "Narrative": narrative.Narrative,
}


@pytest.mark.parametrize("name", SHAPES)
def test_the_web_type_has_exactly_the_shape_s_fields(name: str) -> None:
    source = TYPES.read_text(encoding="utf-8")
    shape = SHAPES[name]
    found = {f.name for f in fields(shape)} if is_dataclass(shape) else set(shape.__annotations__)

    assert interface_fields(source, name) == found


def test_the_web_app_mirrors_the_servers_upload_limits() -> None:
    # The Upload panel cuts files at the server's segment limit, and offers only the types it
    # takes; the server refuses anything else.
    source = TYPES.read_text(encoding="utf-8")
    exts = re.search(r"export const ALLOWED_EXTS = \[(.*?)\] as const;", source)
    limit = re.search(r"export const MAX_SEGMENT_BYTES = ([\d_]+);", source)

    assert exts is not None and limit is not None
    assert tuple(re.findall(r'"(\w+)"', exts.group(1))) == storage.ALLOWED_EXTS
    assert int(limit.group(1)) == limits.MAX_SEGMENT_BYTES


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NOTE_MAX_CHARS", limits.NOTE_MAX_CHARS),
        ("SUMMARY_MAX_CHARS", limits.SUMMARY_MAX_CHARS),
        ("RESOLUTION_NOTE_MAX_CHARS", limits.RESOLUTION_NOTE_MAX_CHARS),
        ("PERIOD_FIRST_YEAR", limits.PERIOD_FIRST_YEAR),
        ("ASK_QUESTION_MAX_CHARS", limits.ASK_QUESTION_MAX_CHARS),
        ("PERIOD_LAST_YEAR", limits.PERIOD_LAST_YEAR),
    ],
)
def test_the_web_app_mirrors_the_server_s_limit(name: str, value: int) -> None:
    source = TYPES.read_text(encoding="utf-8")
    limit = re.search(rf"export const {name} = ([0-9_]+);", source)

    assert limit is not None and int(limit.group(1)) == value
