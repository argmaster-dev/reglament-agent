CREATE TABLE ingest_jobs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id text NOT NULL,
    card jsonb NOT NULL,
    filename text NOT NULL,
    data bytea NOT NULL,
    sha256 text NOT NULL,
    status text NOT NULL DEFAULT 'parsed'
        CHECK (status IN ('parsed','queued','running','ready','failed','duplicate')),
    stage text NOT NULL DEFAULT 'parse',
    done integer NOT NULL DEFAULT 0,
    total integer NOT NULL DEFAULT 0,
    stats jsonb NOT NULL DEFAULT '{}'::jsonb,
    selfcheck jsonb,
    error text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ingest_jobs_status ON ingest_jobs (status, id);
