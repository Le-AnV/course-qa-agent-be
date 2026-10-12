-- AI Course Syllabus & Q&A Agent
-- Canonical PostgreSQL schema based on docs/design/ai-119-core-data-model.md
-- Run against a NEW, empty database. This script is intentionally not destructive.
-- Qdrant stores vector embeddings; PostgreSQL stores relational metadata and chunk text.

BEGIN;

CREATE TABLE role (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(50) NOT NULL UNIQUE,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_role_name CHECK (
        name IN ('STUDENT', 'TEACHING_ASSISTANT', 'TRAINER', 'ADMIN')
    )
);

CREATE TABLE permission (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE role_permission (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role_id UUID NOT NULL REFERENCES role(id) ON DELETE CASCADE,
    permission_id UUID NOT NULL REFERENCES permission(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_role_permission UNIQUE (role_id, permission_id)
);

CREATE TABLE "user" (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name VARCHAR(100) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    role_id UUID NOT NULL REFERENCES role(id) ON DELETE RESTRICT,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ,
    avatar_url TEXT,
    CONSTRAINT ck_user_status CHECK (status IN ('ACTIVE', 'LOCKED', 'INACTIVE'))
);

CREATE TABLE account (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES "user"(id) ON DELETE CASCADE,
    password_hash VARCHAR(255) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE course (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(50) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_course_status CHECK (status IN ('ACTIVE', 'INACTIVE'))
);

CREATE TABLE class (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    course_id UUID NOT NULL REFERENCES course(id) ON DELETE RESTRICT,
    trainer_id UUID NOT NULL REFERENCES "user"(id) ON DELETE RESTRICT,
    code VARCHAR(50) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,
    semester VARCHAR(20) NOT NULL,
    academic_year VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_class_status CHECK (status IN ('ACTIVE', 'LOCKED', 'INACTIVE'))
);

CREATE TABLE enrollment (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    class_id UUID NOT NULL REFERENCES class(id) ON DELETE RESTRICT,
    student_id UUID NOT NULL REFERENCES "user"(id) ON DELETE RESTRICT,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    enrolled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_enrollment_class_student UNIQUE (class_id, student_id),
    CONSTRAINT ck_enrollment_status CHECK (status IN ('ACTIVE', 'INACTIVE'))
);

CREATE TABLE document (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    class_id UUID NOT NULL REFERENCES class(id) ON DELETE RESTRICT,
    uploaded_by UUID NOT NULL REFERENCES "user"(id) ON DELETE RESTRICT,
    file_name VARCHAR(255) NOT NULL,
    file_type VARCHAR(20) NOT NULL,
    file_size BIGINT NOT NULL CHECK (file_size >= 0),
    file_path VARCHAR(500) NOT NULL,
    approval_status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    processing_status VARCHAR(20) NOT NULL DEFAULT 'NOT_STARTED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_document_file_type CHECK (
        file_type IN ('PDF', 'DOCX', 'MD', 'TXT', 'PPT', 'PPTX', 'XLSX', 'XLS')
    ),
    CONSTRAINT ck_document_approval_status CHECK (
        approval_status IN ('PENDING', 'APPROVED', 'REJECTED')
    ),
    CONSTRAINT ck_document_processing_status CHECK (
        processing_status IN ('NOT_STARTED', 'PROCESSING', 'DONE', 'FAILED')
    )
);

CREATE TABLE document_chunk (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    class_id UUID NOT NULL REFERENCES class(id) ON DELETE RESTRICT,
    content TEXT NOT NULL,
    page_number INTEGER,
    chapter VARCHAR(100),
    source_type VARCHAR(20) NOT NULL,
    embedding_status VARCHAR(20) NOT NULL DEFAULT 'NOT_STARTED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_chunk_page_number CHECK (page_number IS NULL OR page_number > 0),
    CONSTRAINT ck_chunk_source_type CHECK (
        source_type IN ('PDF', 'DOCX', 'MD', 'TXT', 'PPT', 'PPTX', 'XLSX', 'XLS')
    ),
    CONSTRAINT ck_chunk_embedding_status CHECK (
        embedding_status IN ('NOT_STARTED', 'PROCESSING', 'DONE', 'FAILED')
    )
);

CREATE TABLE chat_session (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    class_id UUID NOT NULL REFERENCES class(id) ON DELETE RESTRICT,
    title VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE chat_message (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    used_web_search BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_chat_message_role CHECK (
        role IN ('USER', 'ASSISTANT', 'SYSTEM')
    )
);

CREATE TABLE citation (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL REFERENCES chat_message(id) ON DELETE CASCADE,
    document_chunk_id UUID NOT NULL REFERENCES document_chunk(id) ON DELETE RESTRICT,
    page_number INTEGER,
    relevance_score NUMERIC(5,4),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_citation_page_number CHECK (page_number IS NULL OR page_number > 0),
    CONSTRAINT ck_citation_relevance CHECK (
        relevance_score IS NULL OR relevance_score BETWEEN 0 AND 1
    ),
    CONSTRAINT uq_citation_message_chunk UNIQUE (message_id, document_chunk_id)
);

CREATE TABLE ticket (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE RESTRICT,
    class_id UUID REFERENCES class(id) ON DELETE RESTRICT,
    title VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_ticket_status CHECK (
        status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'CLOSED')
    )
);

CREATE TABLE ticket_reply (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID NOT NULL REFERENCES ticket(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE RESTRICT,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE app_config (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT,
    description TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes used by class-scoped access, document lookup, chat history, and tickets.
CREATE INDEX idx_class_course_id ON class(course_id);
CREATE INDEX idx_class_trainer_id ON class(trainer_id);
CREATE INDEX idx_enrollment_class_id ON enrollment(class_id);
CREATE INDEX idx_enrollment_student_id ON enrollment(student_id);
CREATE INDEX idx_document_class_id ON document(class_id);
CREATE INDEX idx_document_uploaded_by ON document(uploaded_by);
CREATE INDEX idx_document_chunk_document_id ON document_chunk(document_id);
CREATE INDEX idx_document_chunk_class_id ON document_chunk(class_id);
CREATE INDEX idx_chat_session_user_id ON chat_session(user_id);
CREATE INDEX idx_chat_session_class_id ON chat_session(class_id);
CREATE INDEX idx_chat_message_session_id ON chat_message(session_id);
CREATE INDEX idx_citation_message_id ON citation(message_id);
CREATE INDEX idx_citation_document_chunk_id ON citation(document_chunk_id);
CREATE INDEX idx_ticket_user_id ON ticket(user_id);
CREATE INDEX idx_ticket_class_id ON ticket(class_id);
CREATE INDEX idx_ticket_reply_ticket_id ON ticket_reply(ticket_id);
CREATE INDEX idx_ticket_reply_user_id ON ticket_reply(user_id);

-- Seed the canonical roles. There is deliberately no public self-registration flow.
INSERT INTO role (name, description) VALUES
    ('STUDENT', 'Student account provisioned by an administrator or bulk import.'),
    ('TEACHING_ASSISTANT', 'Teaching assistant; permissions must be assigned explicitly.'),
    ('TRAINER', 'Trainer responsible for assigned classes and their learning materials.'),
    ('ADMIN', 'Administrator responsible for platform management.');

-- Starter permissions. Adjust the list to match the team's approved authorization matrix.
INSERT INTO permission (name, description) VALUES
    ('COURSE_READ', 'View courses.'),
    ('COURSE_MANAGE', 'Create and manage courses.'),
    ('CLASS_READ', 'View classes.'),
    ('CLASS_MANAGE', 'Create and manage classes and trainer assignments.'),
    ('USER_READ', 'View user accounts.'),
    ('USER_MANAGE', 'Manage accounts and bulk imports.'),
    ('DOCUMENT_READ', 'View authorized class documents.'),
    ('DOCUMENT_UPLOAD', 'Upload documents to assigned classes.'),
    ('DOCUMENT_APPROVE', 'Approve or reject documents before indexing.'),
    ('DOCUMENT_DELETE', 'Remove documents from assigned classes.'),
    ('CHAT_USE', 'Ask questions and view personal chat history.'),
    ('TICKET_CREATE', 'Create support tickets.'),
    ('TICKET_REPLY', 'Reply to support tickets.'),
    ('TICKET_MANAGE', 'Manage ticket status and assignment.'),
    ('CONFIG_MANAGE', 'Manage application configuration.');

-- Least-privilege starter mapping. Teaching assistant permissions are intentionally
-- not granted until the team confirms the TA requirements.
INSERT INTO role_permission (role_id, permission_id)
SELECT r.id, p.id
FROM role r
JOIN permission p ON p.name IN (
    'COURSE_READ', 'CLASS_READ', 'DOCUMENT_READ', 'CHAT_USE', 'TICKET_CREATE'
)
WHERE r.name = 'STUDENT';

INSERT INTO role_permission (role_id, permission_id)
SELECT r.id, p.id
FROM role r
JOIN permission p ON p.name IN (
    'COURSE_READ', 'CLASS_READ', 'DOCUMENT_READ', 'DOCUMENT_UPLOAD',
    'DOCUMENT_APPROVE', 'DOCUMENT_DELETE', 'CHAT_USE', 'TICKET_CREATE',
    'TICKET_REPLY', 'TICKET_MANAGE'
)
WHERE r.name = 'TRAINER';

INSERT INTO role_permission (role_id, permission_id)
SELECT r.id, p.id
FROM role r
JOIN permission p ON p.name IN (
    'COURSE_READ', 'COURSE_MANAGE', 'CLASS_READ', 'CLASS_MANAGE',
    'USER_READ', 'USER_MANAGE', 'DOCUMENT_READ', 'DOCUMENT_UPLOAD',
    'DOCUMENT_APPROVE', 'DOCUMENT_DELETE', 'CHAT_USE', 'TICKET_CREATE',
    'TICKET_REPLY', 'TICKET_MANAGE', 'CONFIG_MANAGE'
)
WHERE r.name = 'ADMIN';

COMMIT;
