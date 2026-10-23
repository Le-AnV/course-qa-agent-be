"""Bulk import from .xlsx — template columns, per-row report, credential emails (AC-05..AC-10)."""

from __future__ import annotations

import io
import uuid

from email_validator import EmailNotValidError, validate_email
from openpyxl import Workbook, load_workbook
from sqlalchemy.orm import Session

from app.models.import_job import (
    ROW_FAILED,
    ROW_IMPORTED,
    ROW_SKIPPED,
    ImportBatch,
    ImportRow,
)
from app.models.user import User
from app.services import email_service, user_service

# AC-05/AC-06: exact expected columns — NO password or code columns, ever.
# Class assignment is deliberately NOT part of the template: students
# browse the catalog and request to join; TAs approve (enrollment flow).
# Accounts are created class-less; import never enrolls anyone.
# MSSV/MSNV are generated from full name: Tran Van A -> TVA_4821, with a
# random 4-digit suffix. Older 4/5-column files are still accepted so
# existing spreadsheets keep working.
TEMPLATE_COLUMNS = ["fullname", "email", "type"]
LEGACY_COLUMNS = [
    ["fullname", "email", "type", "mssv"],
    ["fullname", "email", "type", "msnv"],
    ["fullname", "email", "type", "mssv", "msnv"],
]
EXAMPLE_ROW = ["Nguyen Van A", "student001@gmail.com", "student"]

GUIDE_SHEET = "Hướng dẫn"
GUIDE_ROWS = [
    ["Cột", "Bắt buộc?", "Cách nhập"],
    ["fullname", "Có", "Họ tên đầy đủ, vd: Tran Van A. Dùng để sinh MSSV/MSNV tự động."],
    ["email", "Có", "Gmail của tài khoản, phải duy nhất, vd: student001@gmail.com. Trùng email có sẵn thì bỏ qua (skipped)."],
    ["type", "Có", "Một trong: student, trainer, teaching_assistant (hoặc ta). Chữ thường, không dấu."],
    ["Ghi chú", "", "Không thêm cột MSSV/MSNV hay password — hệ thống tự sinh MSSV/MSNV từ họ tên và gửi mật khẩu qua email. Sheet này chỉ để đọc, hệ thống chỉ đọc sheet accounts."],
]


def build_template_bytes() -> bytes:
    """AC-06: downloadable template with exact columns + one example row + guide sheet."""
    wb = Workbook()
    ws = wb.active
    ws.title = "accounts"
    ws.append(TEMPLATE_COLUMNS)
    ws.append(EXAMPLE_ROW)
    for col, width in (("A", 24), ("B", 28), ("C", 20)):
        ws.column_dimensions[col].width = width
    guide = wb.create_sheet(GUIDE_SHEET)
    for row in GUIDE_ROWS:
        guide.append(row)
    for col, width in (("A", 12), ("B", 14), ("C", 90)):
        guide.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def parse_workbook(data: bytes) -> tuple[list[str], list[dict]]:
    """Return (header, rows). Raises ValueError on bad structure/content."""
    try:
        wb = load_workbook(filename=io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 — openpyxl error types vary
        raise ValueError(f"Cannot read .xlsx file: {exc}")
    ws = wb["accounts"] if "accounts" in wb.sheetnames else wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows or all(c is None or str(c).strip() == "" for c in rows[0]):
        raise ValueError("The file is empty — first row must be the header.")
    header = [(str(c).strip().lower() if c is not None else "") for c in rows[0]]
    while header and header[-1] == "":
        header.pop()
    if header not in [TEMPLATE_COLUMNS, *LEGACY_COLUMNS]:
        raise ValueError(
            f"Invalid template. Expected header exactly: {', '.join(TEMPLATE_COLUMNS)}. "
            "Download the latest template from the dashboard."
        )
    parsed = []
    for i, r in enumerate(rows[1:], start=2):
        cells = [(str(c).strip() if c is not None else "") for c in list(r)]
        while len(cells) < len(header):
            cells.append("")
        # Skip fully blank rows.
        if all(c == "" for c in cells):
            continue
        row = {"row_number": i, "fullname": "", "email": "", "type": "", "mssv": "", "msnv": ""}
        for index, key in enumerate(header):
            row[key] = cells[index]
        parsed.append(row)
    if not parsed:
        raise ValueError("No data rows found — fill in rows under the header.")
    return header, parsed


def _handle_existing_email(db: Session, row: dict) -> tuple[str, str, uuid.UUID | None]:
    """Import carries no class, so a repeated email is always a duplicate.

    Returns (status, message, user_id). Same role -> skipped; a conflicting
    role fails loudly instead of silently hiding the conflict.
    """
    email = row["email"].strip().lower()
    user = db.query(User).filter(User.email == email).first()
    want_role = user_service.normalize_role(row["type"])
    got_role = user_service.role_name_of(user)
    if not got_role:
        from app.models.user import Role

        role_row = db.query(Role).filter(Role.id == user.role_id).first()
        got_role = role_row.name if role_row else ""
    if got_role != want_role:
        return (
            ROW_FAILED,
            f"Email '{email}' already exists with role '{got_role.lower()}' — row says '{row['type']}'.",
            user.id,
        )
    return ROW_SKIPPED, f"Email '{email}' already exists — skipped.", user.id


def validate_row(db: Session, row: dict) -> str | None:
    """Return an error message, or None if the row looks valid."""
    if not row["fullname"]:
        return "Fullname is empty."
    try:
        validate_email(row["email"].strip(), check_deliverability=False)
    except EmailNotValidError:
        return f"Email '{row['email']}' is not a valid email."
    try:
        role = user_service.normalize_role(row["type"])
    except user_service.DomainError as exc:
        return str(exc)
    if (row.get("mssv") or "").strip():
        try:
            code = user_service.normalize_student_code(row["mssv"])
        except user_service.DomainError as exc:
            return str(exc)
        if db.query(User).filter(User.student_code == code).first():
            return f"Student code '{code}' already exists."
    if (row.get("msnv") or "").strip():
        if role == "student":
            return "MSNV is only for staff accounts."
        try:
            staff = user_service.normalize_staff_code(row["msnv"])
        except user_service.DomainError as exc:
            return str(exc)
        if db.query(User).filter(User.staff_code == staff).first():
            return f"Staff code '{staff}' already exists."
    return None


def import_rows(
    db: Session, filename: str, rows: list[dict], created_by_id: uuid.UUID | str | None
) -> ImportBatch:
    """Create batch + per-row results. AC-07: random password per account,
    emailed to that gmail; the admin never sees generated passwords (they are
    only queued in the outbox body, never returned by any API)."""
    batch = ImportBatch(
        filename=filename,
        created_by_id=(
            created_by_id
            if isinstance(created_by_id, uuid.UUID) or created_by_id is None
            else uuid.UUID(str(created_by_id))
        ),
        total_rows=len(rows),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)

    imported = skipped = failed = 0

    for row in rows:
        error = validate_row(db, row)
        status = ROW_IMPORTED
        message = "Imported. Credentials emailed."
        user_id: uuid.UUID | None = None
        student_code: str | None = None
        staff_code: str | None = None

        if error is not None:
            status, message = ROW_FAILED, error
            failed += 1
        elif db.query(User).filter(User.email == row["email"].strip().lower()).first():
            # Import carries no class: a repeated email is a duplicate.
            # Class membership now happens via request + TA approval.
            status, message, user_id = _handle_existing_email(db, row)
            if status == ROW_IMPORTED:
                imported += 1
            elif status == ROW_SKIPPED:
                skipped += 1
            else:
                failed += 1
        else:
            try:
                user, plain = user_service.create_user(
                    db,
                    username=row["fullname"],
                    email=row["email"],
                    role_raw=row["type"],
                    class_code=None,
                    student_code=row.get("mssv") or None,
                    staff_code=row.get("msnv") or None,
                )
                user_id = user.id
                student_code = user.student_code
                staff_code = user.staff_code
                subject, body = email_service.credential_email_body(
                    user.full_name, user.email, plain, None
                )
                email_service.queue_email(db, user.email, subject, body, user_id=user.id)
                if student_code:
                    message = f"Imported (MSSV {student_code}). Credentials emailed."
                elif staff_code:
                    message = f"Imported (MSNV {staff_code}). Credentials emailed."
                imported += 1
            except user_service.DomainError as exc:
                status, message = ROW_FAILED, str(exc)
                failed += 1

        db.add(
            ImportRow(
                batch_id=batch.id,
                row_number=row["row_number"],
                username=row["fullname"],
                email=row["email"].strip().lower(),
                role_raw=row["type"],
                class_raw="",
                status=status,
                message=message,
                user_id=user_id,
                student_code=student_code,
                staff_code=staff_code,
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
        db,
        {
            "fullname": row.username,
            "email": row.email,
            "type": row.role_raw,
            "mssv": row.student_code or "",
            "msnv": row.staff_code or "",
        },
    )
    if error is not None:
        row.message = error
        db.commit()
        db.refresh(row)
        return row
    if db.query(User).filter(User.email == row.email).first():
        status, message, user_id = _handle_existing_email(
            db, {"fullname": row.username, "email": row.email, "type": row.role_raw}
        )
        row.user_id = user_id
        batch = db.query(ImportBatch).filter(ImportBatch.id == row.batch_id).first()
        if status == ROW_IMPORTED:
            row.status = ROW_IMPORTED
            row.message = "Imported on retry. " + message
            if batch is not None:
                batch.imported_count += 1
                batch.failed_count = max(0, batch.failed_count - 1)
        elif status == ROW_SKIPPED:
            row.status = ROW_SKIPPED
            row.message = message
            if batch is not None:
                batch.skipped_count += 1
                batch.failed_count = max(0, batch.failed_count - 1)
        else:
            row.message = message
        db.commit()
        db.refresh(row)
        return row
    try:
        user, plain = user_service.create_user(
            db, username=row.username, email=row.email, role_raw=row.role_raw, class_code=None,
            student_code=row.student_code or None,
            staff_code=row.staff_code or None,
        )
        subject, body = email_service.credential_email_body(user.full_name, user.email, plain, None)
        email_service.queue_email(db, user.email, subject, body, user_id=user.id)
        row.status = ROW_IMPORTED
        row.student_code = user.student_code
        row.staff_code = user.staff_code
        if user.student_code:
            row.message = f"Imported on retry (MSSV {user.student_code}). Credentials emailed."
        elif user.staff_code:
            row.message = f"Imported on retry (MSNV {user.staff_code}). Credentials emailed."
        else:
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
    # Lowercase aliases for the FE dropdown / .xlsx `type` column.
    return [r for r in ("student", "trainer", "teaching_assistant") if r]
