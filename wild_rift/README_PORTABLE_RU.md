# Wild Rift Counter Assistant 3.7.11 — Windows Portable FREE

Эта ПК-версия является desktop-оболочкой над **той же логикой**, что и Android APK 3.7.11.

## Что одинаково с APK

- единый `DraftMatrixEngine`;
- итоговый рейтинг: **60% matchup + 20% coverage + 15% Tier + 5% win rate**;
- матрица WildRiftCore `-3..+3`;
- повышенный вес вероятного соперника по линии;
- только чемпионы выбранной роли;
- автоматическое определение ролей противников;
- WildRiftCore role builds и build variants;
- адаптация сборки по всему вражескому драфту;
- situational items и точные adaptations by opponent;
- ручное обновление базы и кэша;
- RU/EN интерфейс и адаптивные объяснения предметов.

## Отличие от Android

Интерфейс сделан под широкое окно Windows: слева драфт, по центру рейтинг пиков, справа адаптивная сборка. Данные, формулы и билдер предметов общие с APK.

## Portable-режим

Программа хранит всё рядом с `WildRiftCounterAssistant.exe`:

- `data/wildrift.db`
- `cache/`
- `logs/`

Python пользователю не нужен.

## Запуск из исходников

Нужен Windows 10/11 x64 и Python 3.10+.

Запустите:

```bat
run_pc.bat
```

## Локальная сборка portable EXE

Запустите:

```bat
build_pc.bat
```

Готовый ZIP появится в папке `release`.

## GitHub Actions

Workflow: **Build Windows Portable**.

Он прогоняет те же regression tests, собирает one-file EXE через `flet pack`, формирует portable-папку и публикует ZIP как artifact.
