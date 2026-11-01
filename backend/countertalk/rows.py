"""One of an org's rows by its ID (R1): the one way a module finds or locks one, so another
org's is 404, never 403 (§2.10), and a locked row is read as it is now, even if the session
loaded it before the lock (R6). Added in batch 15, when R1 came up in two parts in a row."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from countertalk.errors import NotFound
from countertalk.models import parse_id


def find_one[Row](
    session: Session,
    model: type[Row],
    org_id: str,
    row_id: str,
    missing: NotFound,
    *,
    for_update: bool = False,
) -> Row:
    """The row of this org with this ID, or `missing` raised: a malformed ID is missing too."""
    found = parse_id(row_id)
    row = None
    if found is not None:
        table: Any = model
        query = select(model).where(table.id == found, table.org_id == org_id)
        if for_update:
            query = query.with_for_update()
        row = session.scalars(query.execution_options(populate_existing=True)).one_or_none()
    if row is None:
        raise missing
    return row
