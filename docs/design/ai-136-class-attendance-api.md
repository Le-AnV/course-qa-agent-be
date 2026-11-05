# AI-136 — Class Attendance Per Session API Design

> **Ticket:** [AI-136](https://ai03.atlassian.net/browse/AI-136) (Epic AI-145: Class Schedule & Attendance)
> **Priority:** High | **Story Points:** 8 SP
> **Status:** Design — ready for implementation review
> **Target branch:** `features/Design_ClassAttendance` → `features/Implementation_ClassAttendance`
> **Depends on:** AI-155 (schema, shipped), AI-156 (holiday service, shipped)
> **Related:** AI-157 (student schedule view), AI-158 (teacher absence + handover), AI-140 (holiday admin)

---

## 1. Purpose

Give a trainer a session-based attendance sheet for every class they teach, and
give enrolled students read-only access to their own attendance history.

The schema already exists from AI-155. This story adds:

* weekly schedule CRUD (the minimum needed for sessions to exist),
* lazy/idempotent session materialisation,
* ad-hoc session creation,
* attendance sheet read/upsert,
* class attendance summary,
* student self-history.

No database migration is introduced. `main` has a single Alembic head,
`0037_merge_cg_handover`.

### Acceptance criteria mapped to this contract

| AC | Where it is satisfied |
|---|---|
| Trainer marks enrolled students present / absent / late | `PUT /sessions/{session_id}/attendance` (§3.1 #7) |
| One record per student per session | `attendance` partial unique index from AI-155 + upsert in §6 |
| Student sees own history | `GET /api/student/classes/{class_id}/attendance` (§3.1 #9) |
| Trainer + admin see attendance-rate summary | `GET /classes/{class_id}/attendance/summary` (§3.1 #8) |
| Editing a record updates `updated_at` | SQLAlchemy `onupdate` + `Attendance.updated_at` in the response |

The Jira wording says "present / absent / late". The persisted contract uses
four values from AI-155 so an absence can be classified:

* `present`
* `late`
* `excused_absence`
* `unexcused_absence`

`excused_absence` is excluded from the attendance-rate denominator (see §5.1).

---

## 2. Decisions carried into the API

| # | Decision | Effect on this contract |
|---|---|---|
| D1 | Four statuses, not three | `AttendanceStatus` literal has four values |
| D2 | Rate = `(present + late) / (present + late + unexcused_absence + unmarked)` | §5.1 |
| D3 | Future sessions cannot be marked | `PUT` returns `400` when `session.date > today` (Vietnam time) |
| D4 | Weekly schedule + ad-hoc session are in scope | Endpoints #1–#5 |
| D5 | Admin summary is a modal, not a new page | API is class-scoped; no admin page contract |
| D6 | Cancel/reschedule session is out of scope for AI-136 | Existing `cancelled` rows are read-only; no cancel endpoint |
| D7 | Co-trainer has the same attendance rights as the primary trainer | `class_ids_for_trainer(..., active_only=True)` |
| D13 | Plain JSON, `snake_case`, no envelope | §3 |
| D14 | Staff and student endpoints are separated | `/api/classes/...` vs `/api/student/...` |
| D18 | No new migration | Schema from AI-155; only verify one Alembic head |

### Contract clarifications

* **Teaching assistants.** `TEACHING_ASSISTANT` is not granted staff access by
  this story. The student endpoint remains enrollment-based; a TA who is also an
  active class member can read their own history, but does not receive the
  trainer's sheet.
* **Timezone.** "Today" means the Vietnam-local date (`UTC+7`), using
  `holiday_service.VIETNAM_TZ`. Session timestamps are stored with timezone and
  serialized with an offset.
* **Response field names.** Query parameters stay `from` / `to`; response bodies
  use `date_from` / `date_to` to avoid the Python `from` keyword.

---

## 3. API contract

All endpoints require `Authorization: Bearer <JWT>`. Errors use the repo-wide
shape `{"detail": "..."}`.

### 3.1 Endpoints

| # | Method | Path | Allowed caller | Purpose |
|---|---|---|---|---|
| 1 | `GET` | `/api/classes/{class_id}/schedules` | ADMIN, class staff | List weekly schedule templates |
| 2 | `POST` | `/api/classes/{class_id}/schedules` | ADMIN, class staff | Create a weekly template |
| 3 | `DELETE` | `/api/classes/{class_id}/schedules/{schedule_id}` | ADMIN, class staff | Delete a template; existing sessions stay |
| 4 | `GET` | `/api/classes/{class_id}/sessions?from=&to=` | ADMIN, class staff, enrolled student | Materialise missing sessions, then list |
| 5 | `POST` | `/api/classes/{class_id}/sessions` | ADMIN, class staff | Create an ad-hoc session |
| 6 | `GET` | `/api/classes/{class_id}/sessions/{session_id}/attendance` | ADMIN, class staff | Full attendance sheet |
| 7 | `PUT` | `/api/classes/{class_id}/sessions/{session_id}/attendance` | ADMIN, class staff | Partial upsert of attendance |
| 8 | `GET` | `/api/classes/{class_id}/attendance/summary?from=&to=` | ADMIN, class staff | Per-student summary |
| 9 | `GET` | `/api/student/classes/{class_id}/attendance?from=&to=` | Enrolled student | Caller's own history |

"Class staff" means ADMIN, the primary trainer, or an active co-trainer of that
class. The router must use one shared helper so the rule cannot drift between
endpoints.

### 3.2 Permission matrix

| Endpoint | ADMIN | Primary trainer | Co-trainer | Enrolled student | Outsider |
|---|---:|---:|---:|---:|---:|
| Schedules (1–3) | ✅ | ✅ | ✅ | ❌ 403 | ❌ 403 |
| Sessions list (4) | ✅ | ✅ | ✅ | ✅ (own class only) | ❌ 403 |
| Ad-hoc session (5) | ✅ | ✅ | ✅ | ❌ 403 | ❌ 403 |
| Sheet GET/PUT (6–7) | ✅ | ✅ | ✅ | ❌ 403 | ❌ 403 |
| Summary (8) | ✅ | ✅ | ✅ | ❌ 403 | ❌ 403 |
| Student history (9) | ❌ 403 | ❌ 403 | ❌ 403 | ✅ (self only) | ❌ 403 |

Reference helpers (implementation will live in
`app/routers/attendance/permissions.py` or the router module):

```python
def require_class_staff(db, user, class_id) -> Class:
    clazz = course_service.get_class(db, class_id)
    if clazz is None:
        raise HTTPException(404, "Class not found.")
    role = (user_service.role_name_of(user) or "").upper()
    if role == "ADMIN":
        return clazz
    if role == "TRAINER" and clazz.id in course_service.class_ids_for_trainer(
        db, user.id, active_only=True
    ):
        return clazz
    raise HTTPException(403, "Only this class' teachers can manage attendance.")


def require_enrolled_student(db, user, class_id) -> Class:
    clazz = course_service.get_class(db, class_id)
    if clazz is None:
        raise HTTPException(404, "Class not found.")
    if not course_service.is_active_member(db, class_id, user.id):
        raise HTTPException(403, "You are not enrolled in this class.")
    return clazz
```

### 3.3 DTOs

#### `ClassScheduleCreate`

```json
{
  "weekday": 0,
  "start_time": "19:00",
  "end_time": "21:00",
  "effective_from": "2026-09-01",
  "effective_to": null,
  "trainer_id": null
}
```

Validation:

* `0 <= weekday <= 6` (`0 = Monday`, Python `date.weekday()`).
* `end_time > start_time`.
* `effective_to is null or effective_to >= effective_from`.
* `trainer_id`, when present, must be the primary trainer or a co-trainer of the
  class. When absent, the server uses the class's primary trainer.

The server copies `class.name` into `class_schedule.subject`; clients do not
send it.

#### `ClassScheduleRead`

```json
{
  "id": "5f0d...",
  "class_id": "9c1a...",
  "subject": "Introduction to Computer Science - Section 1",
  "weekday": 0,
  "start_time": "19:00:00",
  "end_time": "21:00:00",
  "trainer_id": "b2e4...",
  "trainer_name": "Academic Trainer",
  "effective_from": "2026-09-01",
  "effective_to": null
}
```

#### `SessionCreate` (ad-hoc)

```json
{
  "date": "2026-11-07",
  "start_time": "19:00",
  "end_time": "21:00",
  "trainer_id": null,
  "note": "Extra review session"
}
```

Validation:

* `end_time > start_time`.
* The date is not a system/class holiday (`holiday_service.ensure_working_day`).
* `trainer_id`, when present, must be class staff.
* `(class_id, date, start_at)` must be free → otherwise `409`.
* `schedule_id` is always `NULL` for ad-hoc sessions.

#### `ClassSessionRead`

```json
{
  "id": "aa11...",
  "class_id": "9c1a...",
  "schedule_id": "5f0d...",
  "date": "2026-11-02",
  "start_at": "2026-11-02T19:00:00+07:00",
  "end_at": "2026-11-02T21:00:00+07:00",
  "trainer_id": "b2e4...",
  "trainer_name": "Academic Trainer",
  "status": "scheduled",
  "note": null,
  "student_count": 3,
  "marked_count": 2
}
```

`student_count` = ACTIVE enrollments. `marked_count` = attendance rows for this
session (student rows only). `trainer_name` is resolved through
`course_service.user_display_name`.

#### `AttendanceSheetRead`

```json
{
  "session": { "...": "ClassSessionRead" },
  "summary": {
    "present": 1,
    "late": 0,
    "excused_absence": 0,
    "unexcused_absence": 1,
    "unmarked": 1
  },
  "records": [
    {
      "student_id": "u1...",
      "student_code": "AS_0001",
      "full_name": "Academic Student",
      "email": "student@academic.edu.vn",
      "status": "present",
      "note": null,
      "updated_at": "2026-11-05T10:15:00+07:00"
    },
    {
      "student_id": "u2...",
      "student_code": "AS_0002",
      "full_name": "Second Student",
      "email": "student2@academic.edu.vn",
      "status": null,
      "note": null,
      "updated_at": null
    }
  ]
}
```

Ordering: `student_code` ascending, then `full_name`. Students without an
attendance row are still returned with `status = null` (`unmarked`).

#### `AttendanceBulkUpdate` (`PUT` body)

```json
{
  "records": [
    { "student_id": "u1...", "status": "present", "note": null },
    { "student_id": "u2...", "status": "unexcused_absence", "note": "No notice" }
  ]
}
```

Validation:

* `1 <= len(records) <= 200`.
* No duplicate `student_id`.
* `status ∈ {present, late, excused_absence, unexcused_absence}`.
* `note` at most 500 characters.
* Every `student_id` must be an ACTIVE member of the class → `422` otherwise.
* Partial payloads are allowed; the endpoint upserts only the rows sent.

#### `AttendanceSummaryRead`

```json
{
  "class_id": "9c1a...",
  "date_from": "2026-09-01",
  "date_to": "2026-11-05",
  "total_sessions": 8,
  "students": [
    {
      "student_id": "u1...",
      "student_code": "AS_0001",
      "full_name": "Academic Student",
      "present": 6,
      "late": 1,
      "excused_absence": 0,
      "unexcused_absence": 0,
      "unmarked": 1,
      "attendance_rate": 0.875
    }
  ]
}
```

`attendance_rate` is `null` when the denominator is zero. The API returns a
fraction in `[0, 1]`; the UI formats the percentage.

#### `StudentAttendanceRead`

```json
{
  "class_id": "9c1a...",
  "date_from": "2026-09-01",
  "date_to": "2026-11-05",
  "total_sessions": 8,
  "attended": 7,
  "attendance_rate": 0.875,
  "items": [
    {
      "session_id": "aa11...",
      "date": "2026-11-02",
      "start_at": "2026-11-02T19:00:00+07:00",
      "end_at": "2026-11-02T21:00:00+07:00",
      "session_status": "scheduled",
      "status": "present",
      "note": null,
      "updated_at": "2026-11-05T10:15:00+07:00"
    }
  ]
}
```

* Returns every session in the range whose `date <= today`, including sessions
  that are not marked (`status = null`).
* `attended = present + late`.
* Never returns another student's rows, even if `student_id` is guessed.

### 3.4 Error matrix

| Code | When | Suggested `detail` |
|---|---|---|
| `401` | Missing / invalid / expired token | `Not authenticated.` |
| `403` | Caller is not class staff / not enrolled / student calls staff endpoint | `Only this class' teachers can ...` / `You are not enrolled ...` |
| `404` | Class, schedule or session does not exist, or session does not belong to the class | `Session not found.` |
| `409` | Session is `cancelled`; slot already exists; weekly schedule overlaps; ad-hoc session lands on a holiday | `This session is cancelled.` / `A session already exists at this slot.` / `... is a holiday: ...` |
| `422` | Invalid weekday/time/window, `from > to`, range > 400 days, invalid status, student not in roster, duplicate `student_id` | Pydantic message / `Student is not an active member of this class.` |
| `400` | Attendance on a future session | `Attendance can only be recorded for today or past sessions.` |

Unknown IDs in a nested route must be checked against the parent:
`session.class_id == class_id`, otherwise `404` (not `403`).

---

## 4. Session range rules

Query parameters:

| Parameter | Default | Rule |
|---|---|---|
| `from` | `today - 7 days` | Inclusive |
| `to` | `today + 56 days` | Inclusive |
| range | — | `from <= to`; at most **400 days** |

`resolve_range()` owns defaults, clamping and validation. It uses the
Vietnam-local date. A range wider than 400 days returns `422`.

For the attendance summary and student history, the default range is the current
semester when the UI sends one; the API itself only applies the same defaults
above.

---

## 5. Aggregation contract

### 5.1 Attendance rate

```text
denominator = present + late + unexcused_absence + unmarked
rate        = (present + late) / denominator
```

* `excused_absence` is **not** in the denominator.
* `unmarked` counts as an absence.
* `rate = null` when `denominator = 0`.
* Only sessions with `status = 'scheduled'` and `date <= today` are counted.
* The summary uses the class's ACTIVE roster. A student with no rows still
  appears with zeros and `unmarked = total_sessions`.

### 5.2 Summary algorithm

1. Resolve and validate the date range.
2. Load the class's ACTIVE roster (enrollment `status = 'ACTIVE'`).
3. Load sessions in the range with `status = 'scheduled'` and `date <= today`.
4. Load all attendance rows for those sessions in one query.
5. Aggregate in Python (keeps SQLite tests simple and avoids a complex
   `FILTER`/`CASE` query).
6. Sort rows by `student_code`, then `full_name`.

### 5.3 Student history algorithm

1. Validate ACTIVE enrollment.
2. Resolve the range.
3. Load sessions in the range with `date <= today`; include `cancelled` and
   `rescheduled` so the student sees the real calendar.
4. Left-join the caller's attendance rows.
5. Sort by `start_at`.
6. Compute `attended` and `attendance_rate` with the same formula, treating an
   unmarked row as `unmarked` (not `excused_absence`).

---

## 6. Write path (attendance upsert)

For each record in `AttendanceBulkUpdate.records`:

1. Validate the session belongs to `{class_id}` and is not `cancelled`.
2. Reject when `session.date > today` → `400`.
3. Validate the student is an ACTIVE member of the class → `422`.
4. Look up `Attendance` by `(class_session_id, student_id)`.
5. If found: update `status`, `note`, `recorded_by`, `updated_at` (automatic).
6. If missing: insert with `recorded_by = current_user.id`.
7. Commit once after all rows; roll back on error.

Concurrency: the partial unique index
`uq_attendance_session_student` is the final guard. If two requests race, catch
`IntegrityError`, roll back, and retry the upsert once. Do not use PostgreSQL
`ON CONFLICT` so the SQLite test fixture keeps working.

The response is the refreshed `AttendanceSheetRead` for the same session, so the
UI can update the "Last saved" timestamp without a second request.

---

## 7. Session materialisation contract

`schedule_service.generate_sessions(db, clazz, date_from, date_to)`:

1. Load `class_schedule` rows for the class whose effective window intersects
   `[date_from, date_to]`.
2. Load existing `(date, schedule_id)` pairs in the range.
3. Load holidays via `holiday_service.holiday_dates(db, date_from, date_to, clazz.id)`.
4. For each schedule, walk every date in the range whose `weekday` matches:
   * skip when the date is outside `effective_from` / `effective_to`,
   * skip when the date is a holiday,
   * skip when `(date, schedule_id)` already exists,
   * insert `ClassSession` with:
     * `class_id = clazz.id`
     * `schedule_id = schedule.id`
     * `date = day`
     * `start_at = datetime.combine(day, schedule.start_time, tzinfo=VIETNAM_TZ)`
     * `end_at = datetime.combine(day, schedule.end_time, tzinfo=VIETNAM_TZ)`
     * `trainer_id = schedule.trainer_id`
     * `status = "scheduled"`
5. Commit if anything was inserted. On `IntegrityError`, roll back and retry
   once (another request may have materialised the same slot), then return the
   rows queried from the database.

Rules:

1. Only schedules effective on that date generate sessions.
2. Holidays (system + class scope) suppress the whole local day.
3. Materialisation is idempotent by `(date, schedule_id)`.
4. `cancelled` / `rescheduled` rows are never edited or deleted by the generator.
5. A session already present for the same `schedule_id` and date is not inserted
   again.
6. Ad-hoc sessions have `schedule_id = NULL` and still occupy
   `(class_id, date, start_at)`.

The generator runs inside `GET /sessions`; there is no cron job. It also runs
before the attendance summary reads so historical stats are complete.

---

## 8. Schedule validation

* **Overlap** (same class): two templates overlap when their weekday matches,
  their time ranges overlap (`start < other.end and end > other.start`), and
  their effective windows overlap. Return `409` with a human-readable message.
* **Trainer membership**: `trainer_id` must be the class's primary trainer or a
  co-trainer; otherwise `422`.
* **Delete**: deleting a template sets `class_session.schedule_id = NULL`
  through the existing FK (`ON DELETE SET NULL`) and leaves historical sessions
  intact.
* **Class scope**: a schedule/session for another class returns `404`.

---

## 9. Router layout

```text
app/routers/attendance/
├── __init__.py
├── router.py          # /api/classes/... (staff)
├── student_router.py  # /api/student/... (own history)
└── permissions.py     # require_class_staff / require_enrolled_student
```

Both routers are registered in `app/main.py`. Keeping the student router
separate makes it impossible for a student token to hit a staff route by
mistake.

Suggested route order inside the staff router:

1. `/schedules`
2. `/schedules/{schedule_id}`
3. `/sessions`
4. `/sessions/{session_id}/attendance`
5. `/attendance/summary`

FastAPI matches literal segments before path parameters only when the route
ordering is correct; verify `/attendance/summary` is not swallowed by
`/sessions/{session_id}`.

---

## 10. Test plan

### Service tests

`tests/test_schedule_service.py`:

* defaults and 400-day limit,
* weekday matching,
* effective window clipping,
* holiday suppression (system + class),
* idempotent re-run inserts zero duplicates,
* cancelled/rescheduled slots are never regenerated,
* ad-hoc holiday rejection,
* schedule overlap detection.

`tests/test_attendance_service.py`:

* sheet returns every ACTIVE student with `status = null` when unmarked,
* upsert inserts then updates; `updated_at` changes,
* partial update leaves other rows untouched,
* duplicate student IDs rejected at the schema layer,
* non-member student rejected,
* future session rejected,
* cancelled session rejected,
* rate formula excludes `excused_absence`, counts `unmarked`,
* rate is `null` for an empty denominator,
* summary ignores future sessions and non-scheduled statuses,
* student history never leaks another student's rows.

### Router tests

`tests/attendance/test_attendance_router.py` (SQLite + `TestClient`, following
`tests/quizzes/test_quiz_import_router.py`):

* 401 without a token,
* 403 for outsider / wrong role,
* 404 for a session from another class,
* happy path GET sheet → PUT → GET sheet,
* student endpoint returns only self,
* admin can read any class summary,
* response shape uses `date_from` / `date_to`.

### Manual checks

* Swagger: all nine routes listed under the two tags.
* `alembic heads` → exactly `0037_merge_cg_handover`.
* Fresh SQLite test database builds all tables from `Base.metadata.create_all`.

---

## 11. Out of scope

* Cancelling or rescheduling a session (AI-158 / later story).
* Trainer attendance rows (`attendance.trainer_id`) beyond what AI-155 already
  allows.
* Holiday CRUD (AI-140).
* Notifications / auto-absence jobs.
* Admin class-detail page; the admin UI only needs the summary endpoint.
* Per-session half-day holidays.
* New database migrations.

---

## 12. Open items to confirm before coding

| # | Item | Default in this design |
|---|---|---|
| O1 | `TEACHING_ASSISTANT` staff rights | No staff rights; enrollment-based student access only |
| O2 | Should `GET /sessions` be available to students? | Yes, own class only (needed by AI-157 later) |
| O3 | Should the summary count `rescheduled` sessions? | No; only `scheduled` |
| O4 | Maximum range | 400 days |
| O5 | Schedule overlap policy | Reject overlapping templates for the same class |

If the PO changes any default, update this document and the plan before the
implementation branch is merged.
