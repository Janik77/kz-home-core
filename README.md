# KZ Home Core v0.4

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

В development/test файл `.env` загружается автоматически. Уже установленные
переменные процесса имеют приоритет над значениями из файла. При явном
`APP_ENV=production` локальный `.env` не загружается. `DATABASE_URL` остаётся
обязательным, а реальные пароли нельзя коммитить.

```bash
export DATABASE_URL='postgresql+psycopg://kzhome:password@localhost:5432/kzhome'
export APP_ENV=development
export APP_DEBUG=false
export KZHOME_SIMULATOR_ENABLED=false
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

Virtual simulator по умолчанию отключён и никогда не запускается автоматически
при `APP_ENV=production`. Для локального demo mode включите его явно:

```bash
export KZHOME_SIMULATOR_ENABLED=true
export KZHOME_SIMULATOR_INTERVAL=5
uvicorn app.main:app --reload
```

Simulator работает фоновой задачей и последовательно изменяет `hall_motion`,
`bedroom_temperature` и `main_leak_sensor`. Каждый шаг открывает отдельную
короткоживущую database session. События доступны на `/ws`.

## API

Сохранены `/health`, `/devices`, `/devices/{id}/state`, shortcuts `/on` и `/off`,
`/scenes`, `/scenes/{id}/run` и `/ws`. Для houses, floors, rooms, devices,
scenes и automations доступен CRUD. `GET /devices` принимает фильтры `house_id`,
`room_id`, `type` и `online`.

Automation Engine принимает только декларативные JSON-правила. Он не выполняет
Python-код, shell-команды, `eval` или `exec`. Подробности: [ARCHITECTURE.md](ARCHITECTURE.md).

### Motion → light

```json
{
  "trigger": {"type": "device_state", "device_id": "hall_motion", "field": "motion", "operator": "eq", "value": true},
  "conditions": [],
  "actions": [{"type": "device_state", "device_id": "living_room_light", "state": {"on": true}}]
}
```

### Night motion → brightness 15%

```json
{
  "trigger": {"type": "device_state", "device_id": "hall_motion", "field": "motion", "operator": "eq", "value": true},
  "conditions": [{"type": "time", "after": "23:00", "before": "07:00"}],
  "actions": [{"type": "device_state", "device_id": "living_room_light", "state": {"on": true, "brightness": 15}}]
}
```

### Delay → light off

```json
{
  "trigger": {"type": "device_state", "device_id": "hall_motion", "field": "motion", "operator": "eq", "value": false},
  "conditions": [],
  "actions": [
    {"type": "delay", "seconds": 60},
    {"type": "device_state", "device_id": "living_room_light", "state": {"on": false}}
  ]
}
```

Состояние automation меняется через `/automations/{id}/enable` и `/disable`, а
`/automations/{id}/run` запускает правило вручную. История важных событий доступна
через `GET /events` с фильтрами `house_id`, `event_type` и `limit`.

## Тесты

Тесты явно используют отдельную SQLite database и не подключаются к production:

```bash
pip install -r requirements-dev.txt
pytest
ruff check app simulator tests
```
