# KZ Home Core v0.2

Простое серверное ядро умного дома на Python, FastAPI и Pydantic. Данные пока
хранятся в памяти, а demo mode позволяет запускать проект без оборудования и
MQTT-брокера.

## Архитектура

```text
App
 ↓
FastAPI
 ↓
KZ Home Core
 ↓
DeviceService
 ↓
Transport
 ↓
Virtual / MQTT / BLE Mesh / Zigbee / Matter
```

- `app/services/` содержит бизнес-логику устройств, сцен, структуры дома и
  автоматизаций;
- `app/events/` — внутренний in-memory EventBus;
- `app/transports/` — независимые от бизнес-логики интерфейсы транспортов;
- `simulator/` — детерминированные виртуальные датчики для demo mode;
- `app/demo_data.py` — один дом, два этажа, четыре комнаты, пять устройств, две
  сцены и одна автоматизация.

Поддержка MQTT является необязательной. Реальных BLE Mesh, Zigbee и Matter
интеграций пока нет: metadata устройства только сохраняет будущий протокол, а
demo-устройства используют `{"protocol": "virtual"}`.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Swagger UI доступен по адресу:

http://127.0.0.1:8000/docs

Проверка состояния ядра:

```bash
curl http://127.0.0.1:8000/health
```

## Virtual devices

После запуска simulator последовательно и без случайных скачков меняет движение
в коридоре, температуру в спальне и состояние датчика протечки. По умолчанию
один шаг выполняется каждые 5 секунд. Интервал можно изменить:

```bash
KZHOME_SIMULATOR_INTERVAL=2 uvicorn app.main:app --reload
```

При `hall_motion.motion = true` automation `hall_motion_light` включает
`living_room_light`. Состояние можно изменить вручную:

```bash
curl -X PATCH http://127.0.0.1:8000/devices/living_room_light/state \
  -H 'Content-Type: application/json' \
  -d '{"on": true, "brightness": 50}'
```

Старые shortcuts также работают:

```bash
curl -X POST http://127.0.0.1:8000/devices/living_room_light/on
curl -X POST http://127.0.0.1:8000/devices/living_room_light/off
```

WebSocket endpoint `/ws` отправляет события EventBus, в том числе
`device_state_changed`, `scene_started` и `automation_triggered`.

## MQTT abstraction

`MQTTTransport` не зависит от `paho-mqtt` и использует темы:

- `kzhome/{house_id}/{device_id}/state`;
- `kzhome/{house_id}/{device_id}/set`.

Без переданного MQTT-клиента используется no-op клиент, поэтому брокер для
запуска не нужен.

## Тесты

```bash
pytest
```
