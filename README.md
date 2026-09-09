# KZ Home Core

Минимальное серверное ядро умного дома KZ Home на FastAPI. Сейчас оно работает
полностью in-memory и не требует ни физического оборудования, ни MQTT-брокера.

## Возможности

- REST API для комнат, устройств и сцен;
- WebSocket-события об изменениях состояния;
- виртуальный датчик движения `motion1` (переключается каждые 5 секунд);
- автоматизация: при обнаружении движения включается `light1`;
- необязательный абстрактный MQTT-слой для будущего подключения ESP32.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Откройте документацию API в браузере:

http://127.0.0.1:8000/docs

Интервал виртуального датчика можно изменить переменной окружения, например:

```bash
KZHOME_SIMULATOR_INTERVAL=2 uvicorn app.main:app --reload
```

## Основные endpoints

- `GET /health`
- `GET /devices`, `GET /devices/{device_id}`
- `POST /devices/{device_id}/on`, `POST /devices/{device_id}/off`
- `GET /rooms`
- `GET /scenes`, `POST /scenes/{scene_id}/run`
- `WS /ws`

Пример WebSocket-события:

```json
{"type": "device_state_changed", "device_id": "light1", "state": "on"}
```

## MQTT

`app/mqtt.py` задаёт интерфейс транспорта и no-op реализацию. Поэтому MQTT не
нужен для запуска MVP. Будущие устройства смогут использовать темы:

- состояние: `kzhome/home1/livingroom/light1/state`;
- команда: `kzhome/home1/livingroom/light1/set`.

## Тесты

```bash
pytest
```
