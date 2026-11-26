CREATE TABLE ticket_counters (
    year integer PRIMARY KEY,
    last_value integer NOT NULL
);

CREATE TABLE tickets (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    number text NOT NULL UNIQUE CHECK (number ~ '^[0-9]{4}-[0-9]{4,}$'),
    user_email text NOT NULL,
    dialog_id text NOT NULL,
    message_id text,
    question text NOT NULL,
    restated_question text,
    reason text NOT NULL CHECK (reason IN
        ('no_basis','external_doc','conflict','low_confidence','clarify_exhausted','user_dispute')),
    profile jsonb NOT NULL DEFAULT '{}'::jsonb,
    clarifications jsonb NOT NULL DEFAULT '[]'::jsonb,
    candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','answered','closed_duplicate','closed_offtopic')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_email, message_id)
);
CREATE INDEX tickets_user_idx ON tickets (user_email, created_at DESC);

CREATE TABLE mail_outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticket_id bigint NOT NULL REFERENCES tickets (id),
    kind text NOT NULL CHECK (kind IN ('ticket_created','expert_answer')),
    to_addr text NOT NULL,
    cc_addr text,
    subject text NOT NULL,
    body text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','error')),
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    message_id_header text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX mail_outbox_due_idx ON mail_outbox (next_attempt_at) WHERE status <> 'sent';
CREATE UNIQUE INDEX mail_outbox_ticket_kind_created ON mail_outbox (ticket_id) WHERE kind = 'ticket_created';
