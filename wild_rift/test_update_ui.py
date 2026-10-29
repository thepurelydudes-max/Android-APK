"""Regression coverage for the update UI without requiring a Flet runtime."""
import ast
import asyncio
import tempfile
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def load_update_method(runtime_dir, updater):
    source = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    cls = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "MobileAssistant")
    method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "update_data")
    namespace = {
        "asyncio": asyncio, "traceback": traceback, "RUNTIME_DIR": runtime_dir,
        "updater": updater, "P": {"gold_bright": "gold", "success": "green", "danger": "red"},
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), namespace)
    return namespace["update_data"]


class UpdateUITest(unittest.TestCase):
    def run_update(self, already_current):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            summary = {"already_current": already_current, "package_version": "test", "patch": "7.3c", "errors": []}
            updater = SimpleNamespace(update_all=lambda *args: summary)
            update_data = load_update_method(runtime, updater)
            calls = []
            page = SimpleNamespace(update=lambda: None)
            ui = SimpleNamespace(
                lang="ru", page=page, update_button=None, update_progress=None,
                status_text=SimpleNamespace(value="", color=""),
                reload_snapshot=lambda: calls.append("reload"),
                recalculate=lambda **kwargs: calls.append("recalculate"),
                snapshot={"champions_by_id": {}}, enemy_ids=[None] * 5,
                enemy_dropdowns=[], enemy_portraits=[], header_meta_text=None,
                t=lambda key: key,
                _apply_update_progress=lambda value: None,
            )
            async def run_inline(func, *args):
                return func(*args)
            with patch.object(asyncio, "to_thread", run_inline):
                asyncio.run(update_data(ui))
            audit = (runtime / "logs" / "update-audit.log").read_text(encoding="utf-8")
            return calls, ui.status_text.value, audit

    def test_current_package_keeps_snapshot_and_logs_status(self):
        calls, status, audit = self.run_update(True)
        self.assertEqual(calls, [])
        self.assertIn("База уже актуальна", status)
        self.assertIn("Already current: True", audit)

    def test_installed_package_refreshes_snapshot(self):
        calls, status, audit = self.run_update(False)
        self.assertEqual(calls, ["reload", "recalculate"])
        self.assertIn("updated", status)
        self.assertIn("Already current: False", audit)


if __name__ == "__main__":
    unittest.main()
