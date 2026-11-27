-- Общий кэш ответов ищется по ключу без адреса пользователя.
CREATE INDEX dialog_cache_shared ON dialog_requests(cache_key, created_at DESC) WHERE cache_key IS NOT NULL;
