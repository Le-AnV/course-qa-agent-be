# AI-83 — Trainer ticket inbox API design

## Purpose and scope

Give an authenticated trainer a paginated inbox of student tickets for classes assigned to that trainer. Support filtering by class and ticket status. Trainer replies, notifications, and status transitions are separate tasks and are outside this contract.

## Endpoint

### `GET /api/tickets`

Query parameters:

| Parameter | Type | Default | Rules |
|---|---|---:|---|
| `class_id` | UUID | all assigned classes | Optional; must be assigned to the caller |
| `status` | `OPEN`, `ANSWERED`, `RESOLVED` | all statuses | Optional enum filter |
| `page` | integer | 1 | Minimum 1 |
| `page_size` | integer | 20 | Range 1–100 |

The authenticated user must have the `TRAINER` role. Determine authorized classes from the existing trainer/class assignment service, including co-trainer assignments. If `class_id` is provided but not assigned, return `403` before querying or counting tickets. Without a class filter, constrain every query to the trainer's assigned class IDs. An empty assignment returns an empty page and total 0.

Sort newest first by `created_at DESC`, then `id DESC` as a stable tie-breaker. Apply status and class filters before both total count and pagination.

Response:

```json
{
  "items": [
    {
      "id": "<ticket UUID>",
      "class_id": "<class UUID>",
      "student_id": "<student UUID>",
      "question": "<question>",
      "agent_answer": null,
      "status": "OPEN",
      "created_at": "<ISO-8601>",
      "student_name": "<display name>",
      "class_name": "<class name>",
      "class_code": "<class code>"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

Return `403` for non-trainers and unauthorized class filters. Return `200` with an empty `items` array when no rows match. Authentication failures use the existing auth dependency behavior.

## Security and query behavior

- Apply trainer class authorization on the server for every request; frontend filters do not grant access.
- Never count or return tickets from unassigned classes.
- Join student and class records for the display fields while preserving the same authorization predicate.
- Keep status enum validation at the API boundary.

## Verification

Cover assigned-class access (including co-trainer), foreign-class rejection, role rejection, all three status values, combined filters, deterministic newest-first ordering, totals after filtering, pagination boundaries, and empty results. Implementation command: `uv run pytest tests/tickets -q`.
