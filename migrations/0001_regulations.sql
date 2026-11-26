CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    doc_id text PRIMARY KEY,
    title text NOT NULL,
    department text NOT NULL,
    stages text[] NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    active_version_id bigint
);

CREATE TABLE document_versions (
    version_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id text NOT NULL REFERENCES documents,
    version_number integer NOT NULL CHECK (version_number > 0),
    sha256 text NOT NULL,
    filename text NOT NULL,
    uploaded_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    source_docx bytea NOT NULL,
    stats jsonb NOT NULL,
    UNIQUE (doc_id, sha256),
    UNIQUE (doc_id, version_number),
    UNIQUE (doc_id, version_id)
);
ALTER TABLE documents ADD FOREIGN KEY (doc_id, active_version_id)
    REFERENCES document_versions (doc_id, version_id);

CREATE TABLE points (
    point_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id text NOT NULL,
    version_id bigint NOT NULL,
    number text NOT NULL,
    level integer NOT NULL CHECK (level > 0),
    parent text,
    section_number text NOT NULL,
    section_title text NOT NULL,
    heading text NOT NULL,
    text text NOT NULL,
    blocks jsonb NOT NULL,
    sentences text[] NOT NULL,
    position integer NOT NULL,
    sha256 text NOT NULL,
    embedding vector(1024) NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('russian'::regconfig, text)) STORED,
    FOREIGN KEY (doc_id, version_id) REFERENCES document_versions (doc_id, version_id),
    UNIQUE (version_id, number),
    UNIQUE (version_id, position)
);
CREATE INDEX points_doc_hash ON points (doc_id, sha256);
CREATE INDEX points_tsv ON points USING gin(tsv);
CREATE INDEX points_hnsw ON points USING hnsw(embedding vector_cosine_ops);
