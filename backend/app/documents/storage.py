"""Original-file storage on Supabase Storage (the private `medical-documents`
bucket that already exists in the shared project).

Database rows hold metadata and state; this module holds the bytes. The bucket
is private and the app reads files back itself after an ownership check, so no
public or signed URL is ever handed to a browser.

The Supabase client is looked up through `security.get_supabase_client` at call
time (not imported by name) so tests can substitute a fake object store.
"""

from __future__ import annotations

from ..identity import security
from ..log_config import get_logger
from ..settings import settings

log = get_logger()

# Magic-number -> (canonical MIME type, file extension). The file's own bytes
# decide what it is; the client-declared type and filename are only hints.
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"%PDF-", "application/pdf", "pdf"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
)
ALLOWED_MIME_TYPES = frozenset(mime for _, mime, _ in _SIGNATURES)


def sniff(data: bytes) -> tuple[str, str] | None:
    """`(mime_type, extension)` for a PDF / JPEG / PNG, else None."""
    for prefix, mime, ext in _SIGNATURES:
        if data.startswith(prefix):
            return mime, ext
    return None


def object_key(patient_id: object, document_id: object, extension: str) -> str:
    """Stable key, never derived from the user's filename (so a hostile
    filename can never influence a storage path)."""
    return f"patients/{patient_id}/{document_id}.{extension}"


async def _bucket():  # type: ignore[no-untyped-def]
    client = await security.get_supabase_client()
    return client.storage.from_(settings.document_bucket)


async def put(key: str, data: bytes, content_type: str) -> None:
    bucket = await _bucket()
    await bucket.upload(key, data, {"content-type": content_type, "upsert": "false"})


async def get(key: str) -> bytes:
    bucket = await _bucket()
    return await bucket.download(key)


def is_missing(exc: BaseException) -> bool:
    """Whether a storage error means the OBJECT IS NOT THERE (as opposed to the store being
    unreachable or refusing us). The real client raises `StorageApiError` with a status / code /
    message; a test double raises `KeyError`. Anything unrecognised counts as "unavailable" —
    the safe reading, because "not found" tells the patient their file is gone."""
    if isinstance(exc, (KeyError, FileNotFoundError)):
        return True
    status = str(getattr(exc, "status", "")).strip()
    code = str(getattr(exc, "code", "")).strip().lower()
    message = str(getattr(exc, "message", "") or exc).lower()
    return (
        status == "404"
        or code in {"not_found", "nosuchkey", "404"}
        or "object not found" in message
        or "not found" in message
    )


def describe_failure(exc: BaseException) -> dict[str, object]:
    """What may be LOGGED about a storage failure, so a 502 can be told apart: the exception class
    (`ConnectError`, `ReadTimeout`, `StorageApiError`...), the class of what ultimately caused it
    (`gaierror` is a DNS failure) and, if the store answered, its numeric HTTP status. Never the
    message, the key or the bytes — a message can carry paths and file names."""
    info: dict[str, object] = {"error": type(exc).__name__}
    root: BaseException = exc
    for _ in range(5):  # a cause chain is short; never loop on a cycle
        parent = root.__cause__ or root.__context__
        if parent is None or parent is root:
            break
        root = parent
    if root is not exc:
        info["cause"] = type(root).__name__
    status = str(getattr(exc, "status", "")).strip()
    if status.isdigit():
        info["status"] = int(status)
    return info


async def remove(key: str) -> None:
    """Delete the original and RAISE if the store did not confirm — for the one caller (an
    explicit patient deletion) that must never report success for a file that is still there.
    Removing a key that is already gone is not an error (the store answers with an empty list),
    which is what makes a retried deletion safe."""
    bucket = await _bucket()
    await bucket.remove([key])


async def delete(key: str) -> None:
    """Best-effort: a failed delete is logged, never raised — the caller has
    already committed the state change that made the file unnecessary."""
    try:
        bucket = await _bucket()
        await bucket.remove([key])
    except Exception as exc:  # noqa: BLE001 - cleanup must not fail the request
        log.warning("document_storage_delete_failed", **describe_failure(exc))
