"""Backfill course-shared versioning onto existing document rows (plan v3 Phase 2a).

Safe default: every old row is attached to its course so the whole subject
shares it. Each row gets one v1 version
(review_note carried over, D7) + one backfill event. Name clashes across
classes of one course are flagged (``needs_review``) for a human decision and
never auto-merged.

Usage:
    uv run python -m app.core.backfill_versions --dry-run   # report only
    uv run python -m app.core.backfill_versions --apply     # one transaction
"""

import argparse
import logging
import unicodedata

from sqlalchemy.orm import Session

from app.models.course import Class
from app.models.document import Document, DocumentEvent, DocumentVersion

logger = logging.getLogger(__name__)

BACKFILL_EVENT = "backfill"


def normalize_name(file_name: str) -> str:
    """Version-group identity (D3): lowercase + trim + Unicode NFC."""

    return unicodedata.normalize("NFC", file_name or "").strip().lower()


def _course_of(db: Session) -> dict:
    return {c.id: c.course_id for c in db.query(Class).all()}


def _already_done(db: Session) -> set:
    return {v.document_id for v in db.query(DocumentVersion.document_id).distinct()}


def plan(db: Session) -> dict:
    """Compute what apply() would do, without writing anything."""
    course_of = _course_of(db)
    done = _already_done(db)
    rows = db.query(Document).order_by(Document.created_at).all()

    # Clash = same normalized name in the same course on different rows.
    seen: dict[tuple, list] = {}
    for row in rows:
        course_id = course_of.get(row.class_id)
        if course_id is None:
            continue
        seen.setdefault((str(course_id), normalize_name(row.file_name)), []).append(row.id)
    clash_ids = {doc_id for ids in seen.values() if len(ids) > 1 for doc_id in ids}

    targets = []
    skipped = 0
    for row in rows:
        if row.id in done:
            skipped += 1
            continue
        course_id = course_of.get(row.class_id)
        if course_id is None:
            skipped += 1
            continue
        targets.append(
            {
                "document_id": row.id,
                "file_name": row.file_name,
                "course_id": course_id,
                "needs_review": row.id in clash_ids,
            }
        )
    clash_groups = [
        {
            "course_id": course,
            "name": name,
            "files": sorted(
                {r.file_name for r in rows if str(course_of.get(r.class_id)) == course}
            ),
        }
        for (course, name), ids in sorted(seen.items())
        if len(ids) > 1
    ]
    return {
        "total": len(rows),
        "to_backfill": len(targets),
        "skipped": skipped,
        "clash_groups": clash_groups,
        "targets": targets,
    }


def dry_run(db: Session) -> dict:
    """Report only; rolls back any incidental flush."""

    report = plan(db)
    db.rollback()
    logger.info(
        "Backfill dry-run: %d rows, %d to backfill, %d skipped, %d clash groups.",
        report["total"],
        report["to_backfill"],
        report["skipped"],
        len(report["clash_groups"]),
    )
    for group in report["clash_groups"]:
        logger.info("  clash in course %s: %s", group["course_id"], group["files"])
    return report


def apply(db: Session) -> dict:
    """Backfill in a single transaction; rolls back fully on error."""

    report = plan(db)
    try:
        by_id = {t["document_id"]: t for t in report["targets"]}
        rows = (
            db.query(Document).filter(Document.id.in_(by_id)).order_by(Document.created_at).all()
            if by_id
            else []
        )
        for row in rows:
            target = by_id[row.id]
            row.course_id = target["course_id"]
            row.created_by = row.uploaded_by
            row.name_normalized = normalize_name(row.file_name)
            row.needs_review = target["needs_review"]
            version = DocumentVersion(
                document_id=row.id,
                version_no=1,
                status=row.status,
                review_note=row.review_note,
                uploaded_by=row.uploaded_by,
            )
            db.add(version)
            db.flush()
            row.current_version_id = version.id
            db.add(
                DocumentEvent(
                    document_id=row.id,
                    version_no=1,
                    action=BACKFILL_EVENT,
                    detail="Phase 2a backfill as course-shared v1.",
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    logger.info("Backfilled %d documents as course-shared v1.", report["to_backfill"])
    return report


if __name__ == "__main__":
    import sys

    from app.config import settings
    from app.core.database import get_session_local, init_engine

    parser = argparse.ArgumentParser(description="Backfill document versioning (plan v3 Phase 2a).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    group.add_argument("--apply", action="store_true", help="backfill in one transaction")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    init_engine(settings.database_url)
    session = get_session_local(settings.database_url)()
    try:
        report = apply(session) if args.apply else dry_run(session)
        print(
            f"total={report['total']} to_backfill={report['to_backfill']} "
            f"skipped={report['skipped']} clash_groups={len(report['clash_groups'])}"
        )
    finally:
        session.close()
        sys.stdout.flush()
