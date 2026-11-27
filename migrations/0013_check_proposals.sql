-- Проверочные вопросы, предложенные моделью для документа без эталона (admin-p03).
-- status: proposed = ждёт решения эксперта; accepted = принят и входит в проверочные вопросы документа.
CREATE TABLE check_proposals (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id text NOT NULL REFERENCES documents (doc_id),
    version_no integer NOT NULL,
    question text NOT NULL,
    expected_point text NOT NULL,
    status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','accepted')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_by text,
    decided_at timestamptz
);
CREATE INDEX check_proposals_doc ON check_proposals (doc_id, status, id);
