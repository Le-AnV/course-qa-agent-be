"""Document service for Epic E2 (AI-35/37/41): upload validation, list, class tags.

DB-backed (E1 models): class assignments resolve from ``course``/``class``
rows instead of the former static stub, and uploads persist as ``document``
rows with pending status. File bytes are validated then discarded — blob
storage lands with the AI-55 indexing pipeline.
"""

import hashlib
import unicodedata
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.models.course import Class, Course
from app.models.document import (
    Document,
    DocumentClassOptout,
    DocumentEvent,
    DocumentVersion,
)
from app.models.user import User


def normalize_name(file_name: str) -> str:
    """Version-group identity (D3): lowercase + trim + Unicode NFC."""

    return unicodedata.normalize("NFC", file_name or "").strip().lower()


def content_hash(content: bytes) -> str:
    """SHA-256 of the validated bytes (duplicate-content warning, D3)."""

    return hashlib.sha256(content).hexdigest()

# Upload AC-01: 6 accepted material formats (images belong to the E3 vision path).
ACCEPTED_EXTENSIONS: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # Upload AC-01: 100MB per file.

STATUS_PENDING = "pending"  # Displayed as "pending-approval" (Upload AC-02).
STATUS_PROCESSING = "processing"
STATUS_INDEXED = "indexed"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
STATUS_SUPERSEDED = "superseded"  # Replaced by a newer upload, plan v3 Phase 2b.
VALID_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_PROCESSING, STATUS_INDEXED, STATUS_REJECTED, STATUS_FAILED}
)

# Who owns a material (document.scope): the whole subject, or a single class.
SCOPE_COURSE = "course"  # shared with every class of the subject
SCOPE_CLASS = "class"  # private to the class in ``document.class_id``
VALID_SCOPES = frozenset({SCOPE_COURSE, SCOPE_CLASS})


def _role_name(user: User) -> str:
    role = getattr(user, "role", None)
    if role is not None and getattr(role, "name", None):
        return role.name
    return ""


def my_course_ids(db: Session, user: User) -> set[uuid.UUID]:
    """Course ids the caller may see documents of (plan v3 D2/D6).

    Trainers see courses of their own classes; admins see all. Students
    and TAs see none here (student reads go through the student router).
    """
    role = _role_name(user).upper()
    if role == "ADMIN":
        return {c.id for c in db.query(Course).all()}
    if role == "TRAINER":
        return {
            c.course_id for c in db.query(Class).filter(Class.trainer_id == user.id).all()
        }
    return set()


def my_class_ids(db: Session, user: User) -> list[uuid.UUID]:
    """Class ids visible to the caller (Upload AC-05 / List AC-05)."""
    role = _role_name(user).upper()
    if role == "ADMIN":
        return [c.id for c in db.query(Class.id).all()]
    if role == "TRAINER":
        return [c.id for c in db.query(Class.id).filter(Class.trainer_id == user.id).all()]
    return []


def my_classes(db: Session, user: User) -> list[dict]:
    ids = set(my_class_ids(db, user))
    if not ids:
        return []
    rows = db.query(Class).filter(Class.id.in_(ids)).order_by(Class.code).all()
    return [
        {"id": str(c.id), "course_id": str(c.course_id), "name": c.name, "code": c.code}
        for c in rows
    ]


def my_courses(db: Session, user: User) -> list[dict]:
    """Courses the caller is assigned to (teacher Course list).

    One row per course with its class count; admins see every course and
    students/TAs see none (their reads go through the student router).
    """

    role = _role_name(user).upper()
    if role == "ADMIN":
        rows = db.query(Course).order_by(Course.code).all()
    elif role == "TRAINER":
        course_ids = my_course_ids(db, user)
        if not course_ids:
            return []
        rows = db.query(Course).filter(Course.id.in_(course_ids)).order_by(Course.code).all()
    else:
        return []
    out: list[dict] = []
    for course in rows:
        count = db.query(func.count(Class.id)).filter(Class.course_id == course.id).scalar()
        out.append(
            {
                "id": str(course.id),
                "code": course.code,
                "name": course.name,
                "class_count": int(count or 0),
            }
        )
    return out


def record_event(
    db: Session,
    document_id: uuid.UUID,
    action: str,
    version_no: int | None = None,
    actor_id: uuid.UUID | None = None,
    detail: str | None = None,
) -> DocumentEvent:
    """Append one audit row (plan v3: RAG consumes these later). Flush only."""

    event = DocumentEvent(
        document_id=document_id,
        version_no=version_no,
        action=action,
        actor_id=actor_id,
        detail=detail,
    )
    db.add(event)
    return event


def detect_content_kind(extension: str, content: bytes) -> str | None:
    """Return an error message when bytes do not match the extension.

    Returns None when content is consistent (Upload AC-06, stdlib only).
    """
    import io
    import zipfile

    if extension == ".pdf":
        if not content.startswith(b"%PDF"):
            return "content is not a real PDF file"
        return None
    if extension in (".docx", ".pptx", ".xlsx"):
        if not content.startswith(b"PK\x03\x04"):
            return f"content is not a real {extension} (ZIP-based) file"
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = zf.namelist()
        except zipfile.BadZipFile:
            return f"content is not a real {extension} file"
        if "[Content_Types].xml" not in names:
            return f"content is not a real {extension} file"
        expected = {".docx": "word/", ".pptx": "ppt/", ".xlsx": "xl/"}[extension]
        if not any(n.startswith(expected) for n in names):
            return f"content is not a real {extension} file"
        return None
    if extension in (".md", ".txt"):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return f"content is not a real {extension} (UTF-8 text) file"
        if "\x00" in text:
            return f"content is not a real {extension} (UTF-8 text) file"
        return None
    return f"unsupported file format: {extension}"


def validate_file(file_name: str, content: bytes) -> tuple[str, str, int] | str:
    """Validate one file's format/size/content. Returns (ext, mime, size) or an error.

    Course-scoped uploads are not tied to a class, so authorization lives in
    :func:`create_document` instead of here.
    """
    lowered = file_name.lower()
    dot = lowered.rfind(".")
    extension = lowered[dot:] if dot != -1 else ""
    if extension not in ACCEPTED_EXTENSIONS:
        accepted = ", ".join(sorted(ACCEPTED_EXTENSIONS))
        return f"Invalid file format '{extension or file_name}'. Accepted: {accepted}."
    size = len(content)
    if size > MAX_FILE_SIZE_BYTES:
        return f"File '{file_name}' exceeds 100MB per file."
    mismatch = detect_content_kind(extension, content)
    if mismatch is not None:
        return f"File '{file_name}' rejected: {mismatch}."
    return extension, ACCEPTED_EXTENSIONS[extension], size


def _course_id_of_class(db: Session, class_id: uuid.UUID) -> uuid.UUID | None:
    row = db.query(Class.course_id).filter(Class.id == class_id).first()
    return row[0] if row else None


def document_course_id(db: Session, record: Document) -> uuid.UUID | None:
    """Course of a document: stored value, else derived from its owning class."""

    if record.course_id is not None:
        return record.course_id
    return _course_id_of_class(db, record.class_id)


def can_access_course(db: Session, user: User, course_id: uuid.UUID | None) -> bool:
    """Course-scope visibility (plan v3 D6): admin bypasses, trainer must be in-course."""

    if _role_name(user).upper() == "ADMIN":
        return True
    if course_id is None:
        return False
    return uuid.UUID(str(course_id)) in my_course_ids(db, user)


def can_view_document(db: Session, user: User, record: Document) -> bool:
    """Visibility of one document (plan v3 D6): admin bypasses.

    Class-scoped materials stay private to their class trainer; course-scoped
    materials are visible to every trainer of the subject's course.
    """

    if _role_name(user).upper() == "ADMIN":
        return True
    if record.scope == SCOPE_CLASS:
        return record.class_id in set(my_class_ids(db, user))
    return can_access_course(db, user, document_course_id(db, record))


def can_review_document(db: Session, user: User, record: Document) -> bool:
    """Review rights (plan v3 D2): every trainer of the course + admin.

    Class-scoped materials are reviewed only by the trainer of the owning class
    (or an admin). Falls back to class ownership for legacy rows without a
    course.
    """

    if _role_name(user).upper() == "ADMIN":
        return True
    if record.scope == SCOPE_CLASS:
        return record.class_id in set(my_class_ids(db, user))
    course_id = document_course_id(db, record)
    if course_id is not None:
        return uuid.UUID(str(course_id)) in my_course_ids(db, user)
    return record.class_id in set(my_class_ids(db, user))


def pending_version(db: Session, document_id: uuid.UUID) -> DocumentVersion | None:
    return (
        db.query(DocumentVersion)
        .filter(
            DocumentVersion.document_id == document_id,
            DocumentVersion.status == STATUS_PENDING,
        )
        .order_by(DocumentVersion.version_no.desc())
        .first()
    )


def _next_version_no(db: Session, document_id: uuid.UUID) -> int:
    row = (
        db.query(func.max(DocumentVersion.version_no))
        .filter(DocumentVersion.document_id == document_id)
        .scalar()
    )
    return int(row or 0) + 1


def version_number(db: Session, version_id: uuid.UUID | None) -> int | None:
    if version_id is None:
        return None
    row = db.query(DocumentVersion.version_no).filter(DocumentVersion.id == version_id).first()
    return row[0] if row else None


def latest_version_no(db: Session, document_id: uuid.UUID) -> int | None:
    row = (
        db.query(func.max(DocumentVersion.version_no))
        .filter(DocumentVersion.document_id == document_id)
        .scalar()
    )
    return int(row) if row is not None else None


def versions_for(db: Session, document_id: uuid.UUID) -> list[DocumentVersion]:
    """History, newest first (plan v3 D4: every version stays viewable)."""

    return (
        db.query(DocumentVersion)
        .filter(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_no.desc())
        .all()
    )


def _find_group(
    db: Session,
    course_id: uuid.UUID | None,
    name_norm: str,
    scope: str = SCOPE_COURSE,
    class_id: uuid.UUID | None = None,
) -> Document | None:
    """Version group for this upload (one group per scope + name).

    Course materials group by subject, so any class reuses the same group;
    class materials group by class, so two classes may keep same-named files
    independently. A flagged same-name clash is never auto-merged.
    """

    if course_id is None:
        return None
    query = db.query(Document).filter(
        Document.course_id == course_id,
        Document.name_normalized == name_norm,
        Document.scope == scope,
        Document.deleted_at.is_(None),
    )
    if scope == SCOPE_CLASS:
        query = query.filter(Document.class_id == class_id)
    matches = query.order_by(Document.created_at).all()
    return matches[0] if len(matches) == 1 else None


def _clash_in_course(
    db: Session,
    course_id: uuid.UUID | None,
    name_norm: str,
    group_id: uuid.UUID,
    scope: str = SCOPE_COURSE,
    class_id: uuid.UUID | None = None,
) -> bool:
    """True when another same-name group in the same scope exists (never merged)."""

    if course_id is None:
        return False
    query = db.query(Document).filter(
        Document.course_id == course_id,
        Document.name_normalized == name_norm,
        Document.scope == scope,
        Document.deleted_at.is_(None),
        Document.id != group_id,
    )
    if scope == SCOPE_CLASS:
        query = query.filter(Document.class_id == class_id)
    return query.count() > 0


def course_class_ids(db: Session, course_id: uuid.UUID | None) -> list[uuid.UUID]:
    """Every class of a course, ordered by code."""

    if course_id is None:
        return []
    rows = (
        db.query(Class.id).filter(Class.course_id == course_id).order_by(Class.code).all()
    )
    return [r[0] for r in rows]


def optout_class_ids(db: Session, document_id: uuid.UUID) -> list[uuid.UUID]:
    """Classes that removed the document from their own list (per-class opt-out)."""

    rows = (
        db.query(DocumentClassOptout.class_id)
        .filter(DocumentClassOptout.document_id == document_id)
        .all()
    )
    return [r[0] for r in rows]


def is_removed_for_class(
    db: Session, document_id: uuid.UUID, class_id: uuid.UUID | None
) -> bool:
    """True when ``class_id`` removed this document from its own list."""

    if class_id is None:
        return False
    return (
        db.query(DocumentClassOptout.class_id)
        .filter(
            DocumentClassOptout.document_id == document_id,
            DocumentClassOptout.class_id == class_id,
        )
        .first()
        is not None
    )


def _clear_optout(db: Session, document_id: uuid.UUID, class_id: uuid.UUID) -> None:
    db.query(DocumentClassOptout).filter(
        DocumentClassOptout.document_id == document_id,
        DocumentClassOptout.class_id == class_id,
    ).delete(synchronize_session=False)


def _add_optout(
    db: Session,
    document_id: uuid.UUID,
    class_id: uuid.UUID,
    removed_by: uuid.UUID | None = None,
) -> bool:
    """Insert the opt-out row; returns False when it already existed."""

    if is_removed_for_class(db, document_id, class_id):
        return False
    db.add(
        DocumentClassOptout(
            document_id=document_id, class_id=class_id, removed_by=removed_by
        )
    )
    return True


def create_document(
    db: Session,
    file_name: str,
    content: bytes,
    class_id: uuid.UUID | str | None,
    user: User,
    course_id: uuid.UUID | str | None = None,
    scope: str = SCOPE_COURSE,
) -> Document:
    """Persist a validated upload as a new version of its group.

    ``scope`` decides who owns the material:

    * ``"course"`` (default) — shared with every class of the subject, so it is
      uploaded once and every class sees it (``class_id`` is only a label).
    * ``"class"`` — private to one class (``class_id`` is required and must be
      assigned to the caller); other classes of the subject never see it.

    Call with ``course_id`` (course page upload) or with ``class_id`` (which
    also resolves the course). A repeat upload of the same normalized name
    becomes version N+1 and supersedes any still-pending version; a
    duplicate-content hash is warned, never blocked.
    """

    if scope not in VALID_SCOPES:
        raise ValueError(f"Unknown scope '{scope}'.")

    validated = validate_file(file_name, content)
    if isinstance(validated, str):
        raise ValueError(validated)  # noqa: TRY004 - validation failure, not a type error
    extension, mime, size = validated

    class_uuid: uuid.UUID | None = None
    if scope == SCOPE_CLASS:
        if class_id is None:
            raise ValueError("A class is required to upload class materials.")
        try:
            class_uuid = uuid.UUID(str(class_id))
        except (TypeError, ValueError, AttributeError):
            raise ValueError(
                f"Class '{class_id}' does not exist (admin creates classes)."
            )
        if class_uuid not in set(my_class_ids(db, user)):
            raise ValueError("Class is not assigned to you.")
        resolved_course = _course_id_of_class(db, class_uuid)
        if resolved_course is None:
            raise ValueError(f"Class '{class_id}' does not exist (admin creates classes).")
    elif course_id is None:
        if class_id is None:
            raise ValueError("A course or class is required to upload.")
        try:
            class_uuid = uuid.UUID(str(class_id))
        except (TypeError, ValueError, AttributeError):
            raise ValueError(
                f"Class '{class_id}' does not exist (admin creates classes)."
            )
        if class_uuid not in set(my_class_ids(db, user)):
            raise ValueError("Class is not assigned to you.")
        resolved_course = _course_id_of_class(db, class_uuid)
        if resolved_course is None:
            raise ValueError(f"Class '{class_id}' does not exist (admin creates classes).")
    else:
        try:
            resolved_course = uuid.UUID(str(course_id))
        except (TypeError, ValueError, AttributeError):
            raise ValueError(f"Course '{course_id}' does not exist.")
        if class_id is not None:
            try:
                class_uuid = uuid.UUID(str(class_id))
            except (TypeError, ValueError, AttributeError):
                class_uuid = None
        if not can_access_course(db, user, resolved_course):
            raise ValueError("Course is not assigned to you.")

    name_norm = normalize_name(file_name)
    file_hash = content_hash(content)

    group = _find_group(db, resolved_course, name_norm, scope, class_uuid)
    superseded: list[DocumentVersion] = []
    duplicate_hash = False

    if group is not None:
        superseded = (
            db.query(DocumentVersion)
            .filter(
                DocumentVersion.document_id == group.id,
                DocumentVersion.status == STATUS_PENDING,
            )
            .all()
        )
        duplicate_hash = (
            db.query(DocumentVersion)
            .filter(
                DocumentVersion.document_id == group.id,
                DocumentVersion.file_hash == file_hash,
            )
            .count()
            > 0
        )
        version_no = _next_version_no(db, group.id)
    else:
        version_no = 1

    if group is None:
        group = Document(
            class_id=class_uuid,
            scope=scope,
            uploaded_by=user.id,
            created_by=user.id,
            file_name=file_name,
            file_type=extension,
            mime_type=mime,
            size_bytes=size,
            status=STATUS_PENDING,
            course_id=resolved_course,
            name_normalized=name_norm,
        )
        db.add(group)
        db.flush()
    else:
        # Latest upload wins the header fields; created_by stays the v1 uploader (D7).
        group.file_name = file_name
        group.file_type = extension
        group.mime_type = mime
        group.size_bytes = size
        group.status = STATUS_PENDING
        group.review_note = None
        if group.created_by is None:
            group.created_by = group.uploaded_by

    # Uploading from a class that had removed the document brings it back there.
    if class_uuid is not None:
        _clear_optout(db, group.id, class_uuid)

    for old in superseded:
        old.status = STATUS_SUPERSEDED

    version = DocumentVersion(
        document_id=group.id,
        version_no=version_no,
        file_hash=file_hash,
        status=STATUS_PENDING,
        uploaded_by=user.id,
    )
    db.add(version)
    db.flush()

    if _clash_in_course(db, resolved_course, name_norm, group.id, scope, class_uuid):
        group.needs_review = True

    record_event(
        db,
        group.id,
        "upload",
        version_no=version_no,
        actor_id=user.id,
        detail=f"v{version_no} uploaded.",
    )
    for old in superseded:
        record_event(
            db,
            group.id,
            "superseded",
            version_no=old.version_no,
            actor_id=user.id,
            detail=f"v{old.version_no} superseded by v{version_no} before review.",
        )

    db.commit()
    db.refresh(group)
    warning = None
    if duplicate_hash:
        warning = "Identical content already exists in this course group."
    if group.needs_review:
        clash = "A same-named document already exists in this course; review required."
        warning = f"{warning} {clash}".strip() if warning else clash
    group.upload_warning = warning  # type: ignore[attr-defined]
    return group


def get_document(db: Session, document_id: uuid.UUID | str) -> Document | None:
    try:
        doc_uuid = uuid.UUID(str(document_id))
    except (TypeError, ValueError, AttributeError):
        return None
    return db.query(Document).filter(Document.id == doc_uuid).first()


def _switch_current(
    db: Session,
    record: Document,
    version: DocumentVersion,
    reviewer: User | None,
    action: str,
    detail: str,
) -> None:
    """Atomic current-switch shared by approve / rollback / retry (Phase 2b/2c).

    Moves ``current_version_id`` to ``version`` (now processing) and supersedes
    the previously active version. The old version is never deleted, so a
    failed re-index can always be rolled back to or retried.
    """

    previous_current = record.current_version_id
    version.status = STATUS_PROCESSING
    if reviewer is not None:
        version.reviewed_by = reviewer.id
        version.reviewed_at = datetime.now(UTC)
    db.flush()
    record.current_version_id = version.id
    record.status = STATUS_PROCESSING
    if previous_current is not None and previous_current != version.id:
        previous = (
            db.query(DocumentVersion).filter(DocumentVersion.id == previous_current).first()
        )
        if previous is not None and previous.status in (STATUS_PROCESSING, STATUS_INDEXED):
            previous.status = STATUS_SUPERSEDED
            record_event(
                db,
                record.id,
                "superseded",
                version_no=previous.version_no,
                actor_id=reviewer.id if reviewer else None,
                detail=f"v{previous.version_no} superseded by v{version.version_no}.",
            )
    record_event(
        db,
        record.id,
        action,
        version_no=version.version_no,
        actor_id=reviewer.id if reviewer else None,
        detail=detail,
    )


def approve_document(
    db: Session, document_id: uuid.UUID | str, reviewer: User | None = None
) -> Document:
    """Approve pending -> processing and switch the serving version (Phase 2b).

    The switch is atomic: the document's ``current_version_id`` moves to the
    approved version in one commit, and the previously serving version (if it
    was still active) becomes ``superseded``. Self-review is allowed (D2); the
    reviewer is audited on the version and the event log.
    """

    record = get_document(db, document_id)
    if record is None:
        raise KeyError(str(document_id))
    version = pending_version(db, record.id)
    if version is None:
        raise ValueError(f"Only pending documents can be approved (is {record.status}).")
    _switch_current(db, record, version, reviewer, "approve", f"v{version.version_no} approved.")
    db.commit()
    db.refresh(record)
    return record


def reject_document(
    db: Session,
    document_id: uuid.UUID | str,
    reason: str | None = None,
    reviewer: User | None = None,
) -> Document:
    """Reject pending -> rejected (terminal, plan v3 Phase 1/2b).

    Mirrors approve_document guards so Approve/Reject stay paired. Only the
    pending version is stopped; a previously serving version keeps serving.
    """

    record = get_document(db, document_id)
    if record is None:
        raise KeyError(str(document_id))
    version = pending_version(db, record.id)
    if version is None:
        raise ValueError(f"Only pending documents can be rejected (is {record.status}).")
    note = (reason or "").strip()
    version.status = STATUS_REJECTED
    version.review_note = note[:500] or None
    if reviewer is not None:
        version.reviewed_by = reviewer.id
        version.reviewed_at = datetime.now(UTC)
    # ``Document.status`` mirrors the newest version so the list row always
    # shows "vN [status]" for the version the trainer just acted on; the
    # serving version stays visible via ``current_version_no``.
    record.status = STATUS_REJECTED
    record.review_note = note[:500] or None
    record_event(
        db,
        record.id,
        "reject",
        version_no=version.version_no,
        actor_id=reviewer.id if reviewer else None,
        detail=note or None,
    )
    db.commit()
    db.refresh(record)
    return record


def rollback_document(
    db: Session,
    document_id: uuid.UUID | str,
    version_no: int,
    reviewer: User | None = None,
) -> Document:
    """Serve an older version again (plan v3 Phase 2c).

    Re-index first: the chosen version goes back to ``processing`` and becomes
    the current one; the version that was serving becomes ``superseded`` but is
    never deleted, so a failed re-index can be retried or rolled forward.
    """

    record = get_document(db, document_id)
    if record is None:
        raise KeyError(str(document_id))
    version = (
        db.query(DocumentVersion)
        .filter(
            DocumentVersion.document_id == record.id,
            DocumentVersion.version_no == version_no,
        )
        .first()
    )
    if version is None:
        raise KeyError(f"version {version_no}")
    if version.id == record.current_version_id:
        raise ValueError(f"v{version_no} is already the serving version.")
    if version.status == STATUS_PENDING:
        raise ValueError(f"v{version_no} is pending; approve it instead of rolling back.")
    _switch_current(
        db,
        record,
        version,
        reviewer,
        "rollback",
        f"rolled back to v{version_no} (re-index).",
    )
    db.commit()
    db.refresh(record)
    return record


def retry_document(
    db: Session, document_id: uuid.UUID | str, reviewer: User | None = None
) -> Document:
    """Retry the newest failed version (plan v3 Phase 2c)."""

    record = get_document(db, document_id)
    if record is None:
        raise KeyError(str(document_id))
    version = (
        db.query(DocumentVersion)
        .filter(
            DocumentVersion.document_id == record.id,
            DocumentVersion.status == STATUS_FAILED,
        )
        .order_by(DocumentVersion.version_no.desc())
        .first()
    )
    if version is None:
        raise ValueError("No failed version to retry.")
    _switch_current(
        db,
        record,
        version,
        reviewer,
        "retry",
        f"retrying v{version.version_no} (re-index).",
    )
    db.commit()
    db.refresh(record)
    return record


def delete_document(
    db: Session, document_id: uuid.UUID | str, actor: User | None
) -> Document:
    """Soft-delete a document for the whole course (plan v3 D5).

    Only the v1 uploader (``created_by``) or an admin may delete; raises
    ``PermissionError`` otherwise (mapped to 403 by the router).
    """

    record = get_document(db, document_id)
    if record is None:
        raise KeyError(str(document_id))
    role = _role_name(actor).upper() if actor is not None else ""
    if role != "ADMIN" and (actor is None or record.created_by != actor.id):
        raise PermissionError("Only the uploader or an admin may delete a document.")
    if record.deleted_at is None:
        record.deleted_at = datetime.now(UTC)
        record_event(
            db,
            record.id,
            "delete",
            actor_id=actor.id if actor else None,
            detail="soft-deleted for the whole course.",
        )
        db.commit()
        db.refresh(record)
    return record


def events_for(db: Session, document_id: uuid.UUID) -> list[DocumentEvent]:
    """Full audit trail, newest first (plan v3 Phase 2c)."""

    return (
        db.query(DocumentEvent)
        .filter(DocumentEvent.document_id == document_id)
        .order_by(DocumentEvent.created_at.desc(), DocumentEvent.id.desc())
        .all()
    )


def class_scope_condition(db: Session, class_uuid: uuid.UUID):
    """SQLAlchemy filter: a class's shelf — its subject's shared materials plus
    the materials the class itself uploaded (ignoring per-class removals)."""

    course_id = _course_id_of_class(db, class_uuid)
    return and_(
        Document.course_id == course_id,
        or_(
            Document.scope == SCOPE_COURSE,
            Document.class_id == class_uuid,
        ),
    )


def class_visibility_condition(db: Session, class_uuid: uuid.UUID):
    """SQLAlchemy filter: documents a class may see (shelf minus its opt-outs)."""

    optouts = db.query(DocumentClassOptout.document_id).filter(
        DocumentClassOptout.class_id == class_uuid
    )
    return and_(
        class_scope_condition(db, class_uuid),
        Document.id.notin_(optouts),
    )


def remove_from_class(
    db: Session,
    document_id: uuid.UUID | str,
    class_id: uuid.UUID | str,
    actor: User | None = None,
) -> Document:
    """Remove a subject-shared document from one class only (AI-37).

    The document keeps belonging to its course, so every other class still
    sees it; only ``class_id`` stops seeing it. Restore with
    :func:`restore_to_class`. Raises ``KeyError`` when the class is not part
    of the document's course (mapped to 404 by the router) and ``ValueError``
    for class-only materials, which must be deleted instead.
    """

    record = get_document(db, document_id)
    if record is None:
        raise KeyError(str(document_id))
    if record.scope == SCOPE_CLASS:
        raise ValueError(
            "Class materials are private to their class; delete them instead."
        )
    class_uuid = uuid.UUID(str(class_id))
    if class_uuid not in set(course_class_ids(db, document_course_id(db, record))):
        raise KeyError(str(class_id))
    if _add_optout(
        db, record.id, class_uuid, removed_by=actor.id if actor else None
    ):
        record_event(
            db,
            record.id,
            "removed_from_class",
            actor_id=actor.id if actor else None,
            detail=f"removed from class {class_uuid}.",
        )
    db.commit()
    db.refresh(record)
    return record


def restore_to_class(
    db: Session,
    document_id: uuid.UUID | str,
    class_id: uuid.UUID | str,
    actor: User | None = None,
) -> Document:
    """Undo :func:`remove_from_class`, bringing the document back to a class."""

    record = get_document(db, document_id)
    if record is None:
        raise KeyError(str(document_id))
    class_uuid = uuid.UUID(str(class_id))
    if class_uuid not in set(course_class_ids(db, document_course_id(db, record))):
        raise KeyError(str(class_id))
    deleted = (
        db.query(DocumentClassOptout)
        .filter(
            DocumentClassOptout.document_id == record.id,
            DocumentClassOptout.class_id == class_uuid,
        )
        .delete(synchronize_session=False)
    )
    if deleted:
        record_event(
            db,
            record.id,
            "restored_to_class",
            actor_id=actor.id if actor else None,
            detail=f"restored to class {class_uuid}.",
        )
    db.commit()
    db.refresh(record)
    return record



def documents_for_class(
    db: Session, class_id: uuid.UUID | str, *, only_indexed: bool = False
) -> list[Document]:
    """Documents visible to one class (its subject's shared files + its own)."""

    class_uuid = uuid.UUID(str(class_id))
    query = db.query(Document).filter(
        Document.deleted_at.is_(None),
        class_visibility_condition(db, class_uuid),
    )
    if only_indexed:
        query = query.filter(Document.status == STATUS_INDEXED)
    return query.order_by(Document.created_at.desc()).all()


def list_documents(
    db: Session,
    user: User,
    search: str = "",
    class_id: uuid.UUID | str | None = None,
    course_id: uuid.UUID | str | None = None,
    file_type: str | None = None,
    status: str | None = None,
    scope: str | None = None,
    page: int = 1,
    page_size: int = 20,
    include_removed: bool = False,
) -> tuple[list[Document], int]:
    """Search/filter/paginate documents visible to the caller (List AC-02..05).

    Trainers see the shared materials of the courses they teach plus the
    materials their own classes uploaded; admins see everything. Per-class
    removals only apply under a class filter, and ``scope`` narrows to
    subject-wide (``"course"``) or class-only (``"class"``) materials.
    Soft-deleted rows never appear.
    """

    role = _role_name(user).upper()
    classes = set(my_class_ids(db, user))
    courses = my_course_ids(db, user)

    query = db.query(Document).filter(Document.deleted_at.is_(None))
    if role != "ADMIN":
        if not courses:
            return [], 0
        scopes = [Document.scope == SCOPE_COURSE]
        if classes:
            scopes.append(
                and_(Document.scope == SCOPE_CLASS, Document.class_id.in_(classes))
            )
        query = query.filter(Document.course_id.in_(courses), or_(*scopes))

    needle = search.strip().lower()
    if needle:
        query = query.filter(Document.file_name.ilike(f"%{needle}%"))
    if course_id is not None:
        try:
            course_uuid = uuid.UUID(str(course_id))
        except (TypeError, ValueError, AttributeError):
            return [], 0
        if role != "ADMIN" and course_uuid not in courses:
            return [], 0
        query = query.filter(Document.course_id == course_uuid)
    if class_id is not None:
        try:
            class_uuid = uuid.UUID(str(class_id))
        except (TypeError, ValueError, AttributeError):
            return [], 0
        if role != "ADMIN" and class_uuid not in classes:
            return [], 0
        # ``include_removed`` answers "what would this class see?" so the UI can
        # offer Restore for documents the class removed from its own list.
        if include_removed:
            query = query.filter(class_scope_condition(db, class_uuid))
        else:
            query = query.filter(class_visibility_condition(db, class_uuid))
    if scope is not None:
        if scope not in VALID_SCOPES:
            return [], 0
        query = query.filter(Document.scope == scope)
    if file_type is not None:
        query = query.filter(Document.file_type == file_type)
    if status is not None:
        query = query.filter(Document.status == status)
    total = query.count()
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    rows = (
        query.order_by(Document.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return rows, total


def build_qdrant_payload(record: Document) -> dict[str, str]:
    """Native payload filter for RAG (course-scoped)."""

    payload = {
        "document_id": str(record.id),
        "file_name": record.file_name,
        "file_type": record.file_type,
        "scope": record.scope,
    }
    if record.course_id is not None:
        payload["course_id"] = str(record.course_id)
    if record.class_id is not None:
        payload["class_id"] = str(record.class_id)
    return payload


def class_name(db: Session, class_id: uuid.UUID | str | None) -> str:
    if class_id is None:
        return ""
    try:
        clazz = db.query(Class).filter(Class.id == uuid.UUID(str(class_id))).first()
    except (TypeError, ValueError, AttributeError):
        return str(class_id)
    if clazz is None:
        return str(class_id)
    return clazz.name
