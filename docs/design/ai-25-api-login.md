# API Design & Implementation Specs — Login API (AI-25)

> **Ticket:** AI-25  
> **Epic:** E1 / E2 - Authentication & Authorization  
> **Status:** Implemented  

This document serves as the Technical Implementation Specification and hand-over reference for the Backend Login feature (`POST /auth/login`). The endpoint provides secure, database-driven authentication against PostgreSQL, issuing signed JWT access tokens containing user identities and roles.

---

## 1. Overview

The backend exposes an authentication endpoint (`POST /auth/login`) that accepts credentials (`identifier` and `password`), validates them directly against PostgreSQL database records using Bcrypt, and returns a signed JWT access token along with the user's role.

Mock data (`MOCK_USERS`) has been completely removed in favor of pure database authentication.

> **Update (AI-25 hotfix — `hotfix/Bug_MockLoginFallback`):** the legacy `POST /auth/login`
> contract has been **removed** — the FE login modal now calls `POST /api/auth/login` only.
> Contract for the live endpoint: `docs/design/ai-29-personal-profile.md` §3.1.

---

## 2. API Contract

### 2.1. `POST /auth/login`

Authenticates a user against PostgreSQL and issues a JWT token.

**Request:**
- Headers: `Content-Type: application/json`
- Body:
```json
{
  "identifier": "student@academic.edu.vn",
  "password": "Student@123"
}
```
*(Note: If `identifier` is supplied as a short username without `@`, e.g., `student` or `admin`, the domain `@academic.edu.vn` is automatically appended).*

**Response (200 OK):**
```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "role": "student"
}
```
*(Roles: `student`, `admin`, `trainer`)*

**Error Responses:**
- **401 Unauthorized:**
```json
{
  "detail": "Invalid email or password."
}
```
*(Note: To meet security requirement AI-25 AC, error messages deliberately do not reveal whether the email exists in the system).*

- **403 Forbidden:**
```json
{
  "detail": "Account is suspended."
}
```
*(Returned if the user account status is not `ACTIVE`).*

- **503 Service Unavailable:**
```json
{
  "detail": "Database service unavailable."
}
```

---

## 3. Architecture & Implementation

### 3.1. Database & Models (`app/models/user.py`, `app/core/database.py`)
- **`User` model**: Stores `id` (UUID), `email`, `full_name`, `role_id`, `status` (`ACTIVE`, `INACTIVE`, `SUSPENDED`), and `last_login_at`.
- **`Account` model**: Stores `user_id`, `password_hash`, and `is_active`.
- **`Role` model**: Defines system roles (`ADMIN`, `TRAINER`, `STUDENT`, `TEACHING_ASSISTANT`).
- **Session Dependency**: `get_db()` yields SQLAlchemy session with connection pooling.

### 3.2. Password Security (`app/core/security.py`)
- `hash_password(password: str) -> str`: Hashes passwords using `bcrypt` (12 rounds) with automatic salt generation.
- `verify_password(plain_password: str, hashed_password: str) -> bool`: Validates plain passwords against stored hashes.

### 3.3. Database Seeding (`app/core/seed.py`)
- Executed on application startup (`app/main.py` lifespan) if database is available:
  - Seeds default roles (`ADMIN`, `TRAINER`, `STUDENT`).
  - Provisions initial default accounts:
    - `admin@academic.edu.vn` / `Admin@123` (Role: `admin`)
    - `teacher@academic.edu.vn` / `Teacher@123` (Role: `trainer`)
    - `student@academic.edu.vn` / `Student@123` (Role: `student`)

### 3.4. Service Layer (`app/services/auth.py`)
- `verify_credentials(identifier, password, db)`:
  - Normalizes identifier (strips whitespace, lowercases).
  - Queries `User` table by email.
  - Verifies user status is `ACTIVE`.
  - Compares password against `Account.password_hash` using Bcrypt.
  - Updates `last_login_at` on successful authentication.
  - Returns `{"identifier": user.email, "role": role_name}`.
- `create_access_token(data: dict) -> str`:
  - Encodes payload (`sub`, `role`, `exp`) using `HS256` and `SECRET_KEY`.

### 3.5. Schemas & Router (`app/schemas/auth.py`, `app/routers/auth/router.py`)
- `LoginRequest`: Pydantic schema validating input fields.
- `LoginResponse`: Pydantic schema returning `token` and `role`.
- Mounted in FastAPI app under `/auth` prefix.

---

## 4. Automated Testing & Verification

Comprehensive automated tests in [tests/test_auth_db.py](file:///c:/Users/nghia/OneDrive/Desktop/FAcaProject/course-qa-agent-be/tests/test_auth_db.py):
1. **`test_database_connection`**: Validates PostgreSQL live health.
2. **`test_seed_database_and_verify_credentials`**: Validates database seeding, Bcrypt matching for Admin, Trainer, and Student, and rejection of invalid passwords.
3. **`test_auth_login_api_endpoint`**: Executes end-to-end `TestClient` HTTP POST request to `/auth/login`.

All tests verified and passing (100% pass rate).

