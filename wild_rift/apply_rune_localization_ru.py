from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

# Official Russian rune names follow Riot's Russian Wild Rift terminology
# (current naming verified against Riot patch notes through 7.3).
# Descriptions below are deliberately concise player-facing explanations,
# not verbatim patch-note text, so they remain readable in the app.
RUNE_RU = {
    "Absolute Focus": ("Полная сосредоточенность", "Пока у тебя больше 70% здоровья, повышает адаптивную силу."),
    "Aery": ("Пушинка", "При нанесении урона Пушинка атакует врага, а при помощи союзнику дает ему щит."),
    "Arcane Comet": ("Магическая комета", "Попадание умением по чемпиону вызывает комету в его точку; успешные попадания усиливают последующие кометы."),
    "Axiom Arcanist": ("Мастер Аксиом", "Усиливает урон абсолютного умения, а также лечение и щиты от него; участие в убийстве сокращает оставшуюся перезарядку абсолютного умения."),
    "Battle Zeal": ("Боевое рвение", "В бою с вражескими чемпионами постепенно увеличивает урон базовых умений."),
    "Bone Plating": ("Костяная пластина", "После получения урона от чемпиона уменьшает урон нескольких следующих атак или умений."),
    "Botanist": ("Ботаник", "Уничтожение растений дает золото и усиливает их полезные эффекты."),
    "Brutal": ("Жестокость", "Автоатаки наносят чемпионам дополнительный адаптивный урон."),
    "Celerity": ("Быстрота", "Увеличивает дополнительную скорость передвижения и дает постоянную небольшую прибавку к скорости."),
    "Chain Assault": ("Серия атак", "Попадание активным умением помечает чемпиона; следующие две атаки или активных умения наносят дополнительный адаптивный урон."),
    "Cheap Shot": ("Грязный прием", "Наносит дополнительный чистый урон врагам, находящимся под эффектами контроля."),
    "Conqueror": ("Завоеватель", "Нанося урон, накапливаешь заряды адаптивной силы; на максимуме зарядов усиливается восстановление здоровья от нанесенного урона."),
    "Coup de Grace": ("Удар милосердия", "Повышает урон по чемпионам с низким запасом здоровья."),
    "Courage of the Colossus": ("Мужество колосса", "Обездвиживание вражеского чемпиона дает тебе щит."),
    "Cut Down": ("Реванш", "Повышает урон по чемпионам с высоким запасом текущего здоровья."),
    "Dark Harvest": ("Темная жатва", "Попадание по чемпиону с низким здоровьем наносит дополнительный адаптивный урон и собирает душу, навсегда усиливая руну."),
    "Demolish": ("Снос", "После зарядки рядом с башней следующая атака наносит ей большой дополнительный урон."),
    "Electrocute": ("Казнь электричеством", "Три отдельных попадания атакой или умением по одному чемпиону за короткое время наносят дополнительный адаптивный урон."),
    "Empowered Attack": ("Усиленная атака", "Периодически усиливает следующую автоатаку по чемпиону дополнительным адаптивным уроном."),
    "Empowerment": ("Усиление", "Три последовательные атаки по чемпиону наносят дополнительный адаптивный урон и временно увеличивают получаемый им урон."),
    "Eyeball Collection": ("Коллекция глаз", "Участие в убийствах чемпионов и эпических монстров накапливает адаптивную силу."),
    "First Strike": ("Удар на опережение", "Если первым начать бой с чемпионом, на несколько секунд увеличивает наносимый чистый урон и дает золото за этот урон."),
    "Fleet Footwork": ("Искусное лавирование", "Передвижение и атаки накапливают энергию; при полном заряде следующая атака лечит и помогает быстрее двигаться."),
    "Font of Life": ("Живой источник", "Попадание по вражескому чемпиону лечит тебя и союзника рядом с наименьшим запасом здоровья."),
    "Gathering Storm": ("Надвигающаяся буря", "По мере длительности матча периодически дает все больше адаптивной силы."),
    "Grasp of Undying": ("Хватка нежити", "После некоторого времени в бою следующая атака по чемпиону усиливается, лечит и навсегда увеличивает максимальный запас здоровья."),
    "Guardian": ("Страж", "Защищает ближайшего союзника или союзника, на которого ты применяешь умение; при получении серьезного урона вы оба получаете щит."),
    "Hextech Flashtraption": ("Хекс-скачок", "Пока Скачок на перезарядке, позволяет после короткой подготовки совершить короткую телепортацию."),
    "Hubris": ("Гордыня", "Участие в убийстве вскоре после нанесения урона дает временную адаптивную силу; бонус растет с убийствами."),
    "Ice Overlord": ("Владыка льда", "После обездвиживания врага создает замедляющую ледяную область; также временно усиливает защиту и наносит магический урон вокруг тебя."),
    "Ixtali Seedjar": ("Банка для семян", "Уничтоженные растения оставляют семена, которые можно подобрать и посадить заново."),
    "Last Stand": ("Последний рубеж", "Увеличивает наносимый урон, когда у тебя мало здоровья."),
    "Legend Alacrity": ("Легенда: Рвение", "Добивания и убийства постепенно увеличивают скорость атаки."),
    "Legend Bloodline": ("Легенда: Родословная", "Добивания и убийства постепенно увеличивают всестороннее вытягивание жизни."),
    "Legend: Haste": ("Легенда: Ускорение", "Добивания и убийства монстров и миньонов дают заряды ускорения умений."),
    "Lethal Tempo": ("Смертельный темп", "Атаки по чемпионам накапливают скорость атаки; на максимуме зарядов дополнительно усиливают автоатаки."),
    "Manaflow Band": ("Поток маны", "Попадания умениями по чемпионам постепенно увеличивают максимальный запас маны и улучшают ее восстановление."),
    "Nimbus Cloak": ("Сияющий плащ", "После применения заклинания призывателя временно увеличивает скорость передвижения."),
    "Nullifying Orb": ("Сфера уничтожения", "При падении здоровья до опасного уровня дает защитный щит."),
    "Overgrowth": ("Разрастание", "Гибель миньонов и монстров поблизости навсегда увеличивает максимальный запас здоровья."),
    "Perseverance": ("Настойчивость", "Когда на тебя действует контроль, временно повышает броню и сопротивление магии."),
    "Phase Rush": ("Фазовый рывок", "Три отдельных попадания по чемпиону за короткое время дают скорость передвижения и помогают чаще применять базовые умения."),
    "Relentless Hunter": ("Беспощадный охотник", "Увеличивает скорость передвижения вне боя; участие в убийствах усиливает бонус."),
    "Revitalize": ("Оживление", "Усиливает лечение и щиты, особенно для целей с низким запасом здоровья."),
    "Scorch": ("Ожог", "После попадания умением по чемпиону через короткое время наносит дополнительный магический урон."),
    "Second Wind": ("Второе дыхание", "После получения урона от чемпиона восстанавливает часть недостающего здоровья; особенно полезно против постоянного изнуряющего урона."),
    "Sudden Impact": ("Внезапный удар", "После рывка, прыжка, телепортации или выхода из невидимости следующая атака по чемпиону наносит дополнительный чистый урон."),
    "Transcendence": ("Превосходство", "Дает ускорение умений и помогает чаще применять базовые способности."),
    "Triumph": ("Триумф", "Участие в убийстве восстанавливает часть недостающего здоровья и ресурса и ненадолго ускоряет передвижение."),
    "Tyrant": ("Тиран", "При попадании по чемпиону с уровнем здоровья ниже 50% наносит дополнительный адаптивный урон."),
    "Unshakeable": ("Незыблемость", "Повышает броню и сопротивление магии; бонус становится сильнее рядом с несколькими вражескими чемпионами."),
    "Zombie Ward": ("Тотем-зомби", "Уничтожение вражеского тотема создает союзный Тотем-зомби и дает накапливаемую адаптивную силу."),
}

ALIASES = {
    "Axiom arcanist": "Axiom Arcanist",
}


def _rewrite_json_names(raw: str) -> str:
    try:
        values = json.loads(raw or "[]")
    except Exception:
        return raw
    changed = False
    out = []
    for value in values:
        current = ALIASES.get(str(value), str(value))
        out.append(current)
        changed = changed or current != str(value)
    if not changed:
        return raw
    return json.dumps(out, ensure_ascii=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    args = ap.parse_args()

    db_path = Path(args.db)
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        columns = {str(r[1]) for r in con.execute("PRAGMA table_info(runes)")}
        if "effect_ru" not in columns:
            con.execute("ALTER TABLE runes ADD COLUMN effect_ru TEXT NOT NULL DEFAULT ''")

        # Normalize historical spelling before localization.
        for table in ("role_runes", "role_rune_variants"):
            rows = con.execute(
                f"SELECT rowid,runes_json FROM {table}"
            ).fetchall()
            for row in rows:
                new_json = _rewrite_json_names(str(row["runes_json"] or "[]"))
                if new_json != str(row["runes_json"] or "[]"):
                    con.execute(
                        f"UPDATE {table} SET runes_json=?,updated_at=CURRENT_TIMESTAMP WHERE rowid=?",
                        (new_json, row["rowid"]),
                    )

        if "role_rune_adaptations" in {
            str(r[0]) for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }:
            for old, new in ALIASES.items():
                con.execute(
                    "UPDATE role_rune_adaptations SET from_rune=? WHERE from_rune=?",
                    (new, old),
                )
                con.execute(
                    "UPDATE role_rune_adaptations SET to_rune=? WHERE to_rune=?",
                    (new, old),
                )

        # Copy any useful media row from an alias before removing it.
        for old, new in ALIASES.items():
            old_row = con.execute("SELECT * FROM runes WHERE name=?", (old,)).fetchone()
            new_row = con.execute("SELECT * FROM runes WHERE name=?", (new,)).fetchone()
            if old_row and new_row:
                con.execute(
                    """UPDATE runes SET
                       icon_url=CASE WHEN icon_url='' THEN ? ELSE icon_url END,
                       icon_path=CASE WHEN icon_path='' THEN ? ELSE icon_path END,
                       category=CASE WHEN category='' THEN ? ELSE category END
                       WHERE name=?""",
                    (
                        str(old_row["icon_url"] or ""),
                        str(old_row["icon_path"] or ""),
                        str(old_row["category"] or ""),
                        new,
                    ),
                )
                con.execute("DELETE FROM runes WHERE name=?", (old,))

        # Remove an accidental scraper/navigation row; it is not a rune.
        con.execute("DELETE FROM runes WHERE name LIKE 'Detail page%'")

        for name, (name_ru, effect_ru) in RUNE_RU.items():
            row = con.execute("SELECT name FROM runes WHERE name=?", (name,)).fetchone()
            if row is None:
                raise SystemExit(f"Rune missing from catalog: {name}")
            con.execute(
                """UPDATE runes SET name_ru=?,effect_ru=?,updated_at=CURRENT_TIMESTAMP
                   WHERE name=?""",
                (name_ru, effect_ru, name),
            )

        used = set()
        priority = {
            "wrpocket.app:tencent-cn": 0,
            "wildriftfire.com": 1,
            "wildriftcore.com:indexed-7.3a": 2,
        }
        selected = {}
        for row in con.execute(
            "SELECT champion_id,role,runes_json,source FROM role_runes"
        ):
            source = str(row["source"])
            if source not in priority:
                continue
            key = (str(row["champion_id"]), str(row["role"]))
            current = selected.get(key)
            if current is None or priority[source] < priority[current["source"]]:
                selected[key] = dict(row)
        for row in selected.values():
            used.update(json.loads(row["runes_json"] or "[]"))

        missing = []
        for name in sorted(used):
            row = con.execute(
                "SELECT name_ru,effect_ru FROM runes WHERE name=?", (name,)
            ).fetchone()
            if not row or not str(row["name_ru"] or "").strip() or not str(row["effect_ru"] or "").strip():
                missing.append(name)
        if missing:
            raise SystemExit("Missing RU rune localization: " + ", ".join(missing))

        con.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('rune_locale_ru','official_names+concise_effects')"
        )
        con.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('rune_locale_ru_patch','7.3a')"
        )

        quick = con.execute("PRAGMA quick_check").fetchone()[0]
        if str(quick).casefold() != "ok":
            raise SystemExit(f"SQLite quick_check failed: {quick}")
        con.commit()

    print(json.dumps({
        "localized_catalog_entries": len(RUNE_RU),
        "selected_role_pairs": len(selected),
        "used_runes": len(used),
        "missing": missing,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
