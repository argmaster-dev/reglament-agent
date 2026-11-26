# Финальный стенд на ноутбуке

Финальная конфигурация публикует один порт на loopback для Tailscale Funnel. Caddy запрашивает общие учётные данные HTTP Basic на каждом внешнем маршруте, включая `/health`, `/admin` и WebSocket. После входа Caddy передаёт запросы приложения и WebSocket в Chainlit. Внутри приложения сотрудник и эксперт входят отдельно. Маршрут `/mail/` открывается после входа в службу качества, включая API и WebSocket Mailpit. В роли сотрудника и без сеанса этот маршрут возвращает 403. Служба проверки сеанса доступна Caddy во внутренней сети. Caddy проверяет сеанс обычным HTTP-запросом, затем передаёт исходный WebSocket upgrade в Mailpit для обновления списка писем.

## Подготовка

Docker Engine и Tailscale должны запускаться при загрузке системы. Для Docker Compose требуется поддержка `!reset` в файлах переопределения (2.24.4 или новее). Ollama должна иметь локальные модели из `MODEL_CHAT` и `MODEL_EMBED`. Файлы systemd рассчитаны на существующий бинарник `~/.local/ollama/bin/ollama` и стандартный каталог моделей `~/.ollama`.

Создайте `.env` с правами 600 из `.env.example`. Заполните `POSTGRES_PASSWORD`, `CHAINLIT_AUTH_SECRET` и `CHAT_EXPERT_PASSWORD` отдельными случайными значениями. Пароль БД без специальных символов можно получить командой `openssl rand -hex 24`, секрет сеанса — `openssl rand -hex 32`. Пустой `CHAT_EXPERT_PASSWORD` отключает вход эксперта и доступ к почте. Пять неправильных попыток закрывают вход эксперта на оставшуюся часть минутного окна независимо от адреса клиента.

Один раз подготовьте общий пароль в рабочей копии. Команда использует закреплённый образ Caddy. Если его нет локально, сначала загрузите образ, указанный в `compose.stand.yaml`.

```sh
python3 deploy/setup-basic.py
```

Сценарий сохраняет общий логин и пароль только в локальном `.env` с правами 600 и печатает статус. Caddy получает bcrypt-хэш и логин. Заголовок `Authorization` снимается перед приложением и Mailpit. Общий пароль вводится в браузере после перехода по HTTPS-адресу. Экспертский пароль вводится отдельно в форме службы качества. При замене общих учётных данных удалите прежние значения `STAND_BASIC_PASSWORD` и `STAND_BASIC_PASSWORD_HASH`, повторите подготовку и пересоздайте gateway.

В `.env` задайте `CONTAINER_OLLAMA_URL=http://172.28.71.1:11434`. Пользовательский сокет systemd открывается только на шлюзе сети стенда и передаёт трафик Ollama на loopback. При изменении подсети или адреса шлюза одновременно обновите `STAND_SUBNET`, `STAND_GATEWAY`, `CONTAINER_OLLAMA_URL` и `ListenStream` сокета.

```sh
install -d ~/.config/systemd/user
```

Установите три файла и включите пользовательские службы. Если на 11434 уже работает вручную запущенная Ollama, включите её службу для следующей загрузки и запустите сейчас только сокет прокси. При первой перезагрузке Ollama запустится из службы.

```sh
install -m 644 deploy/reglament-ollama.service deploy/reglament-ollama-proxy.service deploy/reglament-ollama-proxy.socket ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable reglament-ollama.service
systemctl --user enable --now reglament-ollama-proxy.socket
# Запустите, если на порту 11434 ещё нет процесса Ollama.
systemctl --user start reglament-ollama.service
# Один раз, если loginctl show-user "$(id -un)" --property=Linger показывает Linger=no.
sudo loginctl enable-linger "$(id -un)"
```

`enable-linger` запускает пользователя systemd без входа в рабочий стол. Ollama продолжает слушать на loopback и обслуживает один запрос генерации за раз.

## Запуск и публикация

Команды выполнять из каталога этой копии репозитория. Имя проекта Compose отделяет тома и сети стенда от стендов разработки.

```sh
export REGULATIONS_CADDYFILE_SHA256="$(sha256sum deploy/Caddyfile | cut -d ' ' -f 1)"
docker compose -p reglament-stand -f compose.yaml -f compose.stand.yaml up -d --build --wait
docker compose -p reglament-stand -f compose.yaml -f compose.stand.yaml ps --all
curl -fsS --user stand http://127.0.0.1:18800/health
```

В примере `curl` предложит ввести пароль без отображения. Если вы изменили `STAND_BASIC_USER`, укажите своё имя вместо `stand`. Запросы без общего пароля получают 401. Прокси ограничивает multipart-запрос загрузки 21 МиБ с учётом метаданных формы. Сам файл DOCX приложение принимает до 20 МиБ.

После обновления `deploy/Caddyfile` пересчитайте `REGULATIONS_CADDYFILE_SHA256`. При необходимости примените новую конфигурацию командой `docker compose -p reglament-stand -f compose.yaml -f compose.stand.yaml up -d --no-deps --force-recreate gateway`. Она пересоздаёт только прокси. Тома базы и Mailpit остаются на месте.

При первом запуске `migrate` должен завершиться с кодом 0. Compose дожидается доступности очереди PostgreSQL и ответа SMTP на NOOP у почтового процесса. Отправку письма проверяйте отдельно через новое обращение. Один сотрудник может выполнять один ход одновременно во всех своих вкладках. По умолчанию приложение принимает до четырёх параллельных ходов, 12 ходов в минуту на сотрудника и 36 ходов в минуту на процесс. У модели один активный слот и до трёх запросов в очереди. Значения заданы в `.env.example`. Уже созданные данные сохраняются в томах PostgreSQL и Mailpit. Для первичного наполнения чистой БД можно запустить обычный импорт выданных материалов внутри контейнера.

```sh
docker compose -p reglament-stand -f compose.yaml -f compose.stand.yaml exec -T app python -m reglament_agent.ingest load docx/
```

Владелец tailnet авторизуется через браузер командой `sudo tailscale up`. Для публикации может потребоваться включить HTTPS и разрешить Funnel в tailnet. Перед публикацией проверьте `tailscale funnel status`, чтобы не заменить работающий маршрут на том же порту tailnet. Проверьте 401 без общего пароля на `/`, `/health`, `/admin` и WebSocket чата. Введите общий пароль и проверьте чат, ограничения запросов и загрузку DOCX.

```sh
sudo tailscale funnel --bg http://127.0.0.1:18800
tailscale funnel status
```

Впишите выданный HTTPS-адрес в `MAIL_PUBLIC_BASE_URL` и повторите `docker compose ... up -d` с теми же именем проекта и файлами. Тогда письма содержат точный адрес карточки обращения. Для проверки с телефона отключите VPN и Wi-Fi, откройте HTTPS-адрес, задайте вопрос и откройте источник. Войдите экспертом, откройте `/mail/` и проверьте доставку обращения.

## После перезагрузки

```sh
systemctl is-active docker tailscaled
systemctl --user is-active reglament-ollama.service reglament-ollama-proxy.socket
tailscale funnel status
docker compose -p reglament-stand -f compose.yaml -f compose.stand.yaml ps --all
curl -fsS --user stand http://127.0.0.1:18800/health
```

Контейнеры с политикой `unless-stopped` запускаются вместе с Docker. `tailscale funnel --bg` сохраняет конфигурацию между перезапусками. Проверьте прежний HTTPS-адрес, запрос с источником и новое письмо. При отказе части приложения смотрите `checks` в `/health`, `docker compose ... logs --tail=100` для конкретного сервиса и `journalctl --user -u reglament-ollama.service`.

Чтобы снять публикацию этого маршрута, используйте `sudo tailscale funnel --https=443 off`. Для остановки контейнеров с сохранением данных используйте `docker compose -p reglament-stand -f compose.yaml -f compose.stand.yaml stop`.

- [Справка Tailscale Funnel](https://tailscale.com/docs/reference/tailscale-cli/funnel)
- [Параметры Mailpit](https://mailpit.axllent.org/docs/configuration/runtime-options/)
