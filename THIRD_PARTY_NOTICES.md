# Сторонние компоненты и наработки

Список ведётся по мере добавления зависимостей. Для каждого компонента указаны источник, версия, лицензия и роль в продукте.

## Библиотеки

| Компонент | Версия | Лицензия | Роль |
|---|---|---|---|
| psycopg и psycopg-binary (PyPI) | 3.3.9 | LGPL-3.0-only | Драйвер PostgreSQL |
| pgvector (PyPI) | 0.5.1 | MIT | Адаптер pgvector в окружении |
| HTTPX (PyPI) | 0.28.1 | BSD-3-Clause | Клиент Ollama |
| Pydantic / pydantic-settings (PyPI) | 2.14.4 / 2.16.3 | MIT | Модели и настройки приложения |
| pytest / pgserver (PyPI, разработка) | 9.2.0 / 0.1.4 | MIT / Apache-2.0 | Проверки и временная БД |
| Chainlit (с FastAPI, Starlette, Uvicorn) | 2.12.0 (0.165.0, 1.10.0, 0.54.0) | Apache-2.0 (MIT, BSD-3-Clause, BSD-3-Clause) | Веб-интерфейс чата |
| Jinja2 / python-multipart (PyPI) | 3.1.6 / 0.0.34 | BSD-3-Clause / Apache-2.0 | Страницы админки и приём загружаемых файлов |

## Модели

| Модель | Редакция | Лицензия | Роль |
|---|---|---|---|
| BAAI/bge-m3 (Hugging Face, поставка Ollama) | bge-m3:latest, digest `7907646426070047…` | MIT | Плотный вектор, 1024 измерения |

## Ранее созданные наработки

| Наработка | Что взято | Что доработано в спринте |
|---|---|---|
| НормАссист (PEf_norms_rag), demo/chainlit | перевод ru-RU.json, настройки config.toml, приём с боковой панелью и защитой от повторной отправки | адаптировано под роли, уточнения, обращения и контракт answer.py |

## Сервисы стенда

| Компонент | Версия | Лицензия | Роль |
|---|---|---|---|
| PostgreSQL / pgvector (образ pgvector/pgvector) | 16 / 0.8.7, digest в compose.yaml | PostgreSQL License | Хранилище, полнотекстовый и векторный поиск |
| Mailpit (axllent/mailpit) | digest `ed9b00c609e7…` в compose.yaml | MIT | Приём почты на стенде |
| Ollama (ollama/ollama) | 0.40.2 на проверочном стенде | MIT | Локальный сервер моделей |
