"""Seed initial default users and roles into PostgreSQL."""

import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models.course import Class, Course, Enrollment
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
        "student_code": "AS_0001",
    },
]

INITIAL_COURSES = [
    {
        "code": "CS101",
        "name": "Introduction to Computer Science",
        "description": "Fundamentals of computer science and programming.",
    },
]

INITIAL_CLASSES = [
    {
        "class_code": "CS101-FA26-001",
        "class_name": "Introduction to Computer Science - Section 1",
        "semester": "Fall",
        "academic_year": "2026",
        "trainer_email": "teacher@academic.edu.vn",
        "credits": 3,
    },
]

INITIAL_ENROLLMENTS = [
    {
        "class_code": "CS101-FA26-001",
        "student_email": "student@academic.edu.vn",
    },
]


def seed_database(db: Session) -> None:
    """Ensure core roles, users, courses, classes, and enrollments exist in database."""
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
        user_map: dict[str, User] = {}
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

            if u.get("student_code") and not user.student_code:
                user.student_code = u["student_code"]
                db.flush()

            user_map[user.email] = user

        # 3. Ensure initial courses exist
        course_map: dict[str, Course] = {}
        for c in INITIAL_COURSES:
            course = db.query(Course).filter(Course.code == c["code"]).first()
            if not course:
                course = Course(
                    code=c["code"],
                    name=c["name"],
                    description=c.get("description"),
                    status="ACTIVE",
                )
                db.add(course)
                db.flush()
                logger.info(f"Created course: {course.code}")
            course_map[course.code] = course

        # 4. Ensure initial classes exist (standalone — no course binding)
        class_map: dict[str, Class] = {}
        for cl in INITIAL_CLASSES:
            clazz = db.query(Class).filter(Class.code == cl["class_code"]).first()
            trainer = user_map.get(cl["trainer_email"])
            if not trainer:
                logger.error(f"Trainer {cl['trainer_email']} not found")
                continue

            if not clazz:
                clazz = Class(
                    trainer_id=trainer.id,
                    code=cl["class_code"],
                    name=cl["class_name"],
                    semester=cl["semester"],
                    academic_year=cl["academic_year"],
                    credits=int(cl.get("credits", 3)),
                    status="ACTIVE",
                )
                db.add(clazz)
                db.flush()
                logger.info(f"Created class: {clazz.code}")
            class_map[clazz.code] = clazz

        # 5. Ensure initial enrollments exist
        for e in INITIAL_ENROLLMENTS:
            clazz = class_map.get(e["class_code"])
            student = user_map.get(e["student_email"])
            if not clazz or not student:
                logger.error(
                    f"Class {e['class_code']} or student {e['student_email']} not found"
                )
                continue

            enrollment = (
                db.query(Enrollment)
                .filter(
                    Enrollment.class_id == clazz.id,
                    Enrollment.student_id == student.id,
                )
                .first()
            )
            if not enrollment:
                enrollment = Enrollment(
                    class_id=clazz.id,
                    student_id=student.id,
                    status="ACTIVE",
                )
                db.add(enrollment)
                db.flush()
                logger.info(f"Created enrollment: student {student.email} in class {clazz.code}")

        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Failed to seed initial data: {e}")


DEV_CLASS = {
    "code": "TEST101-A",
    "name": "Test Class A",
    "semester": "HK1",
    "academic_year": "2026-2027",
    "credits": 3,
}


def seed_dev_data(db: Session) -> None:
    """Seed a sample class/enrollment so every login role is testable.

    Idempotent: existing rows are reused. Covers the AC-03 login gate —
    the seed trainer owns TEST101-A and the seed student is enrolled in it.
    """
    try:
        seed_database(db)

        teacher = db.query(User).filter(User.email == "teacher@academic.edu.vn").first()
        clazz = db.query(Class).filter(Class.code == DEV_CLASS["code"]).first()
        if clazz is None and teacher is not None:
            clazz = Class(
                trainer_id=teacher.id,
                status="ACTIVE",
                **DEV_CLASS,
            )
            db.add(clazz)
            db.flush()
            logger.info(f"Created class: {clazz.code} (trainer={teacher.email})")

        student = db.query(User).filter(User.email == "student@academic.edu.vn").first()
        if clazz is not None and student is not None:
            enrolled = (
                db.query(Enrollment)
                .filter(Enrollment.class_id == clazz.id, Enrollment.student_id == student.id)
                .first()
            )
            if enrolled is None:
                db.add(Enrollment(class_id=clazz.id, student_id=student.id, status="ACTIVE"))
                db.flush()
                logger.info(f"Enrolled {student.email} in {clazz.code}")

        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Failed to seed dev data: {e}")


if __name__ == "__main__":
    import argparse

    from app.config import settings
    from app.core.database import get_session_local, init_engine

    parser = argparse.ArgumentParser(description="Seed roles/users (+ dev sample data).")
    parser.add_argument(
        "--dev", action="store_true", help="also seed TEST101 course/class/enrollment"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    init_engine(settings.database_url)
    session = get_session_local(settings.database_url)()
    try:
        if args.dev:
            seed_dev_data(session)
        else:
            seed_database(session)
    finally:
        session.close()
