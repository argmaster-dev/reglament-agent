CREATE FUNCTION faq_text(question text, alts text[], answer text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT question || ' ' || coalesce(array_to_string(alts, ' '), '') || ' ' || answer $$;

CREATE TABLE faq_entries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    question text NOT NULL,
    alt_questions text[] NOT NULL DEFAULT '{}',
    answer text NOT NULL,
    basis_kind text NOT NULL CHECK (basis_kind IN ('points','outside','document')),
    doc_id text REFERENCES documents (doc_id),
    version_id bigint REFERENCES document_versions (version_id),
    basis jsonb NOT NULL DEFAULT '[]'::jsonb,
    author_email text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','review','deleted')),
    ticket_id bigint REFERENCES tickets (id),
    embedding vector(1024) NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('russian'::regconfig, faq_text(question, alt_questions, answer))) STORED,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    reviewed_at timestamptz
);
CREATE INDEX faq_entries_tsv ON faq_entries USING gin (tsv);
CREATE INDEX faq_entries_hnsw ON faq_entries USING hnsw (embedding vector_cosine_ops);
CREATE INDEX faq_entries_status ON faq_entries (status, id);

CREATE TABLE ticket_answers (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticket_id bigint NOT NULL UNIQUE REFERENCES tickets (id),
    expert_email text NOT NULL,
    answer text NOT NULL,
    basis_kind text NOT NULL CHECK (basis_kind IN ('points','outside','document')),
    basis jsonb NOT NULL DEFAULT '[]'::jsonb,
    visibility text NOT NULL CHECK (visibility IN ('faq','private')),
    faq_id bigint REFERENCES faq_entries (id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE chat_notifications (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_email text NOT NULL,
    dialog_id text,
    ticket_number text NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz
);
CREATE INDEX chat_notifications_pending ON chat_notifications (user_email, id) WHERE delivered_at IS NULL;

ALTER TABLE mail_outbox DROP CONSTRAINT IF EXISTS mail_outbox_kind_check;
ALTER TABLE mail_outbox ADD CONSTRAINT mail_outbox_kind_check
    CHECK (kind IN ('ticket_created','expert_answer'));
