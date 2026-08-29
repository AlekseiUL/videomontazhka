from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import install_runtime  # noqa: E402
import runtime_paths  # noqa: E402


class RuntimePathsTest(unittest.TestCase):
    def test_macos_defaults_follow_application_support_and_caches(self) -> None:
        home = Path("/Users/example")
        self.assertEqual(
            runtime_paths.application_home(environ={}, system="Darwin", home=home),
            home / "Library" / "Application Support" / "Videomontazhka",
        )
        self.assertEqual(
            runtime_paths.cache_home(environ={}, system="Darwin", home=home),
            home / "Library" / "Caches" / "Videomontazhka",
        )

    def test_linux_defaults_honor_xdg(self) -> None:
        environment = {
            "XDG_DATA_HOME": "/srv/user-data",
            "XDG_CACHE_HOME": "/srv/user-cache",
        }
        self.assertEqual(
            runtime_paths.application_home(
                environ=environment,
                system="Linux",
                home=Path("/home/example"),
            ),
            Path("/srv/user-data/videomontazhka"),
        )
        self.assertEqual(
            runtime_paths.cache_home(
                environ=environment,
                system="Linux",
                home=Path("/home/example"),
            ),
            Path("/srv/user-cache/videomontazhka"),
        )

    def test_windows_defaults_honor_local_app_data(self) -> None:
        environment = {"LOCALAPPDATA": "/profiles/example/local"}
        self.assertEqual(
            runtime_paths.application_home(
                environ=environment,
                system="Windows",
                home=Path("/profiles/example"),
            ),
            Path("/profiles/example/local/Videomontazhka"),
        )
        self.assertEqual(
            runtime_paths.venv_executable(Path("/runtime"), "python", system="Windows"),
            Path("/runtime/Scripts/python.exe"),
        )

    def test_home_and_specific_root_overrides_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-paths-") as temporary:
            root = Path(temporary)
            environment = {
                "VIDEOMONTAZHKA_HOME": "app",
                "VIDEOMONTAZHKA_RUNTIME_DIR": "isolated-runtimes",
                "VIDEOMONTAZHKA_CACHE_HOME": "cache",
            }
            self.assertEqual(
                runtime_paths.application_home(environ=environment, cwd=root),
                (root / "app").resolve(),
            )
            self.assertEqual(
                runtime_paths.runtime_root(environ=environment, cwd=root),
                (root / "isolated-runtimes").resolve(),
            )
            self.assertEqual(
                runtime_paths.cache_home(environ=environment, cwd=root),
                (root / "cache").resolve(),
            )

    def test_component_names_fail_closed_on_path_traversal(self) -> None:
        for value in ("", ".", "..", "../escape", "nested/runtime", "/absolute"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runtime_paths.component_runtime(value, environ={})

    def test_runtime_installer_binds_the_product_runtime_and_constraints(self) -> None:
        self.assertEqual(install_runtime.DEFAULT_RUNTIME, runtime_paths.PYTHON_RUNTIME)
        constraints = install_runtime.requirement_constraints()
        self.assertEqual(constraints["numpy"], (("==", "2.4.6"),))
        self.assertTrue(install_runtime._satisfies("2.4.6", constraints["numpy"]))
        self.assertFalse(install_runtime._satisfies("2.4.7", constraints["numpy"]))
        self.assertTrue(constraints["requests"])


if __name__ == "__main__":
    unittest.main()
