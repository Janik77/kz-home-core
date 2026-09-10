# KZ Home Core v0.3

KZ Home Core — компактное ядро умного дома на FastAPI, Pydantic и SQLAlchemy 2.
PostgreSQL хранит структуру дома, устройства, сцены и автоматизации; EventBus,
WebSocket, transport abstraction и virtual demo mode сохранены.

## Архитектура

```text
HTTP / WebSocket → FastAPI API → Services → Repositories → SQLAlchemy → PostgreSQL
                                  ↓
                              EventBus → Automation / WebSocket
                                  ↓
                         Virtual / MQTT transports
```

API не обращается к SQLAlchemy напрямую. Routes вызывают services, services —
repositories. Состояние, capabilities, metadata и правила хранятся в PostgreSQL
JSONB. Для локальных тестов используется отдельная SQLite database.

## Установка и конфигурация

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Экспортируйте настройки из `.env` удобным для вашей оболочки способом. Обязателен
`DATABASE_URL`; реальные пароли нельзя коммитить. Поддерживаются также `APP_ENV`
и `APP_DEBUG`.

```bash
export DATABASE_URL='postgresql+psycopg://kzhome:password@localhost:5432/kzhome'
export APP_ENV=development
export APP_DEBUG=false
```

## Миграции, demo data и запуск

Таблицы при запуске приложения автоматически не создаются. Сначала примените
миграции и заполните демонстрационные данные:

```bash
alembic upgrade head
python -m app.seed
python -m app.seed  # безопасный повторный запуск, дубликатов не будет
uvicorn app.main:app --reload
```

Swagger UI: http://127.0.0.1:8000/docs

Новая миграция после изменения ORM-моделей:

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

Virtual simulator последовательно изменяет `hall_motion`,
`bedroom_temperature` и `main_leak_sensor`. Интервал задаётся через
`KZHOME_SIMULATOR_INTERVAL` (по умолчанию 5 секунд). События доступны на `/ws`.

## API

Сохранены `/health`, `/devices`, `/devices/{id}/state`, shortcuts `/on` и `/off`,
`/scenes`, `/scenes/{id}/run` и `/ws`. Для houses, floors, rooms, devices,
scenes и automations доступен CRUD. `GET /devices` принимает фильтры `house_id`,
`room_id`, `type` и `online`.

## Тесты

Тесты явно используют отдельную SQLite database и не подключаются к production:

```bash
pytest
```
