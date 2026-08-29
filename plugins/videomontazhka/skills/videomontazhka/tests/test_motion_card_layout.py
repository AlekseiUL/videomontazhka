from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import render_motion_card as cards  # noqa: E402


try:
    import PIL  # noqa: F401
except ImportError:
    PIL_AVAILABLE = False
else:
    PIL_AVAILABLE = True


@unittest.skipUnless(PIL_AVAILABLE, "Pillow is required")
class MotionCardLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cards.load_pillow()
        cls.fonts = (cards.find_font(False), cards.find_font(True))

    @staticmethod
    def visible_text(layout: dict[str, Any]) -> str:
        fragments: list[str] = []
        for field in ("kicker", "title", "body"):
            block = layout.get(field)
            if isinstance(block, dict):
                fragments.extend(block.get("lines") or [])
        items = layout.get("items")
        if isinstance(items, dict):
            for entry in items.get("entries") or []:
                fragments.extend(entry.get("lines") or [])
        cta = layout.get("cta")
        if isinstance(cta, dict):
            fragments.append(str(cta.get("text") or ""))
        return " ".join(fragments)

    def test_vertical_process_card_keeps_all_text_and_platform_safe_area(self) -> None:
        spec = cards.validate_spec({
            "kind": "process",
            "width": 1080,
            "height": 1920,
            "title": "Как агент сохраняет память",
            "body": "Полный путь от факта до следующего действия",
            "items": [
                "Извлечь проверяемый факт",
                "Связать его с источником",
                "Вернуть в нужный момент",
            ],
            "cta": "Полная схема — в Telegram",
        })
        layout = cards.preflight_layout(spec, self.fonts)
        self.assertEqual(layout["safe_rect"], (80, 150, 930, 1500))
        visible = self.visible_text(layout)
        for phrase in [spec["title"], spec["body"], *spec["items"], spec["cta"]]:
            self.assertIn(phrase, visible)
        frame = cards.render_frame(spec, 30, False, self.fonts, layout)
        self.assertEqual(frame.size, (1080, 1920))

    def test_horizontal_compare_card_keeps_original_words(self) -> None:
        spec = cards.validate_spec({
            "kind": "compare",
            "width": 1920,
            "height": 1080,
            "kicker": "ПАМЯТЬ АГЕНТА",
            "title": "Запись — ещё не память",
            "items": [
                "Просто сохранить всё подряд",
                "Вернуть нужный смысл в нужный момент",
            ],
        })
        layout = cards.preflight_layout(spec, self.fonts)
        self.assertEqual(layout["safe_rect"], (100, 70, 1820, 1010))
        visible = self.visible_text(layout)
        for phrase in [spec["kicker"], spec["title"], *spec["items"]]:
            self.assertIn(phrase, visible)

    def test_overlong_single_token_is_rejected_instead_of_truncated(self) -> None:
        spec = cards.validate_spec({
            "kind": "title",
            "width": 320,
            "height": 320,
            "title": "X" * 500,
        })
        with self.assertRaisesRegex(cards.CardError, "overlong single token"):
            cards.preflight_layout(spec, self.fonts)

    def test_item_list_is_rejected_instead_of_silently_truncated(self) -> None:
        with self.assertRaisesRegex(cards.CardError, "refusing to truncate"):
            cards.validate_spec({
                "kind": "process",
                "title": "Too many steps",
                "items": ["one", "two", "three", "four", "five", "six"],
            })


if __name__ == "__main__":
    unittest.main()
