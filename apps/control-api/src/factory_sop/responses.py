"""The one list envelope every control-plane listing is served in (§5.15).

`{items, page, page_size, total}` is a cross-module wire convention, which is why it lives
beside `problem.py` as shared infrastructure: each module's adapter composes its own item type
into it, and a client that learns the envelope once reads every list the backend serves. It owns
no domain behavior and imports no domain module.

Shared infrastructure, like `observability`.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

# The bounds a listing request may state. `page_size` has a ceiling because an unbounded
# `page_size=1000000` is a denial-of-service on the database for a listing the Web renders
# fifty rows at a time.
DEFAULT_PAGE_SIZE = 50
MAXIMUM_PAGE_SIZE = 200


class ItemPage[ItemT](BaseModel):
    """One page of `items`, and the counts that make the pager renderable."""

    items: list[ItemT]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total: int = Field(ge=0)


def paginate[ItemT](
    items: Sequence[ItemT], *, page: int, page_size: int
) -> tuple[list[ItemT], int]:
    """Slice `items` to the requested page and report the unpaginated total.

    Returns the slice and the total rather than an `ItemPage`, so the caller's item type stays
    with the route that knows it. Slicing in the adapter rather than a `LIMIT` in the store is
    deliberate for this slice: a shop floor's staff and role lists are tens of rows, and the
    seam to change when that stops being true is the listing use case, not every caller.
    """
    total = len(items)
    start = (page - 1) * page_size
    return list(items[start : start + page_size]), total
