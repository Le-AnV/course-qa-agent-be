# AI-137 — Ticket escalation to backup trainer (API design)

**Status:** Draft for review
**Related:** AI-81 (student ticket API), AI-83 (trainer inbox API), AI-89 (ticket lifecycle), AI-137 (this task)
**Implementation branch:** `features/Implementation_TicketEscalationAndPreview`

---

## 1. Scope

AI-137 adds escalation metadata to an open ticket that has waited longer than the
configured SLA. The ticket is assigned to the class backup trainer in the read model so
trainers can see who is expected to pick it up.

In scope:

- Persist the escalation target and escalation timestamp.
- Resolve the backup trainer from the class co-trainer assignment.
- Expose the escalation fields in every ticket read/list response.
- Run the escalation sweep lazily on existing ticket read/write paths.

Out of scope:

- Changing ticket `status`.
- Reassigning the primary trainer or transferring ownership.
- Sending email, in-app, or push notifications.
- A background scheduler or cron job.
- A separate escalation endpoint.

---

## 2. Actors and permissions

| Actor | Can trigger sweep | Can see escalation fields |
|---|---|---|
| Student (ticket owner) | Yes, via own ticket detail | Yes, on own ticket |
| Trainer (assigned or co-trainer) | Yes, via inbox list or reply | Yes, on assigned-class tickets |
| Admin | No ticket UI/API access | No |
| Unauthenticated | No | No |

Existing ticket authorization is unchanged. The frontend is not an authorization
boundary; class and role checks stay server-side.

---

## 3. Escalation rule

### SLA configuration

| Item | Value |
|---|---|
| Config key | `ticket_escalation_sla_hours` in `system_config` |
| Default | `24` hours |
| Minimum | `1` hour |
| Maximum | `167` hours (`TICKET_EXPIRATION` minus one hour) |
| Invalid value | Fall back to the default |
| Config missing | Use the default |

The cap keeps escalation earlier than the seven-day expiry sweep, so an escalated
ticket is still `OPEN` when it becomes visible.

### Eligible ticket

A ticket is escalated only when all conditions are true:

```text
status = 'OPEN'
coalesce(reopened_at, created_at) <= now - escalation_sla
escalated_at IS NULL
```

- `reopened_at` is the fresh anchor after a student follow-up or a reopen.
- `ANSWERED`, `RESOLVED`, and `EXPIRED` tickets are never escalated.
- Escalation is one-shot per ticket. Once `escalated_at` is set, later sweeps do not
  change the target or timestamp.
- A ticket created less than the SLA ago is not escalated.

### Backup trainer selection

The backup trainer is the first `class_trainer` row for the ticket's class whose
`trainer_id` differs from the class primary `trainer_id`, ordered by:

```text
class_trainer.created_at ASC, class_trainer.id ASC
```

If the class has no co-trainer, the ticket remains unescalated. The sweep does not
invent a target and does not fall back to an admin.

### Sweep timing

The sweep is lazy and idempotent. It is invoked by:

- `GET /api/tickets` (trainer inbox list)
- `POST /api/tickets/{id}/replies` for an assigned trainer
- `GET /api/tickets/{id}` for the student owner

`escalated_at` records the sweep time, not the exact moment the SLA elapsed. A read
path that does not invoke the sweep can show the ticket before escalation until one of
the paths above runs.

---

## 4. Data model and migration

### `ticket` columns

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `escalated_to_id` | UUID, FK `user.id` | Yes | Backup trainer selected by the sweep. |
| `escalated_at` | `timestamptz` | Yes | When the sweep assigned the ticket. |

- FK: `fk_ticket_escalated_to_id_user`, `ON DELETE SET NULL`.
- Index: `ix_ticket_escalated_to_id`.
- `escalated_to_name` is not stored. It is derived from `user.full_name` through the
  `escalated_to` relationship.

### Migration

- Revision: `0034_ticket_escalation`
- Depends on: `0033_ticket_reopened_at`
- Upgrade: add both columns, the FK, and the index using `batch_alter_table`.
- Downgrade: drop the index, FK, and both columns.
- Additive only; existing tickets keep `NULL` until a sweep selects them.

---

## 5. API contract

No endpoint is added. The following read models gain three nullable fields:

```json
{
  "escalated_to_id": "uuid-or-null",
  "escalated_at": "2026-11-04T10:00:00Z-or-null",
  "escalated_to_name": "Co Trainer-or-null"
}
```

Affected responses:

| Endpoint | Model |
|---|---|
| `GET /api/tickets` | `TicketListResponse.items[]` (`TicketInboxItem`) |
| `GET /api/tickets/mine` | `TicketListResponse.items[]` (`TicketInboxItem`) |
| `GET /api/tickets/{id}` | `TicketThreadRead` |
| `POST /api/tickets` | `TicketRead` |
| `POST /api/tickets/{id}/replies` | Created reply plus refreshed ticket in the client flow |

Semantics:

- All three fields are `null` before escalation.
- `escalated_to_id` and `escalated_at` are set together by the sweep.
- `escalated_to_name` is `null` only when the target user cannot be resolved.
- A deleted target user sets `escalated_to_id` to `NULL`; `escalated_at` remains as a
  historical marker.

### Example inbox item

```json
{
  "id": "3f0f2d1e-8d8f-4f3a-9f0e-1c2b3a4d5e6f",
  "class_id": "455d574e-7022-450f-8550-d22286ef5ff0",
  "student_id": "b5b9f6a7-5f7a-4b0e-9c5d-2a1f0e9d8c7b",
  "question": "Why does this pointer fail?",
  "status": "OPEN",
  "created_at": "2026-11-03T09:00:00Z",
  "escalated_to_id": "d2f1c0b9-a8e7-4d6c-8b5a-1f0e9d8c7b6a",
  "escalated_at": "2026-11-04T10:00:00Z",
  "escalated_to_name": "Co Trainer"
}
```

---

## 6. Errors and edge cases

| Case | Behaviour |
|---|---|
| No backup trainer | Ticket stays `OPEN` with null escalation fields. |
| Config value non-numeric | Default 24 hours is used. |
| Config below 1 | Clamped to 1 hour. |
| Config above 167 | Clamped to 167 hours. |
| Ticket already escalated | No update on later sweeps. |
| Ticket reopened after escalation | Escalation fields remain; the ticket is not escalated again. |
| Target user deleted | FK sets `escalated_to_id` to null; history remains in `escalated_at`. |
| Archived class | The sweep does not filter class status; existing inbox scoping still hides archived classes. |
| Concurrent sweeps | Both may read the same candidate; the persisted target is stable because the first commit wins and later sweeps see `escalated_at`. |

No new HTTP status code is introduced. Existing `401`, `403`, `404`, `409`, and `422`
behaviour is unchanged.

---

## 7. AC mapping

| Acceptance criterion | Implementation |
|---|---|
| Stale open ticket is escalated to the backup trainer | `escalate_stale_tickets` + `_backup_trainer_id`. |
| Fresh or answered ticket is not escalated | Status and SLA filters in the sweep query. |
| Escalation is visible to trainers | `escalated_to_id`, `escalated_at`, `escalated_to_name` in inbox items. |
| Escalation does not change lifecycle status | Sweep writes only escalation columns. |
| SLA is configurable | `ticket_escalation_sla_hours` system config, clamped 1..167. |
| Reopen starts a fresh window | Sweep uses `coalesce(reopened_at, created_at)`. |
| Missing backup trainer is safe | Sweep skips the ticket. |
| Repeated reads are idempotent | `escalated_at IS NULL` guard. |

---

## 8. Verification plan

- Escalate an `OPEN` ticket older than 24 hours to the co-trainer.
- Leave a ticket inside the SLA unescalated.
- Leave an `ANSWERED` ticket unescalated even when old.
- Leave a class without a co-trainer unescalated.
- Confirm a second sweep does not change the target or timestamp.
- Configure a 2-hour SLA and verify the boundary.
- Configure 200 hours and verify the 167-hour cap; verify expiry still wins after 7 days.
- Set `reopened_at` to one hour ago on an old ticket and verify it is not escalated.
- Confirm the migration has a single Alembic head and both columns are nullable.
- Confirm the existing ticket suite remains green: `uv run --no-sync python -m pytest tests/tickets -q`.

---

## 9. Open decisions

1. **Re-escalation after reopen:** currently one-shot per ticket. If a reopened ticket
   should escalate again, the sweep must clear `escalated_at` on reopen and define
   whether the same backup trainer is reused.
2. **Archived classes:** the sweep currently processes all open tickets regardless of
   class status. The inbox still hides archived classes; whether to skip them at sweep
   time is a product decision.
3. **Notifications:** escalation is metadata only. Email/in-app notification is not part
   of AI-137.
