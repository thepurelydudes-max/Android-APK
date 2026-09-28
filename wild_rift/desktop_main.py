from __future__ import annotations

import os
import sys
from pathlib import Path


def _portable_root() -> Path:
    # PyInstaller/flet pack sets sys.frozen and sys.executable to the generated
    # EXE. Running from source keeps everything beside desktop_main.py.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PORTABLE_ROOT = _portable_root()

# Desktop portable mode intentionally stores the writable SQLite DB, cache and
# logs beside the executable, matching the old 3.4.4 portable distribution.
# The same folder also acts as the Flet assets root for logos/role icons.
os.environ["FLET_APP_STORAGE_DATA"] = str(PORTABLE_ROOT)
os.environ["FLET_ASSETS_DIR"] = str(PORTABLE_ROOT)

import flet as ft

from main import (
    MobileAssistant,
    P,
)


class DesktopAssistant(MobileAssistant):
    """Wide-screen Windows shell around the exact same WRCA 3.7.11 logic.

    The Android app and this desktop app share db.py, engine.py,
    draft_matrix_engine.py, updater.py and adaptive_descriptions.py. Only the
    page composition differs, so pick scores/build decisions remain identical.
    """

    def configure_page(self) -> None:
        self.page.title = "Wild Rift Counter Assistant"
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.theme = ft.Theme(color_scheme_seed=P["gold"])
        self.page.bgcolor = P["bg"]
        self.page.padding = 0
        self.page.scroll = ft.ScrollMode.HIDDEN

        # Flet 1.x desktop window settings. Keep the old portable application's
        # horizontal working style while still allowing the user to resize it.
        try:
            self.page.window.width = 1440
            self.page.window.height = 860
            self.page.window.min_width = 1180
            self.page.window.min_height = 700
            self.page.window.resizable = True
            self.page.window.maximizable = True
        except Exception:
            # The UI remains usable even if a future Flet client changes one of
            # the optional desktop-only window properties.
            pass

    def _pick_panel_desktop(self) -> ft.Control:
        self.pick_column = ft.Column(
            spacing=7,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        self.pick_title_text = ft.Text(
            self.t("picks"),
            size=17,
            weight=ft.FontWeight.BOLD,
            color=P["gold_bright"],
        )
        return ft.Container(
            expand=1,
            bgcolor=P["panel"],
            border=ft.Border.all(1, P["border"]),
            border_radius=12,
            padding=12,
            content=ft.Column(
                expand=True,
                spacing=9,
                controls=[
                    self.pick_title_text,
                    ft.Divider(height=1, color=P["border"]),
                    self.pick_column,
                ],
            ),
        )

    def _build_panel_desktop(self) -> ft.Control:
        self.build_column = ft.Column(
            spacing=10,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        self.build_title_text = ft.Text(
            self.t("build"),
            size=17,
            weight=ft.FontWeight.BOLD,
            color=P["gold_bright"],
        )
        return ft.Container(
            expand=1,
            bgcolor=P["panel"],
            border=ft.Border.all(1, P["border"]),
            border_radius=12,
            padding=12,
            content=ft.Column(
                expand=True,
                spacing=9,
                controls=[
                    self.build_title_text,
                    ft.Divider(height=1, color=P["border"]),
                    self.build_column,
                ],
            ),
        )

    def rebuild_page(self) -> None:
        """Desktop 3-column layout: draft -> rated picks -> adaptive build."""
        self.page.clean()
        self.status_text = ft.Text("")

        selection = ft.Container(
            width=405,
            content=self.selection_panel(),
        )
        picks = self._pick_panel_desktop()
        build = self._build_panel_desktop()

        # Both output columns must exist before rendering, because one pick card
        # already contains a six-item build preview.
        self.render_outputs()

        workspace = ft.Row(
            spacing=12,
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[selection, picks, build],
        )

        content = ft.Column(
            spacing=10,
            expand=True,
            controls=[
                self.header(),
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12),
                    expand=True,
                    content=workspace,
                ),
                self.footer(),
            ],
        )
        self.page.add(content)
        self.page.update()


async def main(page: ft.Page):
    DesktopAssistant(page)


if __name__ == "__main__":
    ft.run(main, assets_dir=str(PORTABLE_ROOT))
