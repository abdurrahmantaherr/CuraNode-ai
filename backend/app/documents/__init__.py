"""Medical-document processing (FR10–FR13).

ONE pipeline for every document type:

    upload -> temporary -> common OCR -> type-specific extraction
           -> review / edit -> Confirm & Save -> finalized record

Only `extractors.py` varies by document type. Storage, OCR, the status
lifecycle, review and finalization are shared, and `service.py` is the single
place every business rule lives — the JSON API (`router.py`) and the
server-rendered pages (`web/documents.py`) both call it.
"""
