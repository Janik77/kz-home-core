# Локальный Docker Compose

Запускайте команды из корня репозитория. Нужен запущенный Docker Desktop с Linux
containers и Docker Compose v2 или новее.

```powershell
docker compose -p kzhome-local -f compose.local.yaml up -d --build --wait
```

Самостоятельный стек содержит Core, PostgreSQL 17, Mosquitto 2 и одноразовый
сервис `migrate`. Сначала PostgreSQL проходит healthcheck, затем Alembic применяет
миграции только к локальной базе `kzhome_local`; Core стартует после успешной
миграции и готовности MQTT. Демоданные и пользователи автоматически не создаются.

| Сервис | Адрес на компьютере |
| --- | --- |
| API / Swagger | http://127.0.0.1:18000/docs |
| Проверка готовности БД и схемы | http://127.0.0.1:18000/ready |
| PostgreSQL | `127.0.0.1:15432`, БД/пользователь `kzhome_local`, пароль `local-only-password` |
| MQTT | `127.0.0.1:11883`, без TLS и аутентификации, только для локальной разработки |

Все опубликованные порты привязаны к `127.0.0.1`. Симулятор выключен.
Локальные настройки и учебные пароли заданы явно в Compose. Существующий `.env`
не подключается к контейнерам, значения `DATABASE_URL` и MQTT из него не
используются. Отдельный образ Core имеет тег `kz-home-core:dev-local`.

## Изоляция старых данных

Проект называется `kzhome-local`; он создаёт собственную сеть и тома:

- `kzhome-local_local_postgres_data`
- `kzhome-local_local_mosquitto_data`

Production Compose, `.env`, `deploy/local/`, сертификаты, старые БД и тома
`kzhome_postgres_data` / `kzhome_mosquitto_data` не используются и не изменяются.
Не объединяйте этот файл с production Compose и не задавайте ему имя проекта
старой установки через `-p` или `COMPOSE_PROJECT_NAME`.

## Проверка и остановка

```powershell
docker compose -p kzhome-local -f compose.local.yaml ps -a
docker compose -p kzhome-local -f compose.local.yaml logs --tail 100 core migrate mosquitto
Invoke-RestMethod http://127.0.0.1:18000/ready
docker compose -p kzhome-local -f compose.local.yaml stop
```

`migrate` со статусом `Exited (0)` — ожидаемый результат. Для повторного запуска
используйте команду `up` выше. Чтобы удалить локальные контейнеры и сеть с
сохранением локальных данных:

```powershell
docker compose -p kzhome-local -f compose.local.yaml down
```

Не добавляйте `-v`: этот флаг удаляет тома соответствующего проекта. Не используйте
`docker volume prune` или `docker system prune --volumes` для обслуживания стека.

При необходимости демоданные добавляются явно только в локальную базу:

```powershell
docker compose -p kzhome-local -f compose.local.yaml exec core python -m app.seed
```

Эта команда сама по себе не создаёт пользователя. Для работы с защищёнными API
используйте процедуру создания пользователя и выдачи прав из основного README.
