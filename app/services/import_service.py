"""Bulk import from .xlsx — template columns, per-row report, credential emails (AC-05..AC-10)."""

from __future__ import annotations

import io

from email_validator import EmailNotValidError, validate_email
from openpyxl import Workbook, load_workbook
from sqlalchemy.orm import Session

from app.models.course import ClassSection
from app.models.import_job import (
    ROW_FAILED,
    ROW_IMPORTED,
    ROW_SKIPPED,
    ImportBatch,
    ImportRow,
)
from app.models.user import VALID_ROLES, User
from app.services import email_service, user_service

# AC-05/AC-06: exact expected columns — NO password column, ever.
TEMPLATE_COLUMNS = ["username", "gmail", "type", "class"]
EXAMPLE_ROW = ["Nguyen Van A", "student001@gmail.com", "student", "SE101"]


def build_template_bytes() -> bytes:
    """AC-06: downloadable template with exact columns + one example row."""
    wb = Workbook()
    ws = wb.active
    ws.title = "accounts"
    ws.append(TEMPLATE_COLUMNS)
    ws.append(EXAMPLE_ROW)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def parse_workbook(data: bytes) -> tuple[list[str], list[dict]]:
    """Return (header, rows). Raises ValueError on bad structure/content."""
    try:
        wb = load_workbook(filename=io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 — openpyxl error types vary
        raise ValueError(f"Cannot read .xlsx file: {exc}")
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows or all(c is None or str(c).strip() == "" for c in rows[0]):
        raise ValueError("The file is empty — first row must be the header.")
    header = [(str(c).strip().lower() if c is not None else "") for c in rows[0]]
    if header != TEMPLATE_COLUMNS:
        raise ValueError(
            f"Invalid template. Expected header exactly: {', '.join(TEMPLATE_COLUMNS)}. "
            "Download the template from the dashboard."
        )
    parsed = []
    for i, r in enumerate(rows[1:], start=2):
        cells = [(str(c).strip() if c is not None else "") for c in list(r)[:4]]
        while len(cells) < 4:
            cells.append("")
        # Skip fully blank rows.
        if all(c == "" for c in cells):
            continue
        parsed.append(
            {"row_number": i, "username": cells[0], "gmail": cells[1], "type": cells[2], "class": cells[3]}
        )
    if not parsed:
        raise ValueError("No data rows found — fill in rows under the header.")
    return header, parsed


def validate_row(row: dict) -> str | None:
    """Return an error message, or None if the row looks valid."""
    if not row["username"]:
        return "Username is empty."
    try:
        validate_email(row["gmail"].strip(), check_deliverability=False)
    except EmailNotValidError:
        return f"Gmail '{row['gmail']}' is not a valid email."
    try:
        user_service.normalize_role(row["type"])
    except user_service.DomainError as exc:
        return str(exc)
    if not row["class"]:
        return "Class is empty — every account must reference an existing class code."
    return None


def import_rows(
    db: Session, filename: str, rows: list[dict], created_by_id: int | None
) -> ImportBatch:
    """Create batch + per-row results. AC-07: random password per account,
    emailed to that gmail; the admin never sees generated passwords (they are
    only queued in the outbox body, never returned by any API)."""
    batch = ImportBatch(filename=filename, created_by_id=created_by_id, total_rows=len(rows))
    db.add(batch)
    db.commit()
    db.refresh(batch)

    imported = skipped = failed = 0
    # Preload valid class codes for fast validation.
    class_codes = {c.code for c in db.query(ClassSection).all()}

    for row in rows:
        error = validate_row(row)
        if error is None and row["class"].strip() not in class_codes:
            cls = row["class"].strip()
            error = f"Class '{cls}' does not exist — create it first."
        status = ROW_IMPORTED
        message = "Imported. Credentials emailed."
        user_id: int | None = None

        if error is not None:
            status, message = ROW_FAILED, error
            failed += 1
        elif db.query(User).filter(User.email == row["gmail"].strip().lower()).first():
            # AC-10: skipped because the email already exists.
            status, message = ROW_SKIPPED, f"Email '{row['gmail'].strip().lower()}' already exists — skipped."
            skipped += 1
        else:
            try:
                user, plain = user_service.create_user(
                    db,
                    username=row["username"],
                    email=row["gmail"],
                    role_raw=row["type"],
                    class_code=row["class"],
                )
                user_id = user.id
                subject, body = email_service.credential_email_body(
                    user.username, user.email, plain, row["class"].strip()
                )
                email_service.queue_email(db, user.email, subject, body, user_id=user.id)
                imported += 1
            except user_service.DomainError as exc:
                status, message = ROW_FAILED, str(exc)
                failed += 1

        db.add(
            ImportRow(
                batch_id=batch.id,
                row_number=row["row_number"],
                username=row["username"],
                email=row["gmail"].strip().lower(),
                role_raw=row["type"],
                class_raw=row["class"],
                status=status,
                message=message,
                user_id=user_id,
            )
        )
        db.commit()

    batch.imported_count = imported
    batch.skipped_count = skipped
    batch.failed_count = failed
    db.commit()
    db.refresh(batch)
    # AC-09: credential emails stay queued — the admin (or a worker) drains
    # the outbox via POST /api/admin/email-queue/process at the limited rate.
    # Never send inline here: that would bypass the rate limit for a whole class.
    return batch


def retry_row(db: Session, row_id: int) -> ImportRow:
    """AC-10: re-send a failed row without re-importing the whole file."""
    row = db.query(ImportRow).filter(ImportRow.id == row_id).first()
    if row is None:
        raise ValueError("Import row does not exist.")
    if row.status != ROW_FAILED:
        raise ValueError("Only failed rows can be retried.")
    error = validate_row(
        {"username": row.username, "gmail": row.email, "type": row.role_raw, "class": row.class_raw}
    )
    if error is None and db.query(ClassSection).filter(ClassSection.code == row.class_raw).first() is None:
        error = f"Class '{row.class_raw}' does not exist — create it first."
    if error is not None:
        row.message = error
        db.commit()
        db.refresh(row)
        return row
    if db.query(User).filter(User.email == row.email).first():
        row.status = ROW_SKIPPED
        row.message = f"Email '{row.email}' already exists — skipped."
        batch = db.query(ImportBatch).filter(ImportBatch.id == row.batch_id).first()
        if batch is not None:
            batch.skipped_count += 1
            batch.failed_count = max(0, batch.failed_count - 1)
        db.commit()
        db.refresh(row)
        return row
    try:
        user, plain = user_service.create_user(
            db, username=row.username, email=row.email, role_raw=row.role_raw, class_code=row.class_raw
        )
        subject, body = email_service.credential_email_body(user.username, user.email, plain, row.class_raw)
        email_service.queue_email(db, user.email, subject, body, user_id=user.id)
        row.status = ROW_IMPORTED
        row.message = "Imported on retry. Credentials emailed."
        row.user_id = user.id
        batch = db.query(ImportBatch).filter(ImportBatch.id == row.batch_id).first()
        if batch is not None:
            batch.imported_count += 1
            batch.failed_count = max(0, batch.failed_count - 1)
        db.commit()
        # Queued for the rate-limited drain (AC-09) — same as the initial import.
    except user_service.DomainError as exc:
        row.message = str(exc)
        db.commit()
    db.refresh(row)
    return row


def valid_role_names() -> list[str]:
    # Importable roles only — admins are never bulk-imported (AC-03).
    return [r for r in VALID_ROLES if r != "admin"]
