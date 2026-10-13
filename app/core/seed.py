"""Seed initial default users and roles into PostgreSQL."""

import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models.user import Account, Role, User

logger = logging.getLogger(__name__)

INITIAL_ROLES = [
    {
        "name": "STUDENT",
        "description": "Student account provisioned by an administrator or bulk import.",
    },
    {
        "name": "TEACHING_ASSISTANT",
        "description": "Teaching assistant; permissions must be assigned explicitly.",
    },
    {
        "name": "TRAINER",
        "description": "Trainer responsible for assigned classes and their learning materials.",
    },
    {"name": "ADMIN", "description": "Administrator responsible for platform management."},
]

INITIAL_USERS = [
    {
        "email": "admin@academic.edu.vn",
        "full_name": "System Administrator",
        "password": "Admin@123",
        "role": "ADMIN",
    },
    {
        "email": "teacher@academic.edu.vn",
        "full_name": "Academic Trainer",
        "password": "Teacher@123",
        "role": "TRAINER",
    },
    {
        "email": "student@academic.edu.vn",
        "full_name": "Academic Student",
        "password": "Student@123",
        "role": "STUDENT",
    },
]


def seed_database(db: Session) -> None:
    """Ensure core roles and initial mock/default users exist in database."""
    try:
        # 1. Ensure roles exist
        role_map: dict[str, Role] = {}
        for role_data in INITIAL_ROLES:
            role = db.query(Role).filter(Role.name == role_data["name"]).first()
            if not role:
                role = Role(name=role_data["name"], description=role_data["description"])
                db.add(role)
                db.flush()
                logger.info(f"Seeded role: {role.name}")
            role_map[role.name] = role

        # 2. Ensure initial users and accounts exist
        for u in INITIAL_USERS:
            user = db.query(User).filter(User.email == u["email"]).first()
            role = role_map.get(u["role"])
            if not role:
                logger.error(f"Role {u['role']} not found for user {u['email']}")
                continue

            if not user:
                user = User(
                    full_name=u["full_name"],
                    email=u["email"],
                    role_id=role.id,
                    status="ACTIVE",
                )
                db.add(user)
                db.flush()
                logger.info(f"Created user: {user.email} ({u['role']})")

            account = db.query(Account).filter(Account.user_id == user.id).first()
            if not account:
                account = Account(
                    user_id=user.id,
                    password_hash=hash_password(u["password"]),
                    is_active=True,
                )
                db.add(account)
                db.flush()
                logger.info(f"Created account with hashed password for: {user.email}")

        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Failed to seed initial data: {e}")
