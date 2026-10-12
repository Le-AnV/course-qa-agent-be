# API Design & Implementation Specs — RBAC (AI-27)

> **Ticket:** AI-27
> **Epic:** E2 - Authentication & Authorization
> **Status:** Planned

This document serves as the Design Specs and implementation plan for the Role-Based Access Control (RBAC) feature on both the Backend and Frontend.

## 1. Overview

With the Login API (AI-25) already issuing JWT tokens containing user roles, the next step is to enforce access control based on those roles. 
- **Backend:** We need to verify incoming tokens, extract the user's role, and block unauthorized access to protected endpoints (returning `401` or `403`).
- **Frontend:** We need to integrate with the real Login API, store the token securely, and use the role to guard UI routes (e.g., preventing a student from accessing `/admin`).

## 2. Backend Implementation Plan

### 2.1. Token Verification Dependency (`app/dependencies/auth.py`)
- Create a new dependency file to handle incoming requests.
- **`get_current_user`**: A FastAPI `Depends` function that:
  1. Extracts the token from the `Authorization: Bearer <token>` header.
  2. Uses `jwt.decode` with the `SECRET_KEY` to validate the token.
  3. Returns the decoded user payload (identifier and role).
  4. Raises `401 Unauthorized` if the token is missing, expired, or invalid.

### 2.2. Role Checker Dependencies
- Create specific dependency wrappers to enforce roles:
  - `require_admin(user = Depends(get_current_user))` -> raises `403 Forbidden` if role is not `admin`.
  - `require_trainer(...)` -> raises `403` if role is not `trainer` or `admin`.
  - `require_student(...)` -> allows access for `student`.

### 2.3. Applying to Routes
- Example usage on future endpoints:
  ```python
  @router.get("/admin-only-data")
  def get_admin_data(user: dict = Depends(require_admin)):
      return {"message": "Welcome Admin!"}
  ```

## 3. Frontend Integration Plan

### 3.1. Connect Real API in `login-modal.tsx`
- Remove the `setTimeout` mock logic.
- Implement an HTTP request (using `fetch` or `axios`) pointing to `POST /auth/login`.
- Upon success (200 OK):
  1. Save the `token` in `localStorage` or `cookies`.
  2. Save the `role` to manage UI state.

### 3.2. Route Guarding (Middleware / HOC)
- Ensure the Next.js App Router utilizes the saved role to restrict access.
- If a user tries to manually navigate to `/admin` but has a `student` role, redirect them to a generic `Not Authorized` page or back to `/student`.

## 4. Next Steps

1. Create `app/dependencies/auth.py` in the Backend and write the token decoding logic.
2. Update the Frontend `login-modal.tsx` to call the real API.
3. Test the full flow: Login on UI -> Receive Token -> Store Token -> Access Protected Route.
