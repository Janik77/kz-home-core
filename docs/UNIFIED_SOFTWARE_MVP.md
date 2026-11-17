# KZ Home: программная интеграция автоматизаций

Точный acceptance candidate: Core tooling `617c1efb96a3e6422ac3abefd7440ef5382fed20`
(draft #49 поверх #47), runtime `f6a0826929c87b39dec7e81d22280f1218bf18f1`,
Web `1e18ec20afc444b07d14e409fd733ba2ed9f0f95` (#27), schema `0013_device_automations`.
Документальные follow-up commits не заменяют runtime pin.

Одна актуальная инструкция: [SOFTWARE_UPGRADE.md](SOFTWARE_UPGRADE.md).
Она начинается с проверки существующих свободных checkout, не требует повторного
клонирования и создаёт отдельный новый persistent профиль. Старый проблемный профиль
не используется. `start` работает foreground; штатный stop — Ctrl-C в его терминале,
затем снова start с тем же state. Supervisor/автозапуск не проверены и не включены.

Ordinary restore строго exact-version, только в новый профиль. Межверсионный upgrade
поддерживает только указанный в инструкции backup #42/Web20/schema0010, а не0011/0012
или произвольную старую установку. Не менять служебные привязки вручную.

Проверенные на Core617c CI: Core regression, PostgreSQL migrations/concurrency,
MosquittoTLS, persistent lifecycle и узкий backup/upgrade/restore. Совместная browser
приёмка с automation SIGKILL прошла: run `44115292304`, включая unknown и отсутствие
replay после реального SIGKILL. Test/docs follow-up добавляет отдельные backup/restore
assertions для automation history; их результат должен быть подтверждён новым CI. ПК владельца и физические ESP32/реле
не испытаны этой интеграцией. Main/production не объединены/не изменены.

Исполняемый scope и ошибки: [AUTOMATIONS_SOFTWARE_SLICE.md](AUTOMATIONS_SOFTWARE_SLICE.md).
Исторические инструкции #43/Web25/schema0011 не являются инструкцией текущей версии.
