"""Reprocessing (batch 13; BRIEF §6): a visit in review or failed goes back a step and runs
again, before it's confirmed. The one way back (R1): the API's POST /api/visits/{id}/reprocess,
scripts/reprocess_failed.py and extract_visit.py --again all come here.

- From `extract`: back to `transcribed`, with an extract job queued. The transcript and saved
  roles are reused: only extraction is paid for, never speech-to-text.
- From `transcribe`: back to `uploaded`, with a transcribe job queued, for a new transcript.
  The upload must have finished. Speech-to-text is paid for again for a visit that has a
  transcript: the job removes the replies that made it, as it starts (transcription.py). A
  visit that failed part-way through transcribing keeps the replies already paid for.

Checked and queued in one transaction under the visit's lock (§2.3), so a confirm can't come
in between, and a visit is never left back a step with nothing queued (§9.1). One with a job
still active, of either kind, is refused: two jobs never race on a visit.
"""

import logging
from typing import Literal

from sqlalchemy.orm import Session

from countertalk import jobs, visits
from countertalk.errors import Conflict
from countertalk.models import JobKind, Visit, VisitStatus

log = logging.getLogger(__name__)

From = Literal["transcribe", "extract"]
STEPS: tuple[From, ...] = ("transcribe", "extract")
PAYS_AGAIN = "From transcribe, speech-to-text is paid for again for a visit that has a transcript"


def reprocess(session: Session, org_id: str, visit_id: str, start: From) -> Visit:
    visit = visits.lock_visit(session, org_id, visit_id)
    if visit.status == VisitStatus.CONFIRMED:
        raise Conflict("visit_confirmed", "A confirmed visit can't be processed again")
    active = visits.processing_visit_ids(session, [visit.id])
    if visit.status not in (VisitStatus.REVIEW, VisitStatus.FAILED) or active:
        raise Conflict("visit_processing", "The visit is still being processed")
    if start == "extract":
        if visit.transcript_id is None:
            raise Conflict("no_transcript", "The visit has no transcript to extract")
        visits.set_status(visit, VisitStatus.TRANSCRIBED)
        kind = JobKind.EXTRACT
    else:
        if visit.completed_at is None:
            raise Conflict(
                "upload_not_finished", "Its upload never finished, so it can't be transcribed"
            )
        visits.set_status(visit, VisitStatus.UPLOADED)
        kind = JobKind.TRANSCRIBE
    queued = jobs.enqueue(session, kind, visit_id=visit.id)
    if queued is None:  # checked above, so only a race the lock prevents; never moved alone
        raise Conflict("visit_processing", "The visit is still being processed")
    log.info("reprocess visit=%s from=%s job=%s", visit.id, start, queued)
    return visit
