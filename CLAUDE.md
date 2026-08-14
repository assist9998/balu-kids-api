# Balu Kids — backend

FastAPI + SQLAlchemy + Postgres. Клиент: Ольга, детский центр на Бали. Фронтенд — отдельный репо `balu-kids-prototype`.

## 🚨 Postgres = источник правды, Sheets = только зеркало

Мигрировано с "читать/писать напрямую в Google Sheets" на Postgres. Правило без исключений: **никогда не проектировать новые фичи поверх прямого чтения/записи в Sheets.** Google уже один раз забанил похожую таблицу у другого проекта (Gorizont, 17.07) — данные были потеряны. Sheets сейчас существует только как читаемое зеркало для Ольги.

Паттерн в `sheets_client.py`: сначала пробуем `pg_dual_write.py` (Postgres), при ошибке — fallback на старый Sheets-код (временная подстраховка на время миграции, не новая точка входа для фич).

## Файлы

- `main.py` — все FastAPI-роуты.
- `models.py` — SQLAlchemy-модели.
- `sheets_client.py` — обёртка над и Postgres (через `pg_dual_write.py`), и legacy Sheets fallback.
- `database.py` — сессии/движок.

## Роли и авторизация

`_STAFF_POSITION_ROLE` в `main.py`: `director`, `accounter/accountant → staff`, `manager`, всё остальное → `teacher`. Токены — in-memory `_SESSIONS` (теряются при рестарте бэкенда). `_NON_TRACKED_POSITIONS = {director, developer}` — эти позиции не участвуют в посещаемости персонала вообще (нет записей, и не будет — не баг).

## Проверка перед коммитом

Тесты через pytest (`test_*.py` в корне) — гонять перед коммитом, если менялась логика sweep/attendance.

## Деплой

Coolify, автодеплой по push **сломан** для этого репо тоже — деплоить вручную через `docker exec coolify php artisan tinker` на сервере (см. CLAUDE.md фронтенда — тот же паттерн, другой UUID приложения).

**Перед деплоем бэкенда — обязательно спросить подтверждение** (это отдельно от общего правила "спросить перед push" — бэкенд-деплой рискованнее, может задеть живые данные).

## Прод

- Бэкенд: `https://rmxois1uv0a24xrbi2dfcfac.95.179.188.141.sslip.io`
- Сервер: `root@95.179.188.141`
