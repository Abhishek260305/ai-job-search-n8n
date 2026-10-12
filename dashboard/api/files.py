"""Which stored file paths the dashboard may serve.

Tailoring writes the PDFs under OUTPUT_DIR (applications/<job>/resume.pdf, cover_letter.pdf) and stores their paths
in the database; the dashboard mounts that directory read-only at the same place. A stored path is served only if,
after following every symlink, it is a regular .pdf file inside OUTPUT_DIR and no larger than 20 MB.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("dashboard.files")

MAX_PDF_BYTES = 20 * 1024 * 1024
# Chrome's and Safari's built-in viewers render a PDF only when the document's policy leaves them room, so a PDF gets
# its own policy instead of the app's. No `sandbox`: it blanks Chrome's viewer.
PDF_CSP = "default-src 'none'; object-src 'self'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'"


def resolve(stored: str | None, output_dir: Path, job_id: int = 0) -> Path | None:
    """The real path of a stored file if it may be served, else None (the route answers 404)."""
    if not stored or "\x00" in stored:
        return None
    root = output_dir.resolve()
    candidate = Path(stored)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        real = candidate.resolve(strict=True)
    except (OSError, RuntimeError):            # missing, or a symlink loop
        return None
    reason = None
    if not real.is_relative_to(root):
        reason = "outside the output directory"
    elif not real.is_file():
        reason = "not a regular file"
    elif real.suffix.lower() != ".pdf":
        reason = "not a PDF"
    elif real.stat().st_size > MAX_PDF_BYTES:
        reason = "larger than 20 MB"
    if reason:
        log.warning("refused the stored file of job %s: %s", job_id, reason)
        return None
    return real
