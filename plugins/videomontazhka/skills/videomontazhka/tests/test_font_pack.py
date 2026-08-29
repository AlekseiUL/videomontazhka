from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FONT_ROOT = ROOT / "assets" / "fonts"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class FontPackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads((FONT_ROOT / "manifest.json").read_text(encoding="utf-8"))

    def test_every_font_and_license_matches_its_declared_hash(self) -> None:
        self.assertEqual(self.manifest["policy"], "local_only")
        self.assertGreaterEqual(len(self.manifest["families"]), 3)
        for family in self.manifest["families"]:
            with self.subTest(family=family["family"]):
                font = FONT_ROOT / family["file"]
                license_file = FONT_ROOT / family["license_file"]
                self.assertTrue(font.is_file())
                self.assertTrue(license_file.is_file())
                self.assertEqual(sha256(font), family["sha256"])
                self.assertEqual(sha256(license_file), family["license_sha256"])
                self.assertEqual(family["license"], "SIL Open Font License 1.1")
                self.assertTrue(family["cyrillic_basic"])

    @unittest.skipUnless(shutil.which("fc-query"), "fontconfig is not installed")
    def test_every_font_declares_cyrillic_glyphs(self) -> None:
        def has_codepoint(charset: str, codepoint: int) -> bool:
            for token in charset.lower().split():
                start_text, separator, end_text = token.partition("-")
                try:
                    start = int(start_text, 16)
                    end = int(end_text, 16) if separator else start
                except ValueError:
                    continue
                if start <= codepoint <= end:
                    return True
            return False

        for family in self.manifest["families"]:
            with self.subTest(family=family["family"]):
                font = FONT_ROOT / family["file"]
                result = subprocess.run(
                    ["fc-query", "--format", "%{charset}", str(font)],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                # U+0410 ('А'), U+044F ('я'), and U+0451 ('ё') exercise the
                # Russian uppercase/lowercase alphabet and its key extension.
                self.assertTrue(has_codepoint(result.stdout, 0x0410))
                self.assertTrue(has_codepoint(result.stdout, 0x044F))
                self.assertTrue(has_codepoint(result.stdout, 0x0451))


if __name__ == "__main__":
    unittest.main()
