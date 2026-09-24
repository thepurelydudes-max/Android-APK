from __future__ import annotations

import asyncio
import json
import re
import traceback

import flet as ft

from license_gate import LicenseGate

from paths import RUNTIME_DIR, ensure_initial_data, resolve_media_path

# Android bundles are read-only. Seed the writable database/cache before db.py
# captures its paths at import time.
ensure_initial_data()
import db
import engine
import updater
from adaptive_descriptions import contextual_item_explanation


P = {
    "bg": "#07131F",
    "panel": "#0B1D2A",
    "panel_alt": "#102936",
    "panel_hover": "#143645",
    "gold": "#C89B3C",
    "gold_bright": "#F0D58A",
    "cyan": "#0AC8B9",
    "cyan_soft": "#69E4DA",
    "text": "#F0E6D2",
    "muted": "#9AA7B2",
    "danger": "#E26D75",
    "success": "#64C89C",
    "border": "#274556",
}

ROLE_KEYS = ["EXP", "Лес", "Мид", "Голд", "Роум"]
ROLE_LABELS = {
    "ru": {"EXP": "EXP", "Лес": "Лес", "Мид": "Мид", "Голд": "Голд", "Роум": "Роум"},
    "en": {"EXP": "EXP", "Лес": "Jungle", "Мид": "Mid", "Голд": "Gold", "Роум": "Roam"},
}
ROLE_ICONS = {
    "EXP": "roles/exp.png",
    "Лес": "roles/jungle.png",
    "Мид": "roles/mid.png",
    "Голд": "roles/gold.png",
    "Роум": "roles/roam.png",
}

TEXT = {
    "ru": {
        "title": "Mobile Legends Counter Assistant",
        "subtitle": "Помощник по драфту и адаптации билда",
        "your_role": "Моя роль",
        "enemy": "Враг",
        "enemy_team": "Состав соперника",
        "clear": "Очистить",
        "picks": "Рейтинг пиков",
        "build": "Адаптация билда",
        "pick_hint": "Укажи хотя бы одного героя соперника.",
        "no_results": "Для выбранной роли рекомендации не найдены.",
        "duplicate": "Этот герой уже выбран в составе соперника.",
        "winrate": "Win rate",
        "counters": "Контрит",
        "danger": "Опасны",
        "items": "Предметы",
        "reasons": "Зачем этот предмет",
        "stats": "Характеристики",
        "effect": "Эффект",
        "update": "Обновить данные",
        "updating": "Обновляю локальную базу из интернет-источников…",
        "updated": "Данные обновлены.",
        "update_error": "Ошибка обновления",
        "patch": "Патч",
        "last_update": "База",
        "lang": "EN",
        "selected": "Мой герой",
        "score": "Оценка",
        "recommended_items": "Рекомендуемые предметы",
        "build_description": "Описание сборки",
        "offline": "Основная работа офлайн; интернет нужен для проверки лицензии не реже одного раза в 30 дней и для обновления базы.",
    },
    "en": {
        "title": "Mobile Legends Counter Assistant",
        "subtitle": "Draft and adaptive build assistant",
        "your_role": "My role",
        "enemy": "Enemy",
        "enemy_team": "Enemy team",
        "clear": "Clear",
        "picks": "Pick rating",
        "build": "Build adaptation",
        "pick_hint": "Select at least one enemy hero.",
        "no_results": "No recommendations for this role.",
        "duplicate": "This hero is already selected in the enemy team.",
        "winrate": "Win rate",
        "counters": "Counters",
        "danger": "Threats",
        "items": "Items",
        "reasons": "Why this item",
        "stats": "Stats",
        "effect": "Effect",
        "update": "Update data",
        "updating": "Updating the local database from internet sources…",
        "updated": "Data updated.",
        "update_error": "Update error",
        "patch": "Patch",
        "last_update": "Database",
        "lang": "RU",
        "selected": "My hero",
        "score": "Score",
        "recommended_items": "Recommended items",
        "build_description": "Build description",
        "offline": "Normal use is offline; internet is required for a license check at least once every 30 days and for database updates.",
    },
}


def _safe_json_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value]
    if not value:
        return []
    try:
        data = json.loads(value)
        return [str(x) for x in data] if isinstance(data, list) else []
    except Exception:
        return []


class MobileAssistant:
    """Portrait Android UI which mirrors the desktop application's information density."""

    def __init__(self, page: ft.Page, initial_lang: str = "ru"):
        self.page = page
        self.lang = "en" if str(initial_lang).lower() == "en" else "ru"
        self.role = "Лес"
        self.enemy_ids: list[str | None] = [None] * 5
        self.selected_pick_id = ""
        self.pick_results: list[dict] = []
        self.pick_builds: dict[str, dict] = {}
        self.current_build: dict | None = None
        self.snapshot: dict = {}

        self.enemy_dropdowns: list[ft.Dropdown] = []
        self.enemy_portraits: list[ft.Container] = []
        self.enemy_clear_buttons: list[ft.IconButton] = []
        self.role_buttons: dict[str, ft.Container] = {}
        self.role_labels: dict[str, ft.Text] = {}

        # References to long-lived controls. Role/language changes mutate these
        # controls in place instead of clearing and rebuilding the whole page.
        self.header_subtitle_text: ft.Text | None = None
        self.header_meta_text: ft.Text | None = None
        self.lang_button: ft.OutlinedButton | None = None
        self.your_role_text: ft.Text | None = None
        self.enemy_team_text: ft.Text | None = None
        self.clear_all_button: ft.TextButton | None = None
        self.pick_title_text: ft.Text | None = None
        self.build_title_text: ft.Text | None = None
        self.footer_offline_text: ft.Text | None = None

        self.status_text = ft.Text("")
        self.pick_column = ft.Column(spacing=7)
        self.build_column = ft.Column(spacing=10)
        self.update_button: ft.FilledButton | None = None
        self.update_progress: ft.ProgressBar | None = None
        self._update_stage = 0

        self.reload_snapshot()
        self.configure_page()
        self.rebuild_page()

    def t(self, key: str) -> str:
        return TEXT[self.lang].get(key, key)

    def configure_page(self) -> None:
        self.page.title = "Mobile Legends Counter Assistant"
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.theme = ft.Theme(color_scheme_seed=P["gold"])
        self.page.bgcolor = P["bg"]
        self.page.padding = 0
        self.page.scroll = ft.ScrollMode.AUTO

    def reload_snapshot(self) -> None:
        db.init_db()
        self.snapshot = db.load_runtime_snapshot()

    def champ_name(self, champ: dict | None) -> str:
        if not champ:
            return ""
        if self.lang == "ru":
            return champ.get("name_ru") or champ.get("name") or champ.get("id") or ""
        return champ.get("name") or champ.get("id") or ""

    def champion_display_name(self, raw: str) -> str:
        """Localise an engine enemy name/id exactly as the desktop description generator does."""
        value = str(raw or "")
        champ = db.resolve_snapshot_champion(self.snapshot, value)
        return self.champ_name(champ) if champ else value

    def item_name(self, canonical: str) -> str:
        row = self.snapshot.get("items", {}).get(canonical) or {}
        if self.lang == "ru":
            return row.get("name_ru") or canonical
        return canonical

    def champ_by_id(self, cid: str | None) -> dict | None:
        return self.snapshot.get("champions_by_id", {}).get(str(cid or ""))

    def item_record(self, canonical: str) -> dict:
        return self.snapshot.get("items", {}).get(canonical) or db.get_item(canonical) or {}

    def image_src(self, record: dict | None, fallback: str = "mobilelegends_icon.png") -> str:
        if not record:
            return fallback
        value = str(record.get("icon_path") or "")
        if value:
            p = resolve_media_path(value)
            if p.is_file():
                return str(p)
        url = str(record.get("icon_url") or "")
        return url or fallback

    def avatar_content(self, champ: dict | None, size: int = 48) -> ft.Control:
        """Desktop-like avatar content: '?' while empty, portrait after selection.

        If a selected champion has no cached/remote portrait, use initials just like
        the Windows version rather than repeating the Mobile Legends app logo.
        """
        if not champ:
            return ft.Row(
                alignment=ft.MainAxisAlignment.CENTER,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text(
                        "?",
                        size=max(18, int(size * 0.38)),
                        weight=ft.FontWeight.BOLD,
                        color=P["gold_bright"],
                        text_align=ft.TextAlign.CENTER,
                    )
                ],
            )
        src = self.image_src(champ, fallback="")
        if src:
            return ft.Image(src=src, fit=ft.BoxFit.COVER)
        initials = (self.champ_name(champ)[:2] or "?").upper()
        return ft.Row(
            alignment=ft.MainAxisAlignment.CENTER,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(
                    initials,
                    size=max(12, int(size * 0.28)),
                    weight=ft.FontWeight.BOLD,
                    color=P["gold_bright"],
                    text_align=ft.TextAlign.CENTER,
                )
            ],
        )

    def avatar_box(self, champ: dict | None, size: int = 48) -> ft.Container:
        return ft.Container(
            width=size,
            height=size,
            border_radius=8,
            border=ft.Border.all(1, P["border"]),
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            bgcolor=P["panel_alt"],
            content=self.avatar_content(champ, size),
        )

    def build_item_names(self, build: dict | None, limit: int = 6) -> list[str]:
        """Return the same six-slot build strip concept used by the desktop UI.

        The engine order is authoritative. If an incomplete source only returns
        fewer than six ordered entries, fill remaining slots exclusively from the
        same champion's finished-item pool; never invent unrelated items.
        """
        if not build:
            return []
        champ = build.get("champion") or {}
        candidates: list[str] = []
        for source in (build.get("ordered") or [], build.get("situational") or [], build.get("base") or []):
            for name in source:
                if name and name not in candidates:
                    candidates.append(name)
        cid = str(champ.get("id") or "")
        for row in self.snapshot.get("item_pools", {}).get(cid, []):
            name = str(row.get("item_name") or "")
            item = self.snapshot.get("items", {}).get(name) or {}
            if str(item.get("tier") or "").casefold() != "upgraded":
                continue
            if name and name not in candidates:
                candidates.append(name)

        # Some public build-trend rows contain only three core items. The desktop
        # layout is six-slot, so complete an undersized source with conservative
        # full-tier staples that match this champion's archetype. The adaptive
        # engine's ordered items always stay first and therefore keep priority.
        arch = engine.archetype(champ)
        if len(candidates) < limit:
            if "tank" in arch or "support" in arch:
                fallback = [
                    "Dominance Ice", "Athena's Shield", "Antique Cuirass",
                    "Immortality", "Radiant Armor", "Blade Armor", "Oracle",
                    "Thunder Belt", "Guardian Helmet", "Cursed Helmet",
                    "Flask of the Oasis", "Fleeting Time",
                ]
            elif "marksman" in arch:
                fallback = [
                    "Demon Hunter Sword", "Golden Staff", "Corrosion Scythe",
                    "Wind of Nature", "Malefic Roar", "Berserker's Fury",
                    "Blade of Despair", "Windtalker", "Haas' Claws",
                    "Rose Gold Meteor", "Immortality",
                ]
            elif "mage" in arch or "magic" in arch:
                fallback = [
                    "Holy Crystal", "Divine Glaive", "Genius Wand",
                    "Glowing Wand", "Winter Crown", "Blood Wings",
                    "Wishing Lantern", "Lightning Truncheon",
                    "Concentrated Energy", "Starlium Scythe",
                    "Feather of Heaven", "Immortality",
                ]
            elif "assassin" in arch:
                fallback = [
                    "Blade of the Heptaseas", "Hunter Strike", "Blade of Despair",
                    "Malefic Roar", "Rose Gold Meteor", "Endless Battle",
                    "War Axe", "Immortality",
                ]
            else:
                fallback = [
                    "War Axe", "Queen's Wings", "Brute Force Breastplate",
                    "Hunter Strike", "Blade of Despair", "Rose Gold Meteor",
                    "Malefic Roar", "Endless Battle", "Immortality",
                ]
            for name in fallback:
                item = self.snapshot.get("items", {}).get(name) or {}
                if str(item.get("tier") or "").casefold() != "upgraded":
                    continue
                if name not in candidates:
                    candidates.append(name)
                if len(candidates) >= limit + 4:
                    break

        result: list[str] = []
        has_boots = False
        for name in candidates:
            item = self.snapshot.get("items", {}).get(name) or {}
            if str(item.get("tier") or "").casefold() != "upgraded":
                continue
            is_boots = engine.is_boot_item(name, str(item.get("category") or ""))
            if is_boots and has_boots:
                continue
            if is_boots:
                has_boots = True
            result.append(name)
            if len(result) >= limit:
                break
        return result

    def header_meta_value(self) -> str:
        patch = db.get_meta("patch_version", "")
        last = db.get_meta("last_update", "")
        info_parts = []
        if patch:
            info_parts.append(f"{self.t('patch')}: {patch}")
        if last:
            info_parts.append(f"{self.t('last_update')}: {last}")
        return " • ".join(info_parts) if info_parts else self.t("offline")

    def header(self) -> ft.Control:
        self.header_subtitle_text = ft.Text(self.t("subtitle"), size=11, color=P["muted"])
        self.header_meta_text = ft.Text(self.header_meta_value(), size=10, color="#758997")
        self.lang_button = ft.OutlinedButton(content=self.t("lang"), icon=ft.Icons.LANGUAGE, on_click=self.toggle_language)

        brand = ft.Column(
            spacing=2,
            expand=True,
            controls=[
                ft.Image(src="mobilelegends_logo.png", width=178, height=48, fit=ft.BoxFit.CONTAIN),
                self.header_subtitle_text,
            ],
        )
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=14, vertical=12),
            bgcolor=P["panel"],
            border=ft.Border.all(1, P["border"]),
            content=ft.Column(
                spacing=5,
                controls=[
                    ft.Row(
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Image(src="mobilelegends_icon.png", width=42, height=42, fit=ft.BoxFit.CONTAIN),
                            brand,
                            self.lang_button,
                        ],
                    ),
                    self.header_meta_text,
                ],
            ),
        )

    def role_control(self) -> ft.Control:
        self.role_buttons = {}
        self.role_labels = {}
        controls: list[ft.Control] = []
        for role in ROLE_KEYS:
            selected = role == self.role
            label = ft.Text(
                ROLE_LABELS[self.lang][role],
                size=9,
                weight=ft.FontWeight.BOLD,
                color=P["gold_bright"] if selected else P["muted"],
                text_align=ft.TextAlign.CENTER,
            )
            box = ft.Container(
                data=role,
                expand=True,
                padding=6,
                border=ft.Border.all(2 if selected else 1, P["gold_bright"] if selected else P["border"]),
                border_radius=10,
                bgcolor=P["panel_hover"] if selected else P["panel_alt"],
                on_click=lambda e, r=role: self.on_role_click(r),
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=2,
                    controls=[
                        ft.Image(src=ROLE_ICONS[role], width=32, height=32, fit=ft.BoxFit.CONTAIN),
                        label,
                    ],
                ),
            )
            self.role_buttons[role] = box
            self.role_labels[role] = label
            controls.append(box)
        return ft.Row(spacing=5, controls=controls)

    def champion_options(self) -> list[ft.DropdownOption]:
        """Blue desktop-like enemy list: white names plus a tiny hero portrait.

        The portrait exists only in the expanded menu. The closed dropdown keeps
        the ordinary text value, while the full-size portrait remains in the
        separate avatar frame on the left.
        """
        champions = sorted(self.snapshot.get("champions", []), key=lambda c: self.champ_name(c).casefold())
        options: list[ft.DropdownOption] = []
        for champ in champions:
            name = self.champ_name(champ)
            option_avatar = self.avatar_box(champ, 28)
            options.append(
                ft.DropdownOption(
                    key=str(champ["id"]),
                    text=name,
                    leading_icon=option_avatar,
                    style=ft.ButtonStyle(
                        color={
                            ft.ControlState.DEFAULT: "#FFFFFF",
                            ft.ControlState.HOVERED: "#FFFFFF",
                            ft.ControlState.FOCUSED: "#FFFFFF",
                            ft.ControlState.SELECTED: "#FFFFFF",
                        },
                        bgcolor={
                            ft.ControlState.DEFAULT: P["panel_alt"],
                            ft.ControlState.HOVERED: P["panel_hover"],
                            ft.ControlState.FOCUSED: P["panel_hover"],
                            ft.ControlState.SELECTED: "#123D4C",
                        },
                        shape=ft.RoundedRectangleBorder(radius=8),
                        padding=ft.Padding.symmetric(horizontal=8, vertical=6),
                    ),
                )
            )
        return options

    def portrait(self, champ: dict | None, size: int = 48) -> ft.Container:
        return self.avatar_box(champ, size)

    def enemy_control(self, index: int) -> ft.Control:
        champ = self.champ_by_id(self.enemy_ids[index])
        portrait_box = self.avatar_box(champ, 48)
        self.enemy_portraits.append(portrait_box)
        dd = ft.Dropdown(
            key=f"enemy_{index}",
            label=f"{self.t('enemy')} {index + 1}",
            value=self.enemy_ids[index],
            editable=True,
            enable_filter=True,
            enable_search=True,
            expand=True,
            options=self.champion_options(),
            # Closed field: only the hero name. Expanded menu: blue surface with
            # white names and the per-option mini portrait defined above.
            filled=True,
            fill_color=P["panel_alt"],
            bgcolor={
                ft.ControlState.DEFAULT: P["panel_alt"],
                ft.ControlState.FOCUSED: P["panel_alt"],
                ft.ControlState.HOVERED: P["panel_alt"],
            },
            color="#FFFFFF",
            text_style=ft.TextStyle(color="#FFFFFF", size=13),
            label_style=ft.TextStyle(color=P["cyan_soft"], size=11),
            hint_style=ft.TextStyle(color=P["muted"]),
            hover_color=P["panel_hover"],
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            border={
                ft.ControlState.DEFAULT: ft.OutlineInputBorder(
                    border_radius=10, side=ft.BorderSide(width=1, color=P["border"])
                ),
                ft.ControlState.FOCUSED: ft.OutlineInputBorder(
                    border_radius=10, side=ft.BorderSide(width=2, color=P["cyan"])
                ),
            },
            menu_style=ft.MenuStyle(
                bgcolor=P["panel_alt"],
                side=ft.BorderSide(width=1, color=P["border"]),
                shape=ft.RoundedRectangleBorder(radius=12),
                padding=4,
            ),
            on_select=lambda e, i=index: self.on_enemy_select(i, e),
        )
        self.enemy_dropdowns.append(dd)
        clear_button = ft.IconButton(
            icon=ft.Icons.CLOSE,
            tooltip=self.t("clear"),
            on_click=lambda e, i=index: self.clear_enemy(i),
        )
        self.enemy_clear_buttons.append(clear_button)
        return ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                portrait_box,
                dd,
                clear_button,
            ],
        )

    def selection_panel(self) -> ft.Control:
        self.enemy_dropdowns = []
        self.enemy_portraits = []
        self.enemy_clear_buttons = []
        self.your_role_text = ft.Text(self.t("your_role"), size=13, weight=ft.FontWeight.BOLD, color=P["gold_bright"])
        self.enemy_team_text = ft.Text(self.t("enemy_team"), size=14, weight=ft.FontWeight.BOLD)
        self.clear_all_button = ft.TextButton(content=self.t("clear"), icon=ft.Icons.CLEAR_ALL, on_click=self.clear_all)
        return ft.Container(
            bgcolor=P["panel"],
            border=ft.Border.all(1, P["border"]),
            border_radius=12,
            padding=12,
            content=ft.Column(
                spacing=10,
                controls=[
                    self.your_role_text,
                    self.role_control(),
                    ft.Divider(height=1, color=P["border"]),
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            self.enemy_team_text,
                            self.clear_all_button,
                        ],
                    ),
                    *[self.enemy_control(i) for i in range(5)],
                ],
            ),
        )

    def output_panel(self) -> ft.Control:
        self.pick_column = ft.Column(spacing=7)
        self.build_column = ft.Column(spacing=10)
        self.pick_title_text = ft.Text(self.t("picks"), size=16, weight=ft.FontWeight.BOLD, color=P["gold_bright"])
        self.build_title_text = ft.Text(self.t("build"), size=16, weight=ft.FontWeight.BOLD, color=P["gold_bright"])
        self.render_outputs()
        return ft.Column(
            spacing=12,
            controls=[
                ft.Container(
                    bgcolor=P["panel"],
                    border=ft.Border.all(1, P["border"]),
                    border_radius=12,
                    padding=12,
                    content=ft.Column(
                        spacing=9,
                        controls=[
                            self.pick_title_text,
                            self.pick_column,
                        ],
                    ),
                ),
                ft.Container(
                    bgcolor=P["panel"],
                    border=ft.Border.all(1, P["border"]),
                    border_radius=12,
                    padding=12,
                    content=ft.Column(
                        spacing=9,
                        controls=[
                            self.build_title_text,
                            self.build_column,
                        ],
                    ),
                ),
            ],
        )

    def footer(self) -> ft.Control:
        self.update_button = ft.FilledButton(content=self.t("update"), icon=ft.Icons.REFRESH, on_click=self.update_data)
        self.footer_offline_text = ft.Text(self.t("offline"), size=10, color="#758997")
        self.update_progress = ft.ProgressBar(
            value=0,
            height=6,
            color=P["cyan"],
            bgcolor=P["panel_alt"],
            visible=False,
        )
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            content=ft.Column(
                spacing=8,
                controls=[
                    ft.Row(controls=[self.update_button]),
                    self.update_progress,
                    self.status_text,
                    self.footer_offline_text,
                ],
            ),
        )

    def rebuild_page(self) -> None:
        self.page.clean()
        self.status_text = ft.Text("")
        content = ft.Column(
            spacing=12,
            controls=[
                self.header(),
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=10),
                    content=ft.Column(spacing=12, controls=[self.selection_panel(), self.output_panel()]),
                ),
                self.footer(),
            ],
        )
        self.page.add(ft.SafeArea(content=content))
        self.page.update()

    def selected_enemies(self) -> list[tuple[str, str]]:
        rows = []
        for cid in self.enemy_ids:
            champ = self.champ_by_id(cid)
            if champ:
                rows.append((champ.get("name") or champ.get("id"), ""))
        return rows

    def refresh_role_controls(self) -> None:
        """Update only the five role buttons; never rebuild the page."""
        for role, box in self.role_buttons.items():
            selected = role == self.role
            box.border = ft.Border.all(
                2 if selected else 1,
                P["gold_bright"] if selected else P["border"],
            )
            box.bgcolor = P["panel_hover"] if selected else P["panel_alt"]
            label = self.role_labels.get(role)
            if label is not None:
                label.value = ROLE_LABELS[self.lang][role]
                label.color = P["gold_bright"] if selected else P["muted"]

    def on_role_click(self, role: str) -> None:
        if role == self.role:
            return
        self.role = role
        # The calculation is fast; the old delay/flicker came from page.clean()
        # and rebuilding five 134-entry dropdowns. Update only role styling and
        # the two result columns, then send one UI diff to Flutter.
        self.refresh_role_controls()
        self.recalculate(preserve_selection=False, update_page=False)
        self.page.update()

    def on_enemy_select(self, index: int, e) -> None:
        cid = str(e.control.value or "") or None
        if cid and any(cid == other for i, other in enumerate(self.enemy_ids) if i != index):
            e.control.value = self.enemy_ids[index]
            self.status_text.value = self.t("duplicate")
            self.status_text.color = P["danger"]
            self.page.update()
            return
        self.enemy_ids[index] = cid
        if index < len(self.enemy_portraits):
            self.enemy_portraits[index].content = self.avatar_content(self.champ_by_id(cid), 48)
        self.status_text.value = ""
        self.recalculate(preserve_selection=False)

    def clear_enemy(self, index: int) -> None:
        self.enemy_ids[index] = None
        if index < len(self.enemy_dropdowns):
            self.enemy_dropdowns[index].value = None
        if index < len(self.enemy_portraits):
            self.enemy_portraits[index].content = self.avatar_content(None, 48)
        self.recalculate(preserve_selection=False)

    def clear_all(self, _e=None) -> None:
        self.enemy_ids = [None] * 5
        for dd in self.enemy_dropdowns:
            dd.value = None
        for box in self.enemy_portraits:
            box.content = self.avatar_content(None, 48)
        self.selected_pick_id = ""
        self.pick_results = []
        self.pick_builds = {}
        self.current_build = None
        self.render_outputs()
        self.page.update()

    def recalculate(self, preserve_selection: bool = True, update_page: bool = True) -> None:
        enemies = self.selected_enemies()
        if not enemies:
            self.pick_results = []
            self.pick_builds = {}
            self.current_build = None
            self.selected_pick_id = ""
            self.render_outputs()
            if update_page:
                self.page.update()
            return

        previous = self.selected_pick_id if preserve_selection else ""
        self.pick_results = engine.recommend_picks(self.role, enemies, 10, snapshot=self.snapshot)
        ids = [str(r.get("champion", {}).get("id") or "") for r in self.pick_results]
        self.selected_pick_id = previous if previous in ids else (ids[0] if ids else "")

        # Desktop UI previews the adaptive items for every candidate in the rating.
        self.pick_builds = {}
        for result in self.pick_results:
            champ = result.get("champion") or {}
            cid = str(champ.get("id") or "")
            try:
                self.pick_builds[cid] = engine.recommend_build(
                    champ.get("name") or champ.get("id"), enemies, snapshot=self.snapshot
                )
            except Exception:
                self.pick_builds[cid] = {"champion": champ, "ordered": [], "base": [], "situational": [], "reasons": {}}

        self.current_build = self.pick_builds.get(self.selected_pick_id)
        if self.current_build is None:
            self.refresh_build()
        self.render_outputs()
        if update_page:
            self.page.update()

    def refresh_build(self) -> None:
        if not self.selected_pick_id:
            self.current_build = None
            return
        cached = self.pick_builds.get(self.selected_pick_id)
        if cached is not None:
            self.current_build = cached
            return
        champ = self.champ_by_id(self.selected_pick_id)
        if not champ:
            self.current_build = None
            return
        try:
            self.current_build = engine.recommend_build(
                champ.get("name") or champ.get("id"), self.selected_enemies(), snapshot=self.snapshot
            )
            self.pick_builds[self.selected_pick_id] = self.current_build
        except Exception:
            self.current_build = None

    def select_pick(self, cid: str) -> None:
        self.selected_pick_id = str(cid)
        self.refresh_build()
        self.render_outputs()
        self.page.update()

    def localized_enemy_records(self, names: list[str]) -> list[dict]:
        out = []
        for name in names:
            champ = db.resolve_snapshot_champion(self.snapshot, name)
            if champ:
                out.append(champ)
        return out

    def matchup_line(self, title: str, names: list[str], danger: bool = False) -> ft.Control:
        records = self.localized_enemy_records(names)[:5]
        chips: list[ft.Control] = []
        if not records:
            chips.append(ft.Text("—", size=9, color=P["muted"]))
        else:
            for champ in records:
                chips.append(
                    ft.Container(
                        padding=ft.Padding.symmetric(horizontal=5, vertical=2),
                        border_radius=6,
                        bgcolor=P["bg"],
                        content=ft.Row(
                            spacing=3,
                            tight=True,
                            controls=[
                                ft.Container(
                                    width=18,
                                    height=18,
                                    border_radius=4,
                                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                                    content=ft.Image(src=self.image_src(champ), fit=ft.BoxFit.COVER),
                                ),
                                ft.Text(self.champ_name(champ), size=9),
                            ],
                        ),
                    )
                )
        return ft.Column(
            spacing=3,
            controls=[
                ft.Text(title + ":", size=9, weight=ft.FontWeight.BOLD, color=P["danger"] if danger else P["muted"]),
                ft.Row(spacing=4, run_spacing=3, wrap=True, controls=chips),
            ],
        )

    def build_preview(self, build: dict | None) -> ft.Control:
        names = self.build_item_names(build)
        icons: list[ft.Control] = []
        for name in names[:6]:
            item = self.item_record(name)
            icons.append(
                ft.Container(
                    width=29,
                    height=29,
                    border_radius=5,
                    border=ft.Border.all(1, P["border"]),
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    content=ft.Image(src=self.image_src(item), fit=ft.BoxFit.COVER),
                    tooltip=self.item_name(name),
                )
            )
        while len(icons) < 6:
            icons.append(ft.Container(width=29, height=29, border_radius=5, border=ft.Border.all(1, P["border"])))
        return ft.Row(spacing=4, controls=icons)

    def pick_card(self, result: dict, rank: int) -> ft.Control:
        champ = result.get("champion") or {}
        cid = str(champ.get("id") or "")
        selected = cid == self.selected_pick_id
        wr = result.get("win_rate")
        wr_text = "—" if wr is None else f"{float(wr):.1f}%"
        build = self.pick_builds.get(cid)

        identity = ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(
                    width=46,
                    height=46,
                    border_radius=8,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    content=ft.Image(src=self.image_src(champ), fit=ft.BoxFit.COVER),
                ),
                ft.Column(
                    spacing=1,
                    expand=True,
                    controls=[
                        ft.Text(f"{rank}. {self.champ_name(champ)}", size=13, weight=ft.FontWeight.BOLD, color=P["gold_bright"]),
                        ft.Text(
                            f"{self.t('score')} {float(result.get('score') or 0):.2f} · {self.t('winrate')} {wr_text}",
                            size=9,
                            color=P["muted"],
                        ),
                    ],
                ),
                ft.Icon(ft.Icons.CHECK_CIRCLE if selected else ft.Icons.CHEVRON_RIGHT, color=P["gold_bright"] if selected else P["muted"]),
            ],
        )

        return ft.Container(
            data=cid,
            padding=9,
            border=ft.Border.all(2, P["gold_bright"] if selected else P["border"]),
            border_radius=10,
            bgcolor=P["panel_alt"],
            on_click=lambda e, pick_id=cid: self.select_pick(pick_id),
            content=ft.Column(
                spacing=6,
                controls=[
                    identity,
                    self.matchup_line(self.t("counters"), result.get("positive") or []),
                    self.matchup_line(self.t("danger"), result.get("negative") or [], danger=True),
                    ft.Row(
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Text(self.t("items") + ":", size=9, weight=ft.FontWeight.BOLD, color=P["muted"]),
                            self.build_preview(build),
                        ],
                    ),
                ],
            ),
        )

    def item_card(self, canonical: str, build: dict) -> ft.Control:
        row = self.item_record(canonical)
        return ft.Container(
            expand=True,
            padding=6,
            border=ft.Border.all(1, P["border"]),
            border_radius=9,
            bgcolor=P["panel_alt"],
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=4,
                controls=[
                    ft.Container(
                        width=52,
                        height=52,
                        border_radius=7,
                        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                        content=ft.Image(src=self.image_src(row), fit=ft.BoxFit.COVER),
                    ),
                    ft.Text(self.item_name(canonical), size=9, weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.CENTER),
                ],
            ),
        )

    def item_detail(self, canonical: str, build: dict, number: int) -> ft.Control:
        """Render the same adaptive human-readable build explanation as the desktop app."""
        row = self.item_record(canonical) or {"name": canonical}
        why, benefits = contextual_item_explanation(
            row, build, self.lang, self.champion_display_name
        )

        def sentence(value: str) -> str:
            value = " ".join(str(value or "").split()).strip().rstrip(" .;:")
            if not value:
                return ""
            return value[:1].upper() + value[1:] + "."

        explanation_parts = [sentence(why)] + [sentence(value) for value in benefits]
        explanation = " ".join(part for part in explanation_parts if part) or "—"

        return ft.Container(
            padding=ft.Padding.only(bottom=8),
            content=ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[
                    ft.Container(
                        width=38,
                        height=38,
                        border_radius=6,
                        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                        content=ft.Image(src=self.image_src(row), fit=ft.BoxFit.COVER),
                    ),
                    ft.Column(
                        spacing=3,
                        expand=True,
                        controls=[
                            ft.Text(
                                f"{number}. {self.item_name(canonical)}",
                                size=11,
                                weight=ft.FontWeight.BOLD,
                                color=P["gold_bright"],
                            ),
                            ft.Text(explanation, size=10, color=P["text"]),
                        ],
                    ),
                ],
            ),
        )

    def render_outputs(self) -> None:
        if not self.pick_results:
            self.pick_column.controls = [
                ft.Text(self.t("pick_hint") if not self.selected_enemies() else self.t("no_results"), color=P["muted"])
            ]
        else:
            self.pick_column.controls = [self.pick_card(r, i + 1) for i, r in enumerate(self.pick_results)]

        if not self.current_build:
            self.build_column.controls = [ft.Text("—", color=P["muted"])]
            return

        champ = self.current_build.get("champion") or {}
        items = self.build_item_names(self.current_build)
        item_rows: list[ft.Control] = []
        for start in range(0, min(6, len(items)), 3):
            cards = [self.item_card(name, self.current_build) for name in items[start:start + 3]]
            while len(cards) < 3:
                cards.append(ft.Container(expand=True))
            item_rows.append(ft.Row(spacing=7, controls=cards))

        details = [self.item_detail(name, self.current_build, i + 1) for i, name in enumerate(items[:6])]
        self.build_column.controls = [
            ft.Container(
                padding=8,
                border=ft.Border.all(1, P["border"]),
                border_radius=10,
                bgcolor=P["panel_alt"],
                content=ft.Row(
                    spacing=10,
                    controls=[
                        ft.Container(
                            width=58,
                            height=58,
                            border_radius=9,
                            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                            content=ft.Image(src=self.image_src(champ), fit=ft.BoxFit.COVER),
                        ),
                        ft.Column(
                            spacing=1,
                            expand=True,
                            controls=[
                                ft.Text(self.t("selected"), size=10, color=P["muted"]),
                                ft.Text(self.champ_name(champ), size=17, weight=ft.FontWeight.BOLD, color=P["gold_bright"]),
                            ],
                        ),
                    ],
                ),
            ),
            ft.Text(self.t("recommended_items"), size=12, weight=ft.FontWeight.BOLD),
            *item_rows,
            ft.Divider(height=1, color=P["border"]),
            ft.Text(self.t("build_description"), size=12, weight=ft.FontWeight.BOLD),
            *details,
        ]

    def refresh_language_controls(self) -> None:
        """Translate existing controls in place without clearing the page."""
        if self.header_subtitle_text is not None:
            self.header_subtitle_text.value = self.t("subtitle")
        if self.header_meta_text is not None:
            self.header_meta_text.value = self.header_meta_value()
        if self.lang_button is not None:
            self.lang_button.content = self.t("lang")
        if self.your_role_text is not None:
            self.your_role_text.value = self.t("your_role")
        if self.enemy_team_text is not None:
            self.enemy_team_text.value = self.t("enemy_team")
        if self.clear_all_button is not None:
            self.clear_all_button.content = self.t("clear")
        if self.pick_title_text is not None:
            self.pick_title_text.value = self.t("picks")
        if self.build_title_text is not None:
            self.build_title_text.value = self.t("build")
        if self.update_button is not None:
            self.update_button.content = self.t("update")
        if self.footer_offline_text is not None:
            self.footer_offline_text.value = self.t("offline")

        self.refresh_role_controls()

        # Keep the already-created dropdown/menu controls. Only their text is
        # changed, so hundreds of portrait controls are not recreated.
        for index, dd in enumerate(self.enemy_dropdowns):
            dd.label = f"{self.t('enemy')} {index + 1}"
            for option in dd.options or []:
                champ = self.champ_by_id(str(option.key or ""))
                if champ:
                    option.text = self.champ_name(champ)
        for button in self.enemy_clear_buttons:
            button.tooltip = self.t("clear")

        # Pick/build cards contain translated labels and adaptive descriptions,
        # so only these two output columns are regenerated.
        self.render_outputs()

    def toggle_language(self, _e=None) -> None:
        self.lang = "en" if self.lang == "ru" else "ru"
        self.refresh_language_controls()
        self.page.update()

    def _apply_update_progress(self, message: str) -> None:
        """Translate updater text into a visible stage and progress bar value."""
        message = str(message or "").strip()
        if not message:
            return
        self.status_text.value = message
        self.status_text.color = P["cyan_soft"]

        stage_match = re.match(r"\s*([1-6])/6\b", message)
        if stage_match:
            self._update_stage = int(stage_match.group(1))
            if self.update_progress:
                # A stage message means that stage has just started.
                self.update_progress.value = max(0.0, min(1.0, (self._update_stage - 1) / 6.0))
            return

        # Stage 6 emits detailed image-cache counters. Reflect those as
        # fractional progress instead of leaving the bar apparently frozen.
        count_match = re.search(r"(\d+)\s*/\s*(\d+)", message)
        if count_match and self._update_stage >= 6 and self.update_progress:
            current = int(count_match.group(1))
            total = max(1, int(count_match.group(2)))
            fraction = max(0.0, min(1.0, current / total))
            is_items = ("предмет" in message.casefold()) or ("item" in message.casefold())
            if is_items:
                self.update_progress.value = (5.5 + 0.5 * fraction) / 6.0
            else:
                self.update_progress.value = (5.0 + 0.5 * fraction) / 6.0

    async def update_data(self, _e=None) -> None:
        if self.update_button:
            self.update_button.disabled = True
        self._update_stage = 0
        if self.update_progress:
            self.update_progress.visible = True
            self.update_progress.value = 0
        self.status_text.value = self.t("updating")
        self.status_text.color = P["gold_bright"]
        self.page.update()

        # updater.update_all() runs in a worker thread. Its progress callback is
        # bridged into the Flet event loop with an asyncio.Queue so UI updates
        # stay on the main async task rather than touching controls cross-thread.
        loop = asyncio.get_running_loop()
        progress_queue: asyncio.Queue[str] = asyncio.Queue()

        def report_progress(message: str) -> None:
            loop.call_soon_threadsafe(progress_queue.put_nowait, str(message))

        worker = asyncio.create_task(asyncio.to_thread(updater.update_all, report_progress, self.lang))
        try:
            while not worker.done() or not progress_queue.empty():
                try:
                    message = await asyncio.wait_for(progress_queue.get(), timeout=0.15)
                except asyncio.TimeoutError:
                    continue
                self._apply_update_progress(message)
                self.page.update()

            summary = await worker
            if self.update_progress:
                self.update_progress.value = 1.0
            self.reload_snapshot()
            valid_ids = set(self.snapshot.get("champions_by_id", {}))
            self.enemy_ids = [cid if cid in valid_ids else None for cid in self.enemy_ids]
            errors = list(summary.get("errors") or [])
            self.recalculate(preserve_selection=True, update_page=False)
            # The database can gain new heroes, so refresh menu options once,
            # but keep the existing page and controls mounted to avoid a flash.
            for index, dd in enumerate(self.enemy_dropdowns):
                dd.options = self.champion_options()
                dd.value = self.enemy_ids[index] if index < len(self.enemy_ids) else None
            for index, box in enumerate(self.enemy_portraits):
                cid = self.enemy_ids[index] if index < len(self.enemy_ids) else None
                box.content = self.avatar_content(self.champ_by_id(cid), 48)
            if self.header_meta_text is not None:
                self.header_meta_text.value = self.header_meta_value()

            msg = self.t("updated")
            if summary.get("patch"):
                msg += f" {self.t('patch')}: {summary['patch']}."
            if errors:
                msg += f" ({len(errors)} source warnings)"
            self.status_text.value = msg
            self.status_text.color = P["success"]
        except Exception as exc:
            self.status_text.value = f"{self.t('update_error')}: {exc}"
            self.status_text.color = P["danger"]
            try:
                log_dir = RUNTIME_DIR / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "android-update-error.log").write_text(traceback.format_exc(), encoding="utf-8")
            except Exception:
                pass
        finally:
            if self.update_button:
                self.update_button.disabled = False
            if self.update_progress:
                self.update_progress.visible = False
            self.page.update()


async def main(page: ft.Page):
    gate = LicenseGate(
        page,
        product_id="mobilelegends",
        product_name="Mobile Legends Counter Assistant",
        logo_asset="mobilelegends_logo.png",
        app_factory=MobileAssistant,
    )
    await gate.start()


if __name__ == "__main__":
    ft.run(main, assets_dir="assets")
