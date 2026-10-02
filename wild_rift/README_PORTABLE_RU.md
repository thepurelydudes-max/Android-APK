# Wild Rift Counter Assistant 3.9.2 — Windows Portable FREE

Это Windows-версия **WRCA 3.9.2**, перенесённая из той же кодовой базы, что и Android APK 3.9.2. Логика рекомендаций, role-specific matchup, глобальное уникальное распределение flex-чемпионов, адаптивные сборки и GitHub-пакетное обновление базы общие с APK.

## Что полностью перенесено из APK 3.9.2

- тот же `DraftMatrixEngine` и текущая логика ранжирования;
- до пяти противников, role-specific matchup и глобально уникальное распределение чемпионов между пятью ролями;
- WildRiftCore role builds, build variants, situational items и adaptations by opponent из текущей базы;
- адаптивные объяснения предметов;
- RU/EN интерфейс;
- текущая база WRCA Data Package и изображения чемпионов/предметов;
- новое обновление **одним проверенным ZIP с GitHub Releases** через `wrca_data/latest.json`;
- докачка прерванного файла, SHA-256 ZIP/manifest/DB/cache, ZIP CRC, SQLite `quick_check`;
- установка новой базы только после полной проверки, rollback при сбое финальной замены;
- отображение в шапке: патч, дата базы и версия пакета.

## Отличие от Android

Меняется только оболочка окна Windows: широкий рабочий стол с тремя колонками — драфт, контр-пики и адаптивная сборка. Расчётная логика и формат пакета обновления не отличаются от APK 3.9.2.

## Portable

Рядом с `WildRiftCounterAssistant.exe` находятся:

- `data/wildrift.db`
- `cache/champions`
- `cache/items`
- `logs`

Python для готовой portable-версии не нужен. Папка должна оставаться доступной для записи, потому что GitHub updater заменяет `data/` и `cache/` после проверки нового пакета.

## PC Builder

Для локальной сборки нужен Windows 10/11 x64 и Python 3.12 x64.

- `run_pc.bat` — запуск версии из исходников.
- `build_pc.bat` — тесты + сборка EXE + создание portable ZIP.
- готовый архив появляется в `release/`.

Версия: **3.9.2**.
