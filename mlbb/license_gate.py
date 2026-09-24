from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import flet as ft
import requests

try:
    import flet_secure_storage as fss
except Exception:  # Fail closed at runtime if the extension was not packaged.
    fss = None


API_BASE_URL = "https://counter-assistant-license.thepurelydudes.workers.dev"
OFFLINE_GRACE_DAYS = 30
OFFLINE_GRACE_SECONDS = OFFLINE_GRACE_DAYS * 24 * 60 * 60
HTTP_TIMEOUT_SECONDS = 7

COLORS = {
    "bg": "#07131F",
    "panel": "#0B1D2A",
    "panel_alt": "#102936",
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

TEXT = {
    "ru": {
        "page_suffix": "Лицензия",
        "lang": "EN",
        "checking": "Проверка лицензии…",
        "storage_error": (
            "Защищённое хранилище лицензии недоступно. "
            "Переустановите официальную сборку приложения."
        ),
        "activation_title": "Активация лицензии",
        "license_key": "Лицензионный ключ",
        "activate": "Активировать",
        "intro": "Введите лицензионный ключ, чтобы открыть полный доступ к программе.",
        "note": (
            "Лицензия бессрочная и поддерживает до 5 устройств. "
            "После активации приложение может работать без связи с сервером до 30 дней."
        ),
        "enter_key": "Введите лицензионный ключ.",
        "activated": "Лицензия активирована.",
        "network": (
            "Не удалось связаться с сервером лицензий. Проверьте подключение к интернету. "
            "Если используется VPN — попробуйте отключить его; если VPN выключен — "
            "попробуйте подключиться через него."
        ),
        "offline_expired": (
            "Для этой установки прошло более {days} дней с последней успешной онлайн-проверки "
            "лицензии. Подключитесь к интернету для подтверждения доступа.\n\n{network}"
        ),
        "invalid_license": "Неверный лицензионный ключ.",
        "device_limit": "Лимит активаций исчерпан: {limit} устройств.",
        "license_blocked": "Лицензия заблокирована.",
        "license_expired": "Срок действия лицензии истёк.",
        "device_not_activated": (
            "Эта установка больше не активирована. Подключитесь к интернету и "
            "активируйте лицензию снова."
        ),
        "invalid_request": (
            "Сервер не смог обработать запрос лицензии. Проверьте ключ и повторите попытку."
        ),
        "verify_failed": "Не удалось проверить лицензию. Повторите попытку позже.",
    },
    "en": {
        "page_suffix": "License",
        "lang": "RU",
        "checking": "Checking license…",
        "storage_error": (
            "Secure license storage is unavailable. Please reinstall the official app build."
        ),
        "activation_title": "License activation",
        "license_key": "License key",
        "activate": "Activate",
        "intro": "Enter your license key to unlock full access to the application.",
        "note": (
            "The license is perpetual and supports up to 5 devices. "
            "After activation, the app can work without contacting the server for up to 30 days."
        ),
        "enter_key": "Enter a license key.",
        "activated": "License activated.",
        "network": (
            "Unable to contact the license server. Check your internet connection. "
            "If you are using a VPN, try disabling it; if VPN is disabled, "
            "try connecting through one."
        ),
        "offline_expired": (
            "This installation has gone more than {days} days since the last successful online "
            "license check. Connect to the internet to verify access.\n\n{network}"
        ),
        "invalid_license": "Invalid license key.",
        "device_limit": "Activation limit reached: {limit} devices.",
        "license_blocked": "This license has been blocked.",
        "license_expired": "The license has expired.",
        "device_not_activated": (
            "This installation is no longer activated. Connect to the internet and "
            "activate the license again."
        ),
        "invalid_request": (
            "The license server could not process the request. Check the key and try again."
        ),
        "verify_failed": "Unable to verify the license. Please try again later.",
    },
}


@dataclass
class ApiResult:
    data: dict | None = None
    transient_error: bool = False


class _DesktopPreviewStorage:
    """Simple persistent storage used only by desktop UI preview.

    Android never uses this fallback: the packaged Android app must use
    Flet SecureStorage backed by Android Keystore. The fallback exists so the
    portrait Android UI can be tested from Windows/macOS/Linux without the
    secure-storage extension being available in that preview runtime.
    """

    def __init__(self, namespace: str) -> None:
        safe_name = namespace.replace(".", "_")
        self.path = (
            Path.home()
            / ".farliner_counter_assistant_preview"
            / f"{safe_name}.json"
        )

    def _read_all(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_all(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    async def get(self, key: str):
        return self._read_all().get(key)

    async def set(self, key: str, value: str) -> None:
        data = self._read_all()
        data[key] = str(value)
        self._write_all(data)

    async def remove(self, key: str) -> None:
        data = self._read_all()
        if key in data:
            data.pop(key, None)
            self._write_all(data)


class LicenseGate:
    """Blocks the application until a Cloudflare license is valid."""

    def __init__(
        self,
        page: ft.Page,
        *,
        product_id: str,
        product_name: str,
        logo_asset: str,
        app_factory: Callable[[ft.Page, str], object],
    ) -> None:
        self.page = page
        self.product_id = product_id.strip().lower()
        self.product_name = product_name
        self.logo_asset = logo_asset
        self.app_factory = app_factory
        self.namespace = f"farliner.counterassistant.{self.product_id}"

        self.lang = "ru"
        self.storage = None
        self.install_uuid = ""
        self.saved_license = ""

        self.current_view = ""
        self.activation_message_key = ""
        self.activation_message_args: dict = {}
        self.activation_prefill = ""
        self.activation_busy = False

        self.license_field: ft.TextField | None = None
        self.action_button: ft.FilledButton | None = None
        self.status_text: ft.Text | None = None
        self.progress: ft.ProgressRing | None = None
        self.lang_button: ft.OutlinedButton | None = None

    def _t(self, key: str, **kwargs) -> str:
        value = TEXT[self.lang].get(key, key)
        return value.format(**kwargs) if kwargs else value

    def _storage_key(self, name: str) -> str:
        return f"{self.namespace}.{name}"

    def _configure_gate_page(self) -> None:
        self.page.title = f"{self.product_name} — {self._t('page_suffix')}"
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.theme = ft.Theme(color_scheme_seed=COLORS["gold"])
        self.page.bgcolor = COLORS["bg"]
        self.page.padding = 0
        self.page.scroll = ft.ScrollMode.AUTO

    def _create_secure_storage(self):
        if fss is None:
            return None
        return fss.SecureStorage(
            android_options=fss.AndroidOptions(
                reset_on_error=True,
                migrate_on_algorithm_change=True,
                shared_preferences_name="farliner_counter_assistant_secure",
                preferences_key_prefix=self.namespace.replace(".", "_"),
            )
        )

    def _platform_name(self) -> str:
        platform = getattr(self.page, "platform", None)
        value = getattr(platform, "value", platform)
        return str(value or "").strip().lower()

    def _is_desktop_preview(self) -> bool:
        name = self._platform_name()
        return any(token in name for token in ("windows", "macos", "linux"))

    async def _storage_self_test(self) -> None:
        if self.storage is None:
            raise RuntimeError("Storage is not available")
        probe_key = self._storage_key("__probe__")
        probe_value = str(uuid.uuid4())
        await self.storage.set(probe_key, probe_value)
        read_back = await self.storage.get(probe_key)
        try:
            await self.storage.remove(probe_key)
        except Exception:
            pass
        if str(read_back or "") != probe_value:
            raise RuntimeError("Storage self-test failed")

    async def _initialize_storage(self) -> None:
        secure_storage = self._create_secure_storage()
        if secure_storage is not None:
            self.storage = secure_storage
            try:
                await self._storage_self_test()
                return
            except Exception:
                self.storage = None

        if self._is_desktop_preview():
            self.storage = _DesktopPreviewStorage(self.namespace)
            await self._storage_self_test()
            return

        raise RuntimeError("SecureStorage is not available")

    async def _safe_get(self, name: str) -> str:
        if self.storage is None:
            return ""
        value = await self.storage.get(self._storage_key(name))
        return str(value or "").strip()

    async def _safe_set(self, name: str, value: str) -> None:
        if self.storage is None:
            raise RuntimeError("SecureStorage is not available")
        await self.storage.set(self._storage_key(name), str(value))

    async def _safe_remove(self, name: str) -> None:
        if self.storage is None:
            return
        try:
            await self.storage.remove(self._storage_key(name))
        except Exception:
            pass

    async def _get_or_create_install_uuid(self) -> str:
        current = await self._safe_get("install_uuid")
        if current:
            return current
        current = str(uuid.uuid4())
        await self._safe_set("install_uuid", current)
        return current

    @staticmethod
    def _normalize_license(value: str) -> str:
        return "".join(str(value or "").strip().upper().split())

    async def _last_ok_timestamp(self) -> int:
        raw = await self._safe_get("last_online_ok")
        try:
            return max(0, int(float(raw)))
        except Exception:
            return 0

    async def _offline_grace_valid(self) -> bool:
        last_ok = await self._last_ok_timestamp()
        if not last_ok:
            return False
        now = int(time.time())
        if now + 300 < last_ok:
            return False
        return (now - last_ok) <= OFFLINE_GRACE_SECONDS

    async def _mark_online_ok(self, license_key: str) -> None:
        await self._safe_set("license_key", self._normalize_license(license_key))
        await self._safe_set("last_online_ok", str(int(time.time())))

    def _post_sync(self, path: str, license_key: str) -> ApiResult:
        payload = {
            "license_key": self._normalize_license(license_key),
            "device_id": self.install_uuid,
            "app_id": self.product_id,
        }
        try:
            response = requests.post(
                f"{API_BASE_URL}{path}",
                json=payload,
                timeout=HTTP_TIMEOUT_SECONDS,
                headers={"Accept": "application/json"},
            )
        except requests.RequestException:
            return ApiResult(transient_error=True)

        if response.status_code == 429 or response.status_code >= 500:
            return ApiResult(transient_error=True)

        try:
            data = response.json()
        except Exception:
            return ApiResult(transient_error=True)

        if not isinstance(data, dict):
            return ApiResult(transient_error=True)
        return ApiResult(data=data)

    async def _post(self, path: str, license_key: str) -> ApiResult:
        return await asyncio.to_thread(self._post_sync, path, license_key)

    async def start(self) -> None:
        self._configure_gate_page()

        try:
            await self._initialize_storage()
            stored_lang = (await self._safe_get("ui_lang")).lower()
            if stored_lang in {"ru", "en"}:
                self.lang = stored_lang
            self._configure_gate_page()
            self.install_uuid = await self._get_or_create_install_uuid()
            self.saved_license = self._normalize_license(await self._safe_get("license_key"))
        except Exception:
            self._show_fatal_storage_error()
            return

        if not self.saved_license:
            self._show_activation()
            return

        self._show_checking()
        result = await self._post("/check", self.saved_license)

        if result.transient_error:
            if await self._offline_grace_valid():
                self._open_application()
            else:
                self._show_activation("offline_expired", prefill=self.saved_license)
            return

        data = result.data or {}
        if data.get("ok") is True and data.get("licensed") is True:
            try:
                await self._mark_online_ok(self.saved_license)
            except Exception:
                self._show_fatal_storage_error()
                return
            self._open_application()
            return

        await self._safe_remove("last_online_ok")
        key, args = self._error_message_key(data.get("error"), data)
        self._show_activation(key, message_args=args, prefill=self.saved_license)

    def _open_application(self) -> None:
        self.page.clean()
        self.page.horizontal_alignment = ft.CrossAxisAlignment.START
        self.page.vertical_alignment = ft.MainAxisAlignment.START
        self.page.scroll = ft.ScrollMode.AUTO
        self.app_factory(self.page, self.lang)

    def _make_language_button(self, *, disabled: bool = False) -> ft.OutlinedButton:
        self.lang_button = ft.OutlinedButton(
            content=self._t("lang"),
            icon=ft.Icons.LANGUAGE,
            on_click=self.toggle_language,
            disabled=disabled,
        )
        return self.lang_button

    def _language_row(self, *, disabled: bool = False) -> ft.Row:
        return ft.Row(
            alignment=ft.MainAxisAlignment.END,
            controls=[self._make_language_button(disabled=disabled)],
        )

    def _show_checking(self) -> None:
        self.current_view = "checking"
        self._configure_gate_page()
        self.page.clean()
        self.status_text = ft.Text(
            self._t("checking"),
            color=COLORS["muted"],
            size=13,
            text_align=ft.TextAlign.CENTER,
        )
        self.page.add(
            ft.SafeArea(
                content=ft.Container(
                    padding=18,
                    content=ft.Column(
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=18,
                        controls=[
                            self._language_row(),
                            ft.Container(height=35),
                            ft.Image(src=self.logo_asset, height=74, fit=ft.BoxFit.CONTAIN),
                            ft.ProgressRing(width=34, height=34, stroke_width=3),
                            self.status_text,
                        ],
                    ),
                )
            )
        )
        self.page.update()

    def _show_fatal_storage_error(self) -> None:
        self.current_view = "fatal"
        self._configure_gate_page()
        self.page.clean()
        self.page.add(
            ft.SafeArea(
                content=ft.Container(
                    padding=18,
                    content=ft.Column(
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=14,
                        controls=[
                            self._language_row(),
                            ft.Container(height=20),
                            ft.Icon(ft.Icons.LOCK, size=46, color=COLORS["danger"]),
                            ft.Text(
                                self._t("storage_error"),
                                color=COLORS["text"],
                                size=14,
                                text_align=ft.TextAlign.CENTER,
                            ),
                        ],
                    ),
                )
            )
        )
        self.page.update()

    def _message(self, key: str, args: dict | None = None) -> str:
        if not key:
            return ""
        args = dict(args or {})
        if key == "offline_expired":
            args.setdefault("days", OFFLINE_GRACE_DAYS)
            args.setdefault("network", self._t("network"))
        return self._t(key, **args)

    def _show_activation(
        self,
        message_key: str = "",
        *,
        message_args: dict | None = None,
        prefill: str = "",
    ) -> None:
        self.current_view = "activation"
        self.activation_message_key = message_key
        self.activation_message_args = dict(message_args or {})
        self.activation_prefill = prefill
        self.activation_busy = False

        self.page.clean()
        self._configure_gate_page()

        self.license_field = ft.TextField(
            label=self._t("license_key"),
            hint_text="CA-XXXXX-XXXXX-XXXXX-XXXXX",
            value=prefill,
            max_length=26,
            border_color=COLORS["border"],
            focused_border_color=COLORS["gold"],
            color=COLORS["text"],
            on_submit=self.activate,
        )
        self.action_button = ft.FilledButton(
            content=self._t("activate"),
            icon=ft.Icons.LOCK_OPEN,
            on_click=self.activate,
        )
        message = self._message(message_key, self.activation_message_args)
        self.status_text = ft.Text(
            message,
            color=COLORS["danger"] if message else COLORS["muted"],
            size=12,
            text_align=ft.TextAlign.CENTER,
            selectable=True,
        )
        self.progress = ft.ProgressRing(
            width=24,
            height=24,
            stroke_width=2.5,
            visible=False,
        )

        card = ft.Container(
            bgcolor=COLORS["panel"],
            border=ft.Border.all(1, COLORS["border"]),
            border_radius=14,
            padding=20,
            content=ft.Column(
                spacing=14,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Image(src=self.logo_asset, height=72, fit=ft.BoxFit.CONTAIN),
                    ft.Text(
                        self._t("activation_title"),
                        size=20,
                        weight=ft.FontWeight.BOLD,
                        color=COLORS["gold_bright"],
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        self.product_name,
                        size=13,
                        color=COLORS["cyan_soft"],
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        self._t("intro"),
                        size=12,
                        color=COLORS["text"],
                        text_align=ft.TextAlign.CENTER,
                    ),
                    self.license_field,
                    ft.Row(
                        alignment=ft.MainAxisAlignment.CENTER,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=12,
                        controls=[self.action_button, self.progress],
                    ),
                    self.status_text,
                    ft.Divider(color=COLORS["border"], height=12),
                    ft.Text(
                        self._t("note"),
                        size=10,
                        color=COLORS["muted"],
                        text_align=ft.TextAlign.CENTER,
                    ),
                ],
            ),
        )

        self.page.add(
            ft.SafeArea(
                content=ft.Container(
                    padding=18,
                    content=ft.Column(
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            self._language_row(),
                            ft.Container(height=4),
                            card,
                        ],
                    ),
                )
            )
        )
        self.page.update()

    async def toggle_language(self, _e=None) -> None:
        if self.activation_busy:
            return
        self.lang = "en" if self.lang == "ru" else "ru"
        try:
            if self.storage is not None:
                await self._safe_set("ui_lang", self.lang)
        except Exception:
            pass

        if self.current_view == "activation":
            current_key = self.license_field.value if self.license_field else self.activation_prefill
            self._show_activation(
                self.activation_message_key,
                message_args=self.activation_message_args,
                prefill=str(current_key or ""),
            )
        elif self.current_view == "checking":
            self._show_checking()
        elif self.current_view == "fatal":
            self._show_fatal_storage_error()
        else:
            self._configure_gate_page()
            self.page.update()

    async def activate(self, _e=None) -> None:
        if not self.license_field or not self.action_button or not self.status_text:
            return

        key = self._normalize_license(self.license_field.value or "")
        if not key:
            self.activation_message_key = "enter_key"
            self.activation_message_args = {}
            self.status_text.value = self._t("enter_key")
            self.status_text.color = COLORS["danger"]
            self.page.update()
            return

        self.activation_busy = True
        self.license_field.value = key
        self.license_field.disabled = True
        self.action_button.disabled = True
        if self.lang_button:
            self.lang_button.disabled = True
        if self.progress:
            self.progress.visible = True
        self.status_text.value = self._t("checking")
        self.status_text.color = COLORS["muted"]
        self.page.update()

        result = await self._post("/activate", key)

        if result.transient_error:
            self._finish_activation_attempt("network")
            return

        data = result.data or {}
        if data.get("ok") is True and data.get("licensed") is True:
            try:
                await self._mark_online_ok(key)
                self.saved_license = key
            except Exception:
                self.activation_busy = False
                self._show_fatal_storage_error()
                return

            self.status_text.value = self._t("activated")
            self.status_text.color = COLORS["success"]
            self.page.update()
            await asyncio.sleep(0.25)
            self.activation_busy = False
            self._open_application()
            return

        error_key, error_args = self._error_message_key(data.get("error"), data)
        self._finish_activation_attempt(error_key, error_args)

    def _finish_activation_attempt(self, message_key: str, message_args: dict | None = None) -> None:
        self.activation_busy = False
        self.activation_message_key = message_key
        self.activation_message_args = dict(message_args or {})
        if self.license_field:
            self.license_field.disabled = False
        if self.action_button:
            self.action_button.disabled = False
        if self.lang_button:
            self.lang_button.disabled = False
        if self.progress:
            self.progress.visible = False
        if self.status_text:
            self.status_text.value = self._message(message_key, self.activation_message_args)
            self.status_text.color = COLORS["danger"]
        self.page.update()

    def _error_message_key(self, error: object, data: dict) -> tuple[str, dict]:
        code = str(error or "").strip()
        if code == "invalid_license":
            return "invalid_license", {}
        if code == "device_limit":
            return "device_limit", {"limit": data.get("max_devices") or 5}
        if code == "license_blocked":
            return "license_blocked", {}
        if code == "license_expired":
            return "license_expired", {}
        if code == "device_not_activated":
            return "device_not_activated", {}
        if code in {"invalid_request", "invalid_json"}:
            return "invalid_request", {}
        return "verify_failed", {}
