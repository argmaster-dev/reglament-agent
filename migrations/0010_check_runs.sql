-- Прогоны проверочных вопросов службы качества по документу (до и после замены версии).
CREATE TABLE check_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id text NOT NULL REFERENCES documents (doc_id),
    version_no integer,
    started_by text NOT NULL,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','done','failed')),
    total integer NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    error text,
    results jsonb NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX check_runs_doc ON check_runs (doc_id, id DESC);
