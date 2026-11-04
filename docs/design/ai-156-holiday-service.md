# AI-156 - Holiday Service & Vietnamese Holiday Seed

> **Ticket:** [AI-156](https://ai03.atlassian.net/browse/AI-156) (Epic AI-145: Class Schedule & Attendance) | **Priority:** High | **Story Points:** 5 SP
> **Status:** Implemented
> **Target Branch:** `features/Implementation_HolidayService`
> **Depends on:** AI-155 (`holiday` table)
> **Related Stories:** AI-136 (class sessions), AI-137 (ticket escalation), AI-140 (holiday admin CRUD), AI-157 (student schedule), AI-158 (absence + ticket handover).

---

## 1. Purpose

Provide one shared way to answer "is this date a holiday?" and to seed the
Vietnamese public holidays, so scheduling, attendance, ticket handover and the
ticket escalation SLA do not each implement their own calendar.

The `holiday` table from AI-155 is the **source of truth**. This story adds no
table and no migration.

### Acceptance Criteria (Jira AI-156)

* AC-01 - a reusable `is_holiday(date, class?)` check.
* AC-02 - solar holidays `1/1, 30/4, 1/5, 2/9, 20/11, 8/3, 20/10`.
* AC-03 - lunar holidays Tết Nguyên Đán and Giỗ Tổ Hùng Vương.
* AC-04 - holidays block sessions / absence counting / handover and do not
  consume the ticket escalation SLA.
* AC-05 - admin CRUD and compensatory working days belong to AI-140.

---

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Lunar conversion uses **`lunar-vn==1.4.0`** | Pure Python, no transitive dependencies, implements the Vietnamese calendar at UTC+7, supports 1900-2100. |
| D2 | Vietnam time is a fixed `UTC+7` offset, not `ZoneInfo("Asia/Ho_Chi_Minh")` | The offset has been stable since 1975 and this avoids a `tzdata` dependency on Windows containers. |
| D3 | Holidays are matched by **date only** and suppress the whole local day | Matches AI-155 C3; half-day holidays are out of scope. |
| D4 | `scope='system'` applies to every class; `scope='class'` only to its class | Keeps a class-specific day off without copying rows per class. |
| D5 | Seeding is **insert-only** and uses the `system_config` marker `holiday_seed_through_year` | A restart must not rename rows edited in AI-140 or resurrect holidays an admin deleted. |
| D6 | Seeding runs on startup for the **current and next year** | No migration with hardcoded dates; next year is ready before January. |
| D7 | The SLA subtracts only full holiday days, not weekends | Jira only asks for holidays; weekends remain a separate policy decision. |
| D8 | AC-04 is wired end-to-end for the ticket SLA; session/absence/handover get a shared guard | AI-136/AI-158 do not exist yet. `ensure_working_day()` is the hook they must call. |

---

## 3. Service API

`app/services/holiday_service.py`

```python
VIETNAM_TZ: timezone                       # UTC+7
SOLAR_HOLIDAYS: tuple[(month, day, name)]  # AC-02
LUNAR_HOLIDAYS: tuple[(month, day, name)]  # AC-03

local_date(moment: datetime) -> date
get_holiday(db, day, class_id=None) -> Holiday | None
is_holiday(db, day, class_id=None) -> bool
holidays_between(db, start, end, class_id=None) -> list[Holiday]
holiday_dates(db, start, end, class_id=None) -> set[date]
ensure_working_day(db, day, class_id=None) -> None   # raises HolidayBlocked
ensure_year_holidays(db, year) -> int                # idempotent insert
seed_holidays(db, years=None, *, today=None) -> int
business_elapsed(start, end, holidays) -> timedelta
```

`get_holiday` returns a class-scoped row before a system row on the same date.
With `class_id=None` only system holidays match.

`ensure_working_day` is the shared guard for AI-136 (session creation), AI-158
(auto absence, handover) and any future scheduling path. Callers catch
`HolidayBlocked` and skip or reschedule the operation.

`app/services/vietnamese_lunar.py` isolates the library:

```python
lunar_date_to_solar(year, month, day) -> date
tet_date(year) -> date        # lunar 1/1
hung_kings_date(year) -> date # lunar 3/10
```

Golden values: Tết 2026 = `2026-02-17`, Giỗ Tổ 2026 = `2026-04-26`.

---

## 4. Seed behaviour

Startup calls `seed_holidays(db)` after `seed_database()` and
`fail_stale_processing()`. A failure is logged and does not block boot.

* Default target: current Vietnam year and the next year.
* Each year inserts the 7 solar rows and the 2 lunar rows resolved through
  `lunar-vn`.
* Existing `scope='system'` rows on the same date are left untouched.
* `system_config.holiday_seed_through_year` records the highest seeded year.
  Once it covers the target, a restart returns `0` without touching the table,
  so admin deletions survive.
* `ensure_year_holidays` is insert-only; renaming or moving a holiday is AI-140's
  job.

---

## 5. SLA integration (AC-04)

`escalate_stale_tickets()` keeps the wall-clock SQL cutoff as a cheap lower
bound, then recomputes:

```text
business_elapsed(start, now, holidays(ticket.class_id)) >= SLA
```

Only full Vietnam-local holiday days that overlap the ticket window are
removed. The window starts at `reopened_at` when present, otherwise
`created_at`, matching the existing reopen rule. Tickets on classes with no
holiday behave exactly as before, and weekends are unchanged.

The `now` parameter remains injectable for tests.

---

## 6. AC-04 follow-ups (not in this story)

| Consumer | Required call |
|---|---|
| AI-136 session generation | `ensure_working_day(db, session_date, class_id)` before creating a `class_session`. |
| AI-158 auto absence | skip sessions whose date `is_holiday(...)`, or call the guard. |
| AI-158 ticket handover | do not auto-create handover on a holiday; use the same guard. |

These consumers do not exist on `main` yet, so this story exports the helpers
instead of inventing their tables or APIs.

---

## 7. Files

| File | Change |
|---|---|
| `app/services/holiday_service.py` | New service, seed, guard and SLA time math. |
| `app/services/vietnamese_lunar.py` | New `lunar-vn` wrapper. |
| `app/services/ticket_service.py` | Holiday-aware `escalate_stale_tickets`. |
| `app/main.py` | `seed_holidays` in the lifespan. |
| `pyproject.toml`, `uv.lock` | Add `lunar-vn==1.4.0`. |
| `tests/test_holiday_service.py` | Service, seed, lunar, timezone and SLA-math tests. |
| `tests/tickets/test_ticket_router.py` | Escalation regression across a holiday. |

No model or Alembic change: AI-155 already created `holiday` and its indexes.

---

## 8. Verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_holiday_service.py tests\tickets\test_ticket_router.py tests\test_attendance_schema.py -q
.\.venv\Scripts\ruff.exe check app\services\holiday_service.py app\services\vietnamese_lunar.py app\services\ticket_service.py app\main.py tests\test_holiday_service.py tests\tickets\test_ticket_router.py
```

Known unrelated failures on `main`: four tests in
`tests/services/chat/test_decision_service.py` fail with the repository `.env`
because `decision_timeout_seconds` is below the Gemini SDK's 10s floor. They
are reproducible on `main` without this branch and are not caused by AI-156.
