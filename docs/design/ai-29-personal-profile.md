# API Design — Personal Profile Management (AI-29)

> **Ticket:** AI-29
> **Epic:** E1 — User Management (AI-23)
> **Status:** Draft

As a student, trainer, admin or teaching assistant, I want to view and edit
my profile (display name, password) so that my account information stays
accurate.

---

## 1. Overview

The backend exposes profile endpoints under `/api/auth`, guarded by the
existing Bearer JWT (`get_current_user` in `app/core/deps.py`). Profile data
lives in the ERD tables from AI-119: `user.full_name` + `user.email` for
display, `account.password_hash` for credentials. Password helpers
(`hash_password` / `verify_password`, bcrypt) already exist in
`app/core/security.py` — no new crypto needed.

AC-01 is half-covered today: `GET /api/auth/me` already returns
`{id, username, email, role, class_id, is_active}` (verified on main).
This spec adds the missing write side.

---

## 2. AC Mapping

| AC | Requirement | Endpoint |
|----|-------------|----------|
| AC-01 | View my own display name and email | `GET /api/auth/me` (exists — verify + test) |
| AC-02 | Changing password requires the old password, stored hashed | `POST /api/auth/change-password` (new) |
| AC-03 | Display name updates apply immediately in the UI | `PATCH /api/auth/profile` (new) + FE re-reads `/me` |

---

## 3. API Contract

All endpoints require `Authorization: Bearer <JWT>`. Missing/invalid token
→ `401` via the existing `get_current_user` dependency (no per-endpoint
auth code).

### 3.1. `GET /api/auth/me` (exists)

No contract change. Response (200 OK):

```json
{
  "id": "e79ab55e-578b-4c25-a7eb-xxxxxxxxxxxx",
  "username": "System Administrator",
  "email": "admin@academic.edu.vn",
  "role": "admin",
  "class_id": null,
  "is_active": true
}
```

### 3.2. `PATCH /api/auth/profile` (new)

Updates the caller's own display name only. Email, role and class are
admin-managed (AI-31) and NOT editable here.

**Request:**

```json
{
  "full_name": "Nguyen Van A"
}
```

Validation: non-empty after strip, max 100 chars (matches
`user.full_name VARCHAR(100)`).

**Response (200 OK):** the updated `MeResponse` (same shape as 3.1).

**Error Responses:**
- **422:** empty name or longer than 100 chars.

### 3.3. `POST /api/auth/change-password` (new)

**Request:**

```json
{
  "old_password": "Admin@123",
  "new_password": "N3w-Secure-Pass"
}
```

Flow:
1. Load the caller's `account` row (`account.user_id == user.id`).
2. `verify_password(old_password, account.password_hash)` — on failure
   return **401** with the generic login message (never reveal whether the
   old password was "close").
3. `account.password_hash = hash_password(new_password)` + commit.

Validation: `new_password` min 8 chars, must differ from old.

**Response (200 OK):**

```json
{
  "message": "Password changed successfully."
}
```

**Error Responses:**
- **401:** old password is wrong (generic message).
- **404:** no active account row (data inconsistency — same handling as login).
- **422:** new password too short or identical to old.

---

## 4. Files to Touch (Implementation)

- `app/schemas/auth.py` — add `ProfileUpdateRequest{full_name}`,
  `ChangePasswordRequest{old_password, new_password}`,
  `MessageResponse{message}`.
- `app/routers/auth/router.py` — add `PATCH /profile` + `POST /change-password`
  on the existing `router` (DB router, not `mock_router`).
- `tests/test_profile.py` (new) — SQLite StaticPool pattern from
  `tests/test_auth_db.py`: view me, update name (200 + persisted + `/me`
  reflects), update name empty (422), change password happy path (old stops
  working, new works on `/api/auth/login`), wrong old (401), short/same
  new (422).
- FE (later story, out of this BE scope): `profile-panel.tsx` wired to these
  endpoints; replaces the hardcoded mock in `teacher/account`.

---

## 5. Out of Scope

- Email change (admin-managed, AI-31).
- Role / class change (RBAC, AI-27 / AI-31).
- Avatar upload (no ERD column; future story if needed).
- `POST /auth/login` legacy mock contract — untouched.
