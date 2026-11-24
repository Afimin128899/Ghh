# Session Shop

Telegram-бот для миграции и очистки **собственных** Telegram-сессий. Бот принимает
`.session` или ZIP с парами `.session`/`.json`, ставит задания в SQLite-очередь и
возвращает результат владельцу.

> **Важно:** очистка необратимо меняет профиль, контакты, диалоги, каналы,
> приватность и активные авторизации аккаунта. Сначала сделайте резервную копию и
> используйте проект только с аккаунтами, которыми вы вправе управлять.

## Что исправлено

- восстановлена полная логика бота — прежний `app.py` был обрезан посередине;
- доступ разрешён только ID из `ADMIN_IDS` и только в личном чате;
- `.env`, `.session`, SQLite, карантин и временные архивы исключены из Git;
- ZIP проверяется на выход из каталога, симлинки, шифрование и превышение лимитов;
- задания атомарно забираются из SQLite и восстанавливаются после перезапуска;
- исходные `.session` удаляются после обработки или отмены;
- недоступный настроенный прокси не заменяется скрытым прямым подключением;
- коды входа и 2FA-сообщения удаляются из чата и не показываются в статусах;
- миграция выполняется последовательно, а параллельность применяется только к
  режиму Fast Clean;
- inline-кнопки используют custom emoji и цвета Bot API 9.4 с автоматическим
  Unicode fallback;
- реферальные сценарии выключены по умолчанию.

## Требования

- Python 3.11 или новее;
- Telegram API ID и API hash с `my.telegram.org`;
- токен Telegram-бота;
- числовой Telegram ID владельца.

## Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Заполните в `.env` как минимум:

- `API_ID`;
- `API_HASH`;
- `BOT_TOKEN`;
- `ADMIN_IDS` — один ID или несколько через запятую.

Затем запустите воркер:

```bash
python app.py
```

Опциональный HTTP health check:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

`main.py` не запускает Telegram polling. Сам бот должен работать на постоянном
worker-процессе; serverless-платформа вроде Vercel для него не подходит.

## Docker

```bash
docker build -t session-shop .
docker run --rm --env-file .env -v session-shop-data:/app/data \
  -e DB_FILE=/app/data/sessions.db \
  -e UPLOAD_DIR=/app/data/uploads \
  -e QUARANTINE_DIR=/app/data/quarantine session-shop
```

Так очередь, исходные загрузки и карантин переживут перезапуск контейнера.

## Основные настройки

- `DEFAULT_AUTO_CODE`: `0` — ручная миграция, `1` — автокод, `2` — Fast Clean;
- `CONCURRENT_WORKERS_PER_CHAT`: параллельность только для Fast Clean;
- `MAX_QUEUE_PER_USER`: максимальное число активных заданий;
- `MAX_UPLOAD_BYTES`: лимит загружаемого файла;
- `MAX_ARCHIVE_MEMBERS`: лимит записей в ZIP;
- `MAX_ARCHIVE_UNCOMPRESSED_BYTES`: лимит распакованного содержимого;
- `SESSION_TIMEOUT_MINUTES`: ожидание кода или 2FA;
- `DB_FILE`: путь к SQLite;
- `UPLOAD_DIR`: каталог исходных загрузок, нужных очереди;
- `QUARANTINE_DIR`: каталог проблемных сессий.

## Custom emoji в кнопках

Кнопки меню используют настоящие `icon_custom_emoji_id`, а не emoji в строке:

- режим — ⚙️;
- Fast Clean — ⚡️;
- новая сессия — 📄;
- карантин — 📁;
- отмена — ❌;
- пропуск — ⬅️.

Основные действия дополнительно получают цвета `primary`, `success` и `danger`.
Функция включена через `ENABLE_CUSTOM_BUTTON_EMOJI=true`. Для неё нужен Telegram
Premium у владельца бота либо дополнительный username с Fragment. Если Telegram
отклонит custom emoji, бот автоматически повторит запрос с обычными Unicode-иконками.

Собственные ID можно задать без изменения кода:

```env
CUSTOM_BUTTON_EMOJI_OVERRIDES=mode=1234567890123456789,cancel=9876543210987654321
```

Допустимые ключи: `mode`, `fast`, `new`, `failed`, `cancel`, `skip`, `success`,
`info`. Чтобы полностью отключить custom emoji, установите
`ENABLE_CUSTOM_BUTTON_EMOJI=false`.

Автоматизации `GameUGSbot` и `redcubebetbot` активируются только явными
переменными `ENABLE_UGS_REFERRAL=true` и `ENABLE_RC_REFERRAL=true` вместе с
соответствующими параметрами запуска.

## Проверки

Быстрые тесты не требуют подключения к Telegram:

```bash
python -m compileall -q app.py main.py session_shop tests
python -m unittest discover -s tests -v
```

## Безопасность секретов

Удаление `.env` из текущего коммита не удаляет его из старой истории Git. Если
реальные значения уже публиковались:

1. отзовите и перевыпустите `BOT_TOKEN` через BotFather;
2. замените остальные доступные ключи;
3. отдельно очистите историю репозитория (`git filter-repo`) и выполните
   согласованный force-push.

Не добавляйте в issues, логи или коммиты содержимое `.env`, `.session`, JSON с
2FA-паролями и файлы базы данных. После получения результата удаляйте сообщение с
архивом из Telegram: вложение содержит действующую авторизацию аккаунта.
