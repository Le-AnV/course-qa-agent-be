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
        "course_code": "CS101",
        "class_code": "CS101-FA26-001",
        "class_name": "Introduction to Computer Science - Section 1",
        "semester": "Fall",
        "academic_year": "2026",
        "trainer_email": "teacher@academic.edu.vn",
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

        # 4. Ensure initial classes exist
        class_map: dict[str, Class] = {}
        for cl in INITIAL_CLASSES:
            clazz = db.query(Class).filter(Class.code == cl["class_code"]).first()
            course = course_map.get(cl["course_code"])
            trainer = user_map.get(cl["trainer_email"])
            if not course or not trainer:
                logger.error(
                    f"Course {cl['course_code']} or trainer {cl['trainer_email']} not found"
                )
                continue

            if not clazz:
                clazz = Class(
                    course_id=course.id,
                    trainer_id=trainer.id,
                    code=cl["class_code"],
                    name=cl["class_name"],
                    semester=cl["semester"],
                    academic_year=cl["academic_year"],
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
