# API Design & Implementation Specs — Login API (AI-25)

> **Ticket:** AI-25
> **Epic:** E2 - Authentication & Authorization
> **Status:** Planned

This document serves as the Design Specs and a hand-over reference detailing the API architecture and payload contracts for the Backend Login feature. The purpose is to provide context and guidance for the backend implementation to integrate with the frontend UI.

## 1. Overview

The backend needs to expose an authentication endpoint (`POST /auth/login`) that accepts user credentials, validates them, and returns a JWT access token along with the user's role.

For the initial phase, we will implement the authentication logic using mock data (matching the frontend mock logic). The structure will be production-ready, allowing easy replacement with database queries in later sprints.

## 2. API Contract

### 2.1. `POST /auth/login`

Authenticates a user and issues a JWT token.

**Request:**
- Headers: `Content-Type: application/json`
- Body:
```json
{
  "identifier": "student@academic.edu.vn",
  "password": "Student@123"
}
```

**Response (200 OK):**
```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR...",
  "role": "student"
}
```
*(Roles: `student`, `admin`, `trainer`)*

**Error Response (401 Unauthorized):**
```json
{
  "detail": "Invalid email or password."
}
```
*(Note: FastAPI conventionally uses `detail` for error messages instead of `message`)*

## 3. Implementation Plan

### 3.1. Schemas (`app/schemas/auth.py`)
- `LoginRequest`: Pydantic model with `identifier` (str) and `password` (str).
- `LoginResponse`: Pydantic model with `token` (str) and `role` (str).

### 3.2. Services (`app/services/auth.py`)
- `verify_credentials(identifier, password)`: Function to validate credentials. Initially, this will check against the hardcoded mock accounts:
  - `student@academic.edu.vn` / `Student@123` -> Role: `student`
  - `admin@academic.edu.vn` / `Admin@123` -> Role: `admin`
  - `teacher` / `Admin@123` -> Role: `trainer`
- `create_access_token(data)`: Function to generate JWT token using a secret key (from `app/config.py`).

### 3.3. Routing (`app/routers/auth.py`)
- Create a new router module for authentication.
- Register `POST /login` endpoint.
- Call the service layer to verify credentials and return the `LoginResponse`.
- Mount the router in `app/main.py` with the prefix `/auth`.

### 3.4. Security Config (`app/config.py`)
- Add `SECRET_KEY` and `ALGORITHM` (e.g. "HS256") to `Settings`.

## 4. Next Steps (Development)

1. Add `PyJWT` or `python-jose` to dependencies (`uv add pyjwt`).
2. Create the Pydantic schemas.
3. Implement the auth service with JWT generation and mock credential validation.
4. Expose the FastAPI route.
5. Test with curl/Postman, then update Frontend to use the real API (Lưu ý: Không xóa code mock ở Frontend mà dùng biến môi trường để bật/tắt dự phòng cho đến khi dự án hoàn thiện).
