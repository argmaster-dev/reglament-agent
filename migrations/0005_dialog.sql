CREATE TABLE dialog_profiles (
    email text PRIMARY KEY, department text, confirmed_at timestamptz
);
CREATE TABLE dialog_topics (
    user_email text NOT NULL, dialog_id text NOT NULL,
    state jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (user_email, dialog_id)
);
CREATE TABLE dialog_requests (
    id uuid PRIMARY KEY, user_email text NOT NULL, dialog_id text NOT NULL,
    message_id text, question text NOT NULL, restated text NOT NULL,
    answer jsonb NOT NULL, trace jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    rating boolean, feedback text, cache_key text,
    UNIQUE (user_email, dialog_id, message_id)
);
CREATE INDEX dialog_cache ON dialog_requests(user_email, cache_key) WHERE cache_key IS NOT NULL;
