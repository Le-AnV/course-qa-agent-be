# AI-87 — In-app ticket reply notification (API design)

**Status:** Implemented
**Related:** AI-81 (student tickets), AI-85 (trainer replies), AI-89 (ticket lifecycle), AI-137 (backup trainer)
**Implementation branch:** `features/Implementation_InAppNotification`

---

## 1. Scope

Notify a student in the app when a trainer replies to one of their tickets. The API exposes unread tickets and records when the student views a ticket. Trainer replies no longer queue ticket-reply emails. Account credential and password-reset emails use separate formatters and remain available.

No notification table, push channel, WebSocket, or email delivery is added for ticket replies. The client polls the existing ticket API.

---

## 2. Actors and permissions

| Actor | Read unread tickets | Mark a ticket seen | Reply to a ticket |
|---|---|---|---|
| Ticket owner (student) | Own active-class tickets | Own active-class tickets | Student follow-up through existing flow |
| Other student | Own tickets only | `404` for another student's ticket | Existing ticket rules |
| Trainer | `403` on `/mine` | `403` | Assigned ticket through existing flow |

The existing student-ticket query excludes archived classes. The seen endpoint uses the same ownership and class checks as the student ticket detail endpoint.

---

## 3. Data model and migration

`ticket.student_seen_at` is a nullable timezone-aware timestamp. A ticket has an unread trainer reply when at least one `ticket_reply` belongs to that ticket, its author differs from `ticket.student_id`, and its `created_at` is later than `student_seen_at`. A null seen timestamp means any trainer reply is unread.

Migration `0038_ticket_student_seen_at` follows `0037_merge_cg_handover`. It adds the column and initializes existing tickets to `CURRENT_TIMESTAMP`, so replies that predate this feature do not populate the new notification bell. New tickets start with a null seen timestamp.

Student follow-ups are excluded by the author check. Replies from a backup trainer count because they are also authored by someone other than the student.

---

## 4. API contract

### List student tickets

```http
GET /api/tickets/mine?unread=true&page=1&page_size=5
```

`unread` is optional and defaults to `false`. The response remains `TicketListResponse` with `items`, `total`, `page`, and `page_size`. With `unread=true`, both `items` and `total` are restricted to tickets with a newer trainer reply. Existing `class_id` and `status` filters still apply.

Ticket responses expose `student_seen_at` alongside the existing reply list. The client uses both values to show unread markers and the latest unread reply time.

### Mark a ticket seen

```http
POST /api/tickets/{ticket_id}/seen
```

The endpoint sets `student_seen_at` to the current UTC time and returns `204 No Content`. It returns `404` when the ticket is absent, belongs to another student, or is outside the active-class scope; a non-student receives `403`.

---

## 5. Reply and notification flow

1. A trainer reply is persisted and moves the ticket to `ANSWERED` through the existing reply endpoint.
2. The student client calls `/mine?unread=true` on entry, on window focus, and every 30 seconds.
3. The unread filter returns tickets with a trainer reply newer than `student_seen_at`.
4. Opening the ticket detail calls `/{ticket_id}/seen`; the next unread query no longer includes that reply.
5. A later trainer reply has a newer timestamp and makes the ticket unread again.

Only ticket-reply email generation and queueing were removed. The admin account creation and password-reset routes still call the shared email queue.

---

## 6. Errors and edge cases

| Case | Behavior |
|---|---|
| No trainer reply | Ticket is absent from the unread result. |
| Student follow-up only | Ticket is absent from the unread result. |
| Multiple trainer replies | Ticket remains unread until the student views it. |
| Seen endpoint called again | Timestamp is updated; response remains `204`. |
| Another student's ticket | Seen endpoint returns `404` without exposing ownership. |
| Archived class | Existing student-ticket scope excludes the ticket. |
| Reply email service fails | Ticket reply no longer depends on email delivery. |

---

## 7. AC mapping

| Criterion | API support |
|---|---|
| AC-01: in-app notification for a new reply | Unread filter and reply timestamps support client polling and badge state. |
| AC-02: notification opens ticket detail | Response includes ticket and class IDs for a client deep link; seen endpoint clears the unread state. |
| AC-03: ticket list shows latest status | Existing ticket response continues to expose status and replies. |

---

## 8. Verification scenarios

- Trainer reply makes the owner's ticket appear in `/mine?unread=true`; a student follow-up does not.
- Marking a ticket seen removes it from unread results; a later trainer reply makes it unread again.
- Another student receives `404` and a trainer receives `403` from the seen endpoint.
- Existing tickets receive a seen timestamp during migration; downgrade removes the column.
- Trainer replies do not create email-outbox entries; account and password-reset email paths remain separate.
