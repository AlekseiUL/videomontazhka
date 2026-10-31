"""Exercise the documented fallback in disposable HOME directories only."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
ROOT = SKILL.parents[3]
SCRIPT = SKILL / "scripts" / "install_skill.py"


class SkillInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skill-install-")
        self.addCleanup(self.temp.cleanup)
        # macOS TMPDIR can use /var -> /private/var. Relative links must be
        # computed from the physical directory, not its differently deep alias.
        self.home = Path(self.temp.name).resolve()
        self.target = self.home / ".codex" / "skills" / "videomontazhka"

    def run_install(self):
        return subprocess.run(
            [sys.executable, str(SCRIPT)], cwd=self.home,
            env={"HOME": str(self.home), "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True, capture_output=True,
        )

    def prepare_parent(self):
        self.target.parent.mkdir(parents=True)

    def test_absent_then_repeat_preserves_link(self):
        first = self.run_install()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.target.resolve(strict=True), SKILL)
        inode = self.target.lstat().st_ino
        second = self.run_install()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.target.lstat().st_ino, inode)

    def test_equivalent_relative_link_is_success(self):
        self.prepare_parent()
        self.target.symlink_to(os.path.relpath(SKILL, self.target.parent))
        original = os.readlink(self.target)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(self.target), original)

    def test_conflicts_are_preserved(self):
        for kind in ("file", "directory", "foreign-link", "broken-link", "loop-link"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(dir=self.home) as folder:
                original_home = self.home
                self.home = Path(folder)
                self.target = self.home / ".codex" / "skills" / "videomontazhka"
                self.prepare_parent()
                other = self.home / "other"
                other.mkdir()
                marker = other / "keep.txt"
                marker.write_text("keep me")
                if kind == "file":
                    self.target.write_text("keep me")
                elif kind == "directory":
                    self.target.mkdir()
                    (self.target / "keep.txt").write_text("keep me")
                else:
                    link = {"foreign-link": other, "broken-link": self.home / "missing",
                            "loop-link": self.target}[kind]
                    self.target.symlink_to(link)
                inode = self.target.lstat().st_ino
                link_before = os.readlink(self.target) if self.target.is_symlink() else None
                result = self.run_install()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Refusing to replace", result.stderr)
                self.assertIn("backup", result.stderr)
                self.assertEqual(self.target.lstat().st_ino, inode)
                self.assertEqual(marker.read_text(), "keep me")
                if link_before is not None:
                    self.assertEqual(os.readlink(self.target), link_before)
                elif kind == "file":
                    self.assertEqual(self.target.read_text(), "keep me")
                else:
                    self.assertEqual((self.target / "keep.txt").read_text(), "keep me")
                self.home = original_home

    def test_symlinked_parent_is_refused(self):
        outside = self.home / "outside"
        outside.mkdir()
        (self.home / ".codex").symlink_to(outside)
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinked install directory", result.stderr)
        self.assertEqual(list(outside.iterdir()), [])

    def test_readme_versions_and_fallback_match_manifest(self):
        version = json.loads((SKILL.parents[1] / ".codex-plugin/plugin.json").read_text())["version"]
        for name in ("README.md", "README.ru.md"):
            text = (ROOT / name).read_text()
            self.assertIn(f"public beta {version}", text)
        for name in ("README.md", "README.ru.md", "docs/INSTALL.md"):
            text = (ROOT / name).read_text()
            self.assertNotIn("ln -sfn", text)
            self.assertIn("python3.12 plugins/videomontazhka/skills/videomontazhka/scripts/install_skill.py", text)


if __name__ == "__main__":
    unittest.main()
