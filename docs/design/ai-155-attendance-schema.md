# AI-155 — Attendance, Schedule, Holiday & Handover Data Model

> **Ticket:** [AI-155](https://ai03.atlassian.net/browse/AI-155) (Epic AI-145: Class Schedule & Attendance) | **Priority:** High | **Story Points:** 5 SP
> **Assignee:** Nguyễn Chí Trung
> **Status:** Design (not implemented)
> **Target Branch:** `features/Implementation_AttendanceSchema`
> **Related Stories:** AI-136 (class attendance per session), AI-137 (ticket escalation), AI-140 (holiday calendar + leave requests), AI-156 (holiday service/seed), AI-157 (student schedule view), AI-158 (teacher absence + ticket handover).

---

## 1. Purpose

Define the persistence layer shared by scheduling, attendance, holidays and
ticket handover so that AI-136/140/156/157/158 do not invent competing tables.

This story ships **schema only**: SQLAlchemy models + one Alembic migration +
model-level tests. No endpoints, no UI, no background job.

### Acceptance Criteria (Jira AI-155)

* `class_schedule` stores class, subject, weekday, start/end time, responsible teacher.
* `attendance` stores person (student or teacher), session, date, status
  (present / excused absence / unexcused absence / late), note, with exactly one
  record per person per session.
* `holiday` stores date, name, type (solar / lunar / admin-added), and scope
  (system or class).
* `ticket_handover` stores ticket, from teacher, to teacher, reason
  (auto_absent / manual_transfer), timestamp, and status (pending / accepted / rejected).
* Migration and required indexes are defined.

---

## 2. Decisions (confirmed)

| # | Decision | Why |
|---|---|---|
| D1 | `class_schedule` is a **recurring template**; `class_session` is a **materialised dated session**. | Attendance, `auto_absent` (AI-158) and weekly/monthly views (AI-157) need dated rows. Pure templates force every read to re-derive history. |
| D2 | Sessions are generated **lazily and idempotently** for a requested date range; no cron/background job. | Avoids a scheduler dependency in this sprint; the same code path serves the student view and the attendance write. |
| D3 | Generated sessions stay **editable**: reschedule, cancel, change teacher. | Real classes shift. Template-only (option B) was rejected because it cannot express a single moved session. |
| D4 | Table is named `class_session`, **not** `session` / `class_meeting`. | `chat_session` already exists; reusing "session" causes real confusion in code review and API payloads. |
| D5 | `attendance` references the person with **two nullable FKs** (`student_id`, `trainer_id`) plus a CHECK enforcing exactly one, not `(person_type, person_id)`. | Polymorphic ids cannot be enforced by the database. Two FKs keep referential integrity; the CHECK forbids both/neither. |
| D6 | Enumerations are `String(20)` + `CheckConstraint`, not native PG enums. | Matches the repo (`chat_message.role`); native enums make later value additions a migration. |
| D7 | `ticket_handover.ticket_id` lands **without a FK**; the FK is added by a follow-up migration once AI-81/AI-83 ship the `ticket` table. | The ticket table does not exist yet; blocking AI-137 on it is worse than a documented follow-up. |
| D8 | All timestamps are `TIMESTAMPTZ`; `weekday` is `SMALLINT` with `0 = Monday … 6 = Sunday` (Python `date.weekday()`). | Matches every other table in the repo and the Python convention used by the generator. |

### My calls (review these while reading)

* **C1** — `attendance` does **not** duplicate `date`; `class_session.date` is the
  single source of truth and the API returns it. Jira AC mentions "session, date"
  because the author assumed denormalised rows; duplicating the date invites drift.
* **C2** — Session generation horizon: `today − 7 days … today + 56 days`
  (1 past week for marking absence retroactively, 8 future weeks for the monthly view).
* **C3** — Holidays are matched by **date only**, ignoring class time: a holiday
  suppresses every session on that date. Half-day holidays are out of scope.
* **C4** — A `CHECK` must run on both PostgreSQL and the SQLite test fixture
  (`Base.metadata.create_all`), so the "exactly one person" rule is written as
  `(student_id IS NOT NULL) <> (trainer_id IS NOT NULL)`. `num_nonnulls()` is
  PostgreSQL-only and would make every SQLite `CREATE TABLE` fail.
* **C5** — `rescheduled` **never moves an existing row**. The original row keeps
  its `(class_id, date, start_at)` slot with `status='rescheduled'`, and the
  replacement is inserted as an ad-hoc session (`schedule_id IS NULL`). Mutation
  in place would free the template slot and make the next materialisation insert
  a phantom duplicate.
* **C6** — At most **one `pending` handover per ticket**
  (`UNIQUE INDEX (ticket_id) WHERE status='pending'`). A ticket has one owner at
  a time; rejected/accepted history stays queryable. AI-137 may relax this later.


---

## 3. Data model

```text
class_schedule                      # recurring template ("Mon 19:00-21:00")
  id                UUID PK
  class_id          UUID FK class.id            ON DELETE CASCADE
  subject           VARCHAR(100) NOT NULL       # class IS the subject; stored for label stability
  weekday           SMALLINT NOT NULL           # 0=Mon .. 6=Sun
  start_time        TIME NOT NULL
  end_time          TIME NOT NULL
  trainer_id        UUID FK user.id             ON DELETE RESTRICT   # responsible teacher
  effective_from    DATE NOT NULL               # inclusive
  effective_to      DATE NULL                   # NULL = open-ended
  created_at/updated_at TIMESTAMPTZ
  CHECK (weekday BETWEEN 0 AND 6)
  CHECK (end_time > start_time)
  CHECK (effective_to IS NULL OR effective_to >= effective_from)
  INDEX (class_id, weekday)

class_session                       # one dated occurrence
  id                UUID PK
  class_id          UUID FK class.id            ON DELETE CASCADE
  schedule_id       UUID FK class_schedule.id   ON DELETE SET NULL   # NULL = ad-hoc session
  date              DATE NOT NULL
  start_at          TIMESTAMPTZ NOT NULL
  end_at            TIMESTAMPTZ NOT NULL
  trainer_id        UUID FK user.id             ON DELETE RESTRICT
  status            VARCHAR(20) NOT NULL DEFAULT 'scheduled'  # scheduled | cancelled | rescheduled
  note              TEXT NULL
  created_at/updated_at TIMESTAMPTZ
  CHECK (status IN ('scheduled','cancelled','rescheduled'))
  CHECK (end_at > start_at)
  UNIQUE (class_id, date, start_at)              # idempotent materialisation
  INDEX (class_id, date)
  INDEX (date)

attendance
  id                UUID PK
  class_session_id  UUID FK class_session.id    ON DELETE CASCADE
  student_id        UUID FK user.id             ON DELETE CASCADE   NULL
  trainer_id        UUID FK user.id             ON DELETE CASCADE   NULL
  status            VARCHAR(20) NOT NULL        # present | excused_absence | unexcused_absence | late
  note              TEXT NULL
  recorded_by       UUID FK user.id             ON DELETE SET NULL  # who marked it
  created_at/updated_at TIMESTAMPTZ
  CHECK ((student_id IS NOT NULL) <> (trainer_id IS NOT NULL))
  CHECK (status IN ('present','excused_absence','unexcused_absence','late'))
  UNIQUE INDEX (class_session_id, student_id) WHERE student_id IS NOT NULL
  UNIQUE INDEX (class_session_id, trainer_id) WHERE trainer_id IS NOT NULL
  INDEX (student_id)

holiday
  id                UUID PK
  date              DATE NOT NULL
  name              VARCHAR(200) NOT NULL
  type              VARCHAR(20) NOT NULL        # solar | lunar | admin
  scope             VARCHAR(20) NOT NULL        # system | class
  class_id          UUID FK class.id            ON DELETE CASCADE   NULL   # required iff scope='class'
  created_at        TIMESTAMPTZ
  CHECK (type IN ('solar','lunar','admin'))
  CHECK (scope IN ('system','class'))
  CHECK ((scope = 'class' AND class_id IS NOT NULL) OR (scope = 'system' AND class_id IS NULL))
  UNIQUE INDEX (date) WHERE class_id IS NULL
  UNIQUE INDEX (class_id, date) WHERE class_id IS NOT NULL
  INDEX (date)

ticket_handover
  id                UUID PK
  ticket_id         UUID NOT NULL               # FK added after AI-81/AI-83
  from_trainer_id   UUID FK user.id             ON DELETE RESTRICT
  to_trainer_id     UUID FK user.id             ON DELETE RESTRICT
  reason            VARCHAR(20) NOT NULL        # auto_absent | manual_transfer
  status            VARCHAR(20) NOT NULL DEFAULT 'pending'  # pending | accepted | rejected
  created_at        TIMESTAMPTZ                 # "when" in the AC
  responded_at      TIMESTAMPTZ NULL
  CHECK (reason IN ('auto_absent','manual_transfer'))
  CHECK (status IN ('pending','accepted','rejected'))
  CHECK (from_trainer_id <> to_trainer_id)
  INDEX (ticket_id)
  UNIQUE INDEX (ticket_id) WHERE status = 'pending'   # one active owner per ticket
  INDEX (to_trainer_id, status)
```

Notes on the two partial unique indexes: PostgreSQL treats `NULL` as distinct, so a
plain `UNIQUE(class_session_id, student_id)` would let unlimited duplicate
trainer rows through. The same applies to `holiday` (system rows have
`class_id IS NULL`), hence the `WHERE` clauses. Both `postgresql_where` and
`sqlite_where` are set so the SQLite test suite enforces them too.

---

## 4. Materialisation rules (contract for AI-136 / AI-157 / AI-158)

1. `generate_sessions(class_id, date_from, date_to)` creates sessions for every
   active `class_schedule` row whose `weekday` matches, clipped by
   `effective_from` / `effective_to`.
2. A date is skipped when a `holiday` matches: `scope='system'` (any class) or
   `scope='class' AND class_id = :class_id`.
3. Insert is idempotent: `ON CONFLICT (class_id, date, start_at) DO NOTHING`.
   Re-running for an already materialised range is a no-op.
4. `status='cancelled'` and `status='rescheduled'` rows are never deleted and
   never regenerated; they suppress their slot permanently and stay in history.
5. Regeneration only replaces rows where **all** of these hold:
   `schedule_id IS NOT NULL`, `status='scheduled'`, `date > today`, and no
   `attendance` row references the session. Rule 4 wins over rule 5 — a
   cancelled or rescheduled row is never eligible.
6. Rescheduling = keep the original row as `rescheduled` on its slot + insert a
   replacement session with `schedule_id IS NULL` (ad-hoc), so the generator can
   never recreate the moved occurrence.
7. `auto_absent` (AI-158) must materialise the target range first, then read
   `class_session` rows; it never works from the template.

---

## 5. Migration

* Revision `0028_attendance_schema`, `down_revision = '0027_gemini_key_quota'`.
* `upgrade()` creates the four tables with indexes and CHECKs; `downgrade()`
  drops them in reverse dependency order.
* No data backfill — all four tables are new.
* Hand-written (repo style), not autogenerated; guarded with
  `sa.inspect(...).get_table_names()` so re-running against a partially
  migrated dev database is safe.
* Models are registered in `app/models/__init__.py` so the SQLite test fixture
  (`Base.metadata.create_all`) sees them.

---

## 6. Explicitly out of scope

* Endpoints and DTOs (AI-136 attendance marking, AI-157 schedule views).
* The generator's trigger points (which router calls it, caching the range).
* Holiday seeding for solar/lunar calendars (AI-156).
* Ticket model, ticket statuses, escalation SLA job (AI-81/83, AI-137).
* Leave requests for trainers (AI-140).
* Half-day holidays or per-session holiday exceptions (C3).

---

## 7. Testing plan

`tests/test_attendance_schema.py` (SQLite, `Base.metadata.create_all`):

1. Tables exist with the expected columns and types after `create_all`.
2. `attendance` rejects two `NULL` persons and rejects both persons set (CHECK).
3. `attendance` allows one student row and one trainer row in the same session.
4. Duplicate `(class_session_id, student_id)` raises `IntegrityError`.
5. `holiday` enforces `scope='system' → class_id IS NULL` and
   `scope='class' → class_id NOT NULL`; duplicate system date raises.
6. `ticket_handover` rejects `from_trainer_id = to_trainer_id` and a second
   `pending` row for the same `ticket_id`; a `rejected` row plus a new `pending`
   row is allowed.
7. `class_session` unique `(class_id, date, start_at)` blocks a double insert.
8. `class_session` rejects out-of-range `status` values, and a `cancelled` row
   still occupies its slot (`(class_id, date, start_at)` unique is enforced),
   which is what stops a materialiser from re-inserting it.
9. Migration: verified manually against PostgreSQL — clone of the dev schema
   stamped at `0027_gemini_key_quota`, `alembic upgrade head` creates all five
   tables, indexes and check constraints (`alembic check` reports no drift for
   them), and `downgrade -1` removes them cleanly. The full chain from an empty
   database does not replay because of pre-existing drift in older migrations,
   so CI does not run it.
