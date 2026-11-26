-- История проверок записей FAQ после изменения регламентов: кто, когда и что сделал.
CREATE TABLE faq_reviews (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    faq_id bigint NOT NULL REFERENCES faq_entries (id),
    actor text NOT NULL,
    action text NOT NULL CHECK (action IN ('to_review','auto_restore','confirm','edit','repin','delete')),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX faq_reviews_faq ON faq_reviews (faq_id, id);
