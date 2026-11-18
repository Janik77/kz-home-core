# Numeric integration candidate

Runtime Core #51 `1ae52afa22431943064b5e85313f671b073b9458`,
Web #29 candidate `cd9592ac0d10f27e2112501e9886ed4bce3fa162`,
schema `0014_numeric_thresholds`; tooling `integration/numeric-web29`.

Use existing launcher, not a second stack implementation:
`python tools/run_software_stack.py --help` for disposable preview;
`python tools/durable_software.py --help` for persistent software profiles.
Source50 backup to NEW target transfer and original-profile rollback:
[SOFTWARE_UPGRADE.md](SOFTWARE_UPGRADE.md). Never edit existing profile bindings.
Acceptance pending until exact integration CI succeeds. Software emulation only.

---
## Historical Web28 integration

# KZ Home: единая программная версия с Web #28

Отдельная ветка `integration/web28-persistent` поверх Core #49.
Runtime `f6a0826929c87b39dec7e81d22280f1218bf18f1` не изменён.
Web `6f8f72f86f668b53f85c13054644c80427fe9eab` (#28), схема `0013_device_automations`.
Точный tooling SHA брать из отчёта/PR после прохождения всех четырёх workflows на нём.

Единственная инструкция: [SOFTWARE_UPGRADE.md](SOFTWARE_UPGRADE.md).
Используются проверенные свободные существующие checkout, без повторного clone.
Новый профиль сохраняется при stop/start, backup и exact-version restore.
Foreground запуск, не supervisor: штатная остановка Ctrl-C в его терминале.

Тесты включают настоящие PostgreSQL/MQTT TLS, программное реле, браузер,
ACK + state, неизвестный исход после SIGKILL без replay, сохранение automation
history/receipts и комнат/сцен/favorites при backup/restore. Считать их пройденными
только по зелёным CI на точном integration SHA; старые результаты #49 не заменяют новые.

Старый профиль не изменяется. Upgrade поддерживает только документированный
source42/schema0010, не произвольный профиль Web27/0011/0012. Ручное редактирование
служебных привязок запрещено. На ПК владельца версия не установлена этой работой.
Физическое оборудование не проверено; main/production не изменены.

Scope: [AUTOMATIONS_SOFTWARE_SLICE.md](AUTOMATIONS_SOFTWARE_SLICE.md).
