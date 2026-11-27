-- Кэш близких формулировок: контекст записи (всё из ключа, кроме вопроса) и вектор вопроса.
ALTER TABLE dialog_requests ADD COLUMN cache_context text, ADD COLUMN cache_embedding real[];
CREATE INDEX dialog_cache_context ON dialog_requests(cache_context, created_at DESC) WHERE cache_embedding IS NOT NULL;
