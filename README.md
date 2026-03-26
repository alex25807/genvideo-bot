# Генерация видео через ProxyAPI + Sora (Telegram Bot)

Проект генерирует видео через ProxyAPI/OpenAI Sora и отправляет результат в Telegram.

## Возможности

- Пошаговый сценарий в чате: `prompt -> параметры -> запуск`
- Кнопки выбора `seconds / model / size`
- В Telegram добавлен выбор `reference_type` (`asset/style`) в настройках провайдера
- В Telegram можно отправлять фото в черновик как `referenceImages` (для Veo при включенном флаге)
- Подготовленный слой `provider` (сейчас: `sora`, расширяемо для Veo)
- Прогресс генерации в Telegram
- Отправка готового `video.mp4` в чат
- Показ стоимости генерации:
  - точной (через метод баланса ProxyAPI),
  - или оценочной (если у ключа нет доступа к балансу)
- Единая кредитная система по пользователям (bot + web)
- Админ-управление кредитами: команды и кнопочная панель

## Установка

```bash
pip install -r requirements.txt
```

## Настройка `.env`

Минимум (общий):

```env
PROXYAPI_KEY=...
TELEGRAM_BOT_TOKEN=...
```

### Режим `telegram_bot.py` (основной чат-бот)

```env
TELEGRAM_ALLOWED_CHAT_ID=123456789
TELEGRAM_ADMIN_CHAT_IDS=123456789

DEFAULT_MODEL=sora-2
DEFAULT_SECONDS=4
DEFAULT_SIZE=1280x720
POLL_INTERVAL=3
TELEGRAM_UPLOAD_TIMEOUT=600

SORA2_PRICE_PER_SECOND_RUB=20
SORA2_PRO_PRICE_PER_SECOND_RUB=30

DEFAULT_NEW_CHAT_CREDITS=20
CREDITS_DB_PATH=credits_db.json
```

### Режим `main.py` (CLI + уведомления в Telegram)

```env
TELEGRAM_CHAT_ID=123456789
```

`main.py` использует `TELEGRAM_CHAT_ID`, а если его нет — берет `TELEGRAM_ALLOWED_CHAT_ID`.

Если ботом пользуешься сам в личке, обычно можно поставить один и тот же ID:

```env
TELEGRAM_CHAT_ID=123456789
TELEGRAM_ALLOWED_CHAT_ID=123456789
TELEGRAM_ADMIN_CHAT_IDS=123456789
```

## Запуск

**Web + Telegram одной командой** (локально, Windows/macOS/Linux):

```bash
python run_mvp.py
```

На Windows можно запустить `run_mvp.bat` из корня проекта.

Если бот «молчит», а сайт открывается — проверьте доступ к Telegram API:

```bash
python diagnose_telegram.py
```

Отдельно:

```bash
python telegram_bot.py
```

Быстрая автопроверка (unit):

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

CLI-режим:

```bash
python main.py --prompt "Закат над океаном" --seconds 4 --telegram-enable
# (опционально) python main.py --provider sora --prompt "..." --seconds 4
```

Веб-интерфейс (Flask):

```bash
python app.py
```

После запуска открой `http://localhost:5000`.

Авто-очистка для веб-задач настраивается через `.env`:

```env
WEB_TASK_RETENTION_HOURS=168
WEB_CLEANUP_INTERVAL_SECONDS=600
```

## Команды бота

- `/start`, `/help`
- `/status`
- `/myid`
- `/credits`
- `/admin [chat_id]` - кнопочная админ-панель кредитов
- `/add_credits <chat_id> <amount>` - админ-команда
- `/set_credits <chat_id> <amount>` - админ-команда
- `/set_web_token <chat_id> <token>` - привязать web token к пользователю
- `/remix <source_video_id> <prompt>` - ремикс завершенного видео

## Flask API роуты

- `POST /generate` - запускает задачу и возвращает `task_id`
- `GET /providers` - возвращает capabilities провайдеров для web-формы
### Экспериментальный Veo provider (опционально)

Для включения Gemini Veo через ProxyAPI:

```env
ENABLE_VEO_PROVIDER=1
```

Для включения режима `referenceImages` (до 3 изображений) дополнительно:

```env
ENABLE_VEO_REFERENCE_IMAGES=1
```

Текущая реализация Veo поддерживает text-to-video с polling и скачиванием результата.
Ограничения текущей версии:

- пока без remix;
- поддерживается `input_reference` (single image, image-to-video);
- поддерживаются `referenceImages` при `ENABLE_VEO_REFERENCE_IMAGES=1`:
  - `asset`: до 3 изображений
  - `style`: ровно 1 изображение
- для `referenceImages`: `seconds=8` и `size=1280x720`;
- пока без `lastFrame`;
- поддерживаемые seconds: `4`, `6`, `8`;
- поддерживаемые модели: `veo-3.1-generate-preview`;
- поддерживаемые размеры: `1280x720` и `720x1280`.

- `GET /status/<task_id>` - возвращает статус (`queued/in_progress/completed/failed`) и прогресс
- `GET /download/<task_id>` - скачивание готового mp4
- `GET /content/<task_id>` - поток видео для встроенного плеера
- `GET /credits?client_token=...` - текущий баланс кредитов для web

Видео сохраняются на сервере в каталоге `web_videos`.
Статусы задач хранятся в SQLite-файле `web_tasks.db`, поэтому после обновления страницы и после перезапуска Flask можно запросить статус/скачивание по `task_id` (если файл видео не удален).
Старые задачи (`completed`/`failed`) и соответствующие видеофайлы автоматически удаляются по TTL.

## Независимость сервисов

`telegram_bot.py` и `app.py` работают независимо:

- можно запускать только бот,
- можно запускать только веб,
- можно запускать оба одновременно в разных процессах/терминалах.

## Docker (web + bot)

Сборка и запуск:

```bash
docker compose up --build -d
```

Остановка:

```bash
docker compose down
```

Логи:

```bash
docker compose logs -f web
docker compose logs -f bot
```

Важно: `web` и `bot` должны использовать общий `BILLING_DB_PATH` (в `docker-compose.yml` это уже настроено через общий volume `billing_data`).

## Deploy на VPS

Готовые команды: `deploy/vps-commands.md`  
Готовые systemd-юниты: `deploy/systemd/video-web.service`, `deploy/systemd/video-bot.service`

## Railway или VPS?

Если у тебя уже есть два деплоя на Railway, для текущего проекта обычно лучше начать с Railway:

- быстрее выкатка и меньше DevOps-рутины;
- легко разделить на 2 сервиса: `web` и `bot`;
- удобно управлять env-переменными.

Когда переходить на VPS:

- нужен жесткий контроль затрат;
- нужен полный контроль над persistent storage;
- рост нагрузки и потребность в более тонкой оптимизации.

## Как считается списание кредитов

- `sora-2`, `4 сек` = `1` кредит
- `sora-2`, `8 сек` = `2` кредита
- `sora-2`, `12 сек` = `3` кредита
- `sora-2-pro` умножает стоимость в `2` раза

Пример: `sora-2-pro` + `8 сек` = `4` кредита.

## Единый биллинг (bot + web)

Биллинг хранится в SQLite `billing.db` (`BILLING_DB_PATH`).

- В Telegram пользователь идентифицируется по `chat_id`.
- В web пользователь идентифицируется по `client_token`.
- Связка делается админом в боте:

```text
/set_web_token <chat_id> <token>
```

После этого в web при запуске `/generate` нужно передавать этот `client_token`
(в форме есть поле `Client token`).

## Web мини-админка

Добавлен интерфейс:

- `GET /admin` — просмотр пользователей и ledger
- `POST /admin/action` — начисление/установка кредитов и установка web token
- `GET /admin/export` — выгрузка CSV (provider summary + top errors), поддерживает фильтр `user_id`

Доступ защищён переменной:

```env
WEB_ADMIN_TOKEN=change-me-admin-token
```

Открытие:

```text
https://your-domain/admin?token=WEB_ADMIN_TOKEN
```

Health-check для мониторинга:

- `GET /health` — статус сервиса (tasks DB, billing DB, providers, config) + `release_version` + `uptime_seconds`

Для контроля версий в деплое можно передавать переменную окружения:

```env
APP_VERSION=2026-02-27.1
```

## Платежи (T-Банк, опционально)

Добавлены backend endpoints для оплаты пакетов кредитов:

- `GET /payments/packages` — доступные пакеты (из `PAYMENT_PACKAGES_JSON` или defaults)
- `POST /payments/create` — создать платеж (`client_token`, `package_id`)
- `GET /payments/status/<payment_id>` — статус платежа и флаг `credits_applied`
- `GET /payments/last` — последний платеж пользователя по `client_token`
- `GET /payments/recent` — последние платежи пользователя (`limit`, по умолчанию 3)
- `POST /payments/webhook/tbank` — webhook подтверждения, идемпотентное начисление кредитов

Web UI:

- На главной странице добавлен блок покупки пакета (выбор пакета, кнопка оплаты, авто-проверка статуса).

Telegram bot:

- `/buy` — показать пакеты кредитов кнопками.
- `Проверить оплату` (кнопка) или `/paycheck` — проверить последнюю оплату и начислить кредиты при `CONFIRMED`.
- `/web` — отправить персональную ссылку на web с автоподстановкой `client_token`.

Локальный mock-режим для разработки:

```env
APP_ENV=dev
MOCK_PAYMENTS_ENABLED=1
APP_BASE_URL=http://127.0.0.1:5000
MOCK_PAYMENTS_TOKEN=local-mock-token
```

В этом режиме:

- реального списания денег нет;
- mock-режим включается только при `APP_ENV=dev|development|local|test`;
- `POST /payments/create` создает mock payment;
- ссылка оплаты ведет на локальную страницу `/mock-pay/<payment_id>`;
- если задан `MOCK_PAYMENTS_TOKEN`, страница `/mock-pay/*` защищается этим токеном;
- там можно вручную нажать `Подтвердить оплату` или `Отменить оплату`;
- после `Подтвердить оплату` кредиты начисляются так же, как в обычном flow.

Минимальные env:

```env
TBANK_TERMINAL_KEY=...
TBANK_PASSWORD=...
```

## Точная стоимость в рублях

Чтобы бот показывал **точную** сумму расхода, у API-ключа ProxyAPI нужно включить разрешение на метод баланса (`Запрос баланса`).

Если доступ не выдан, бот показывает оценку стоимости на основе:

- `SORA2_PRICE_PER_SECOND_RUB`
- `SORA2_PRO_PRICE_PER_SECOND_RUB`

## Smoke checklist (перед релизом)

- `sora`: текстовый prompt, `4s`, проверить статус, скачивание и отправку в Telegram/web.
- `veo` text-to-video: `8s`, модель `veo-3.1-generate-preview`, проверить polling и download.
- `veo` + `input_reference` (single image): проверить успешный запуск.
- `veo` + `referenceImages asset` (2-3 изображения, при `ENABLE_VEO_REFERENCE_IMAGES=1`): проверить успешный запуск.
- `veo` + `referenceType=style` (ровно 1 изображение): проверить успех и ошибку при 2 изображениях.
- Проверить guard-ошибки: `seconds!=8` или `size!=1280x720` для `referenceImages`.

