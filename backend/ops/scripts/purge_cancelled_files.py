"""Remove the stored original of every discarded or deleted document.

Discarding an upload used to remove its file on a best-effort basis: if the store was unreachable
at that moment the document was still marked discarded, and its file was left behind with nothing
in the application pointing at it. Discarding and deleting now confirm the removal before they
report success (`documents/service.py::_retire`), so nothing new is orphaned — this clears what
the old behaviour already left.

It only ever touches the file of a document whose status is `cancelled` (a discarded upload, or a
deleted document's tombstone), never a record, an upload still in progress, or anything else.
Removing a file that is already gone is not an error, so running it again is harmless.

    uv run python backend/ops/scripts/purge_cancelled_files.py            # dry run: how many
    uv run python backend/ops/scripts/purge_cancelled_files.py --apply    # remove them
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Runnable directly, like the other scripts here.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db.models import Document, DocumentStatus
from app.db.session import SessionFactory
from app.documents import storage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def purge_cancelled_files(session: AsyncSession, *, apply: bool) -> tuple[int, int, int]:
    """`(cancelled documents, files removed, failures)`. With `apply` false nothing is removed
    and the second figure is 0."""
    keys = (
        await session.execute(
            select(Document.object_key).where(Document.status == DocumentStatus.CANCELLED.value)
        )
    ).scalars()
    checked = removed = failed = 0
    for key in keys.all():
        checked += 1
        if not apply:
            continue
        try:
            await storage.remove(key)
            removed += 1
        except Exception:  # noqa: BLE001 - report it, carry on with the rest
            failed += 1
    return checked, removed, failed


async def main(apply: bool) -> int:
    async with SessionFactory() as session:
        checked, removed, failed = await purge_cancelled_files(session, apply=apply)
    if apply:
        print(f"{checked} discarded/deleted documents; {removed} files removed; {failed} failed")
    else:
        print(f"{checked} discarded/deleted documents would be checked (dry run: use --apply)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--apply" in sys.argv[1:])))
