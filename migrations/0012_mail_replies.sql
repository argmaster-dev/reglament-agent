-- Ответ эксперта по почте: журнал обработанных писем (идемпотентность), источник ответа,
-- подразделение записи FAQ «вне регламентов».
CREATE TABLE mail_replies (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_id text NOT NULL UNIQUE,
    ticket_id bigint REFERENCES tickets (id),
    sender text NOT NULL DEFAULT '',
    subject text NOT NULL DEFAULT '',
    sent_at timestamptz,
    outcome text NOT NULL DEFAULT 'processing' CHECK (outcome IN
        ('processing','answered','updated','user_reply','unmatched','own_mail','sender_rejected',
         'empty','closed_ticket','superseded','rejected')),
    answer_text text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX mail_replies_ticket ON mail_replies (ticket_id, sent_at);

ALTER TABLE ticket_answers
    ADD COLUMN source text NOT NULL DEFAULT 'cabinet' CHECK (source IN ('cabinet','email')),
    ADD COLUMN updated_at timestamptz;

ALTER TABLE faq_entries ADD COLUMN department text;
