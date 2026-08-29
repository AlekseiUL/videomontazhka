from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import render_edl as renderer  # noqa: E402


def make_mov(path: Path, color: str) -> None:
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s=320x320:r=30:d=1",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1",
            "-map", "0:v:0", "-map", "1:a:0", "-frames:v", "30",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "27",
            "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
            "-map_metadata", "-1", str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg and ffprobe are required",
)
class RenderCacheAttestationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sprut-render-cache-")
        self.root = Path(self.temporary.name)
        self.source_path = self.root / "source.mov"
        make_mov(self.source_path, "black")
        info = renderer.probe(self.source_path)
        self.source = renderer.SourceInfo(
            path=self.source_path.resolve(),
            duration_s=float(info["duration_s"]),
            has_audio=bool(info["has_audio"]),
            audio_duration_s=info["audio_duration_s"],
            coded_width=int(info["coded_width"]),
            coded_height=int(info["coded_height"]),
            width=int(info["width"]),
            height=int(info["height"]),
            display_rotation_degrees=int(info["display_rotation_degrees"]),
            fps=info["fps"],
            color_transfer=info["color_transfer"],
            fingerprint=renderer.file_sha256(self.source_path),
        )
        self.profile = renderer.Profile(
            "preview", 320, 320, 320, 320, Fraction(30, 1), "ultrafast", 27
        )
        self.layout = {
            "source": "source-1",
            "start": 0.0,
            "end": 1.0,
            "source_class": "full_frame_presenter",
            "output_shape": "full_frame",
            "composition": "preserve_source",
            "reason": "Preserve the approved test frame.",
        }
        self.plan = renderer.SegmentPlan(
            0,
            {
                "source": "source-1",
                "start": 0.0,
                "end": 1.0,
                "audio_mode": "source",
                "view_filter": "hflip",
                "_layout": self.layout,
            },
            30,
            48_000,
        )
        self.grade = "eq=contrast=1.01"
        self.identity_sha256 = "a" * 64
        self.cache_dir = self.root / "cache"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def recipe(self) -> dict[str, Any]:
        vf, video_is_complex = renderer.video_filter_spec(
            self.plan, self.source, self.profile, self.grade
        )
        af = renderer.audio_filter(self.plan, self.source)
        return renderer.segment_cache_recipe(
            self.plan,
            self.source,
            self.profile,
            vf,
            af,
            self.identity_sha256,
            grade=self.grade,
            video_is_complex=video_is_complex,
        )

    def test_failed_render_never_publishes_cache_attestation(self) -> None:
        recipe = self.recipe()
        cache_key = renderer.segment_recipe_sha256(recipe)
        cache_path = self.cache_dir / self.profile.mode / f"{cache_key}.mov"
        cache_path.parent.mkdir(parents=True)
        failed_render = cache_path.with_name(".failed.part.mov")
        failed_render.write_bytes(b"not a rendered segment")

        with self.assertRaises(renderer.RenderError):
            renderer.publish_attested_cache_segment(
                failed_render,
                cache_path,
                self.profile,
                self.plan,
                cache_key,
                recipe,
            )
        self.assertFalse(cache_path.exists())
        self.assertFalse(renderer.cache_attestation_path(cache_path).exists())

    def test_audio_mode_is_recipe_bound_and_mute_uses_generated_silence(self) -> None:
        source_recipe = self.recipe()
        muted_item = copy.deepcopy(self.plan.item)
        muted_item["audio_mode"] = "mute"
        muted_plan = renderer.SegmentPlan(
            self.plan.index,
            muted_item,
            self.plan.frame_count,
            self.plan.sample_count,
        )
        vf, is_complex = renderer.video_filter_spec(
            muted_plan, self.source, self.profile, self.grade
        )
        af = renderer.audio_filter(muted_plan, self.source)
        muted_recipe = renderer.segment_cache_recipe(
            muted_plan,
            self.source,
            self.profile,
            vf,
            af,
            self.identity_sha256,
            grade=self.grade,
            video_is_complex=is_complex,
        )
        self.assertEqual(source_recipe["edit"]["audio_mode"], "source")
        self.assertEqual(muted_recipe["edit"]["audio_mode"], "mute")
        self.assertNotEqual(
            renderer.segment_recipe_sha256(source_recipe),
            renderer.segment_recipe_sha256(muted_recipe),
        )

        observed: list[str] = []

        def capture(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            observed.extend(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(renderer, "run", side_effect=capture):
            renderer.extract_segment(
                muted_plan,
                self.source,
                self.profile,
                vf,
                is_complex,
                af,
                self.root / "muted.mov",
            )
        self.assertIn("anullsrc=r=48000:cl=stereo", observed)
        map_pairs = list(zip(observed, observed[1:]))
        self.assertIn(("-map", "1:a:0"), map_pairs)

    def test_sidecar_failure_after_mov_replace_stays_fail_closed(self) -> None:
        recipe = self.recipe()
        cache_key = renderer.segment_recipe_sha256(recipe)
        cache_path = self.cache_dir / self.profile.mode / f"{cache_key}.mov"
        cache_path.parent.mkdir(parents=True)
        valid_render = cache_path.with_name(".valid.part.mov")
        make_mov(valid_render, "black")

        with mock.patch.object(
            renderer,
            "write_json_atomic",
            side_effect=OSError("simulated sidecar publication failure"),
        ):
            with self.assertRaises(OSError):
                renderer.publish_attested_cache_segment(
                    valid_render,
                    cache_path,
                    self.profile,
                    self.plan,
                    cache_key,
                    recipe,
                )
        self.assertTrue(cache_path.is_file())
        self.assertFalse(renderer.cache_attestation_path(cache_path).exists())
        valid, _ = renderer.valid_attested_cached_segment(
            cache_path, self.profile, self.plan, cache_key, recipe
        )
        self.assertFalse(valid)

    def test_replaced_source_is_refused_before_cache_or_render(self) -> None:
        replacement = self.root / "replacement-source.mov"
        make_mov(replacement, "red")
        os.replace(replacement, self.source_path)
        recipe = self.recipe()
        cache_key = renderer.segment_recipe_sha256(recipe)
        cache_path = self.cache_dir / self.profile.mode / f"{cache_key}.mov"

        with self.assertRaisesRegex(renderer.RenderError, "source SHA-256 changed"):
            renderer.build_segments(
                [self.plan],
                {"source-1": self.source},
                self.profile,
                self.grade,
                self.cache_dir,
                self.root / "work-source-replaced",
                True,
                self.identity_sha256,
            )
        self.assertFalse(cache_path.exists())
        self.assertFalse(renderer.cache_attestation_path(cache_path).exists())

    def test_same_format_substitution_is_rejected_and_rebuilt(self) -> None:
        first_paths, first_records = renderer.build_segments(
            [self.plan],
            {"source-1": self.source},
            self.profile,
            self.grade,
            self.cache_dir,
            self.root / "work-first",
            True,
            self.identity_sha256,
        )
        self.assertEqual(len(first_paths), 1)
        cache_key = first_records[0]["cache_key"]
        cache_path = self.cache_dir / self.profile.mode / f"{cache_key}.mov"
        attestation_path = renderer.cache_attestation_path(cache_path)
        self.assertTrue(attestation_path.is_file())
        self.assertFalse(os.path.samefile(cache_path, first_paths[0]))

        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        recipe = self.recipe()
        self.assertEqual(attestation["render_recipe"], recipe)
        self.assertEqual(recipe["source"]["sha256"], self.source.fingerprint)
        self.assertEqual(
            (recipe["source"]["start_s"], recipe["source"]["end_s"]),
            (0.0, 1.0),
        )
        self.assertEqual(recipe["edit"]["layout"], self.layout)
        self.assertEqual(recipe["edit"]["view_filter"], "hflip")
        self.assertEqual(recipe["edit"]["grade"], self.grade)
        self.assertEqual(recipe["profile"]["mode"], "preview")
        self.assertEqual(
            recipe["renderer"]["render_code_toolchain_identity_sha256"],
            self.identity_sha256,
        )

        dimensions = {
            "source hash": (("source", "sha256"), "b" * 64),
            "source bounds": (("source", "start_s"), 0.125),
            "layout": (("edit", "layout", "composition"), "screen_only"),
            "crop": (("edit", "crop"), "crop=300:300:10:10"),
            "view": (("edit", "view_filter"), "vflip"),
            "grade": (("edit", "grade"), "eq=contrast=1.2"),
            "profile": (("profile", "crf"), 18),
            "render/toolchain identity": (
                ("renderer", "render_code_toolchain_identity_sha256"),
                "c" * 64,
            ),
        }
        for label, (path, replacement) in dimensions.items():
            with self.subTest(recipe_dimension=label):
                changed = copy.deepcopy(recipe)
                target: Any = changed
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = replacement
                changed_key = renderer.segment_recipe_sha256(changed)
                self.assertNotEqual(changed_key, cache_key)
                valid, _ = renderer.valid_attested_cached_segment(
                    cache_path, self.profile, self.plan, changed_key, changed
                )
                self.assertFalse(valid)

        ordered_sha256 = renderer.file_sha256(first_paths[0])
        with cache_path.open("r+b") as handle:
            handle.seek(cache_path.stat().st_size // 2)
            original = handle.read(1)
            self.assertTrue(original)
            handle.seek(-1, os.SEEK_CUR)
            handle.write(bytes([original[0] ^ 0xFF]))
        self.assertEqual(renderer.file_sha256(first_paths[0]), ordered_sha256)

        poison = self.root / "same-format-poison.mov"
        make_mov(poison, "red")
        self.assertTrue(renderer.valid_segment_structure(poison, self.profile, self.plan))
        poison_sha256 = renderer.file_sha256(poison)
        os.replace(poison, cache_path)
        # Make every cheap sidecar property agree with the replacement while
        # retaining the attested SHA. Rejection must therefore depend on a
        # fresh hash of the current same-format MOV bytes.
        attestation["segment"]["size_bytes"] = cache_path.stat().st_size
        self.assertNotEqual(attestation["segment"]["sha256"], poison_sha256)
        renderer.write_json_atomic(attestation_path, attestation)

        valid, _ = renderer.valid_attested_cached_segment(
            cache_path, self.profile, self.plan, cache_key, recipe
        )
        self.assertFalse(valid, "same-format substituted bytes must not be a cache hit")

        _, rebuilt_records = renderer.build_segments(
            [self.plan],
            {"source-1": self.source},
            self.profile,
            self.grade,
            self.cache_dir,
            self.root / "work-rebuilt",
            True,
            self.identity_sha256,
        )
        self.assertNotEqual(renderer.file_sha256(cache_path), poison_sha256)
        self.assertEqual(rebuilt_records[0]["sha256"], renderer.file_sha256(cache_path))
        valid, current_sha256 = renderer.valid_attested_cached_segment(
            cache_path, self.profile, self.plan, cache_key, recipe
        )
        self.assertTrue(valid)
        self.assertEqual(current_sha256, rebuilt_records[0]["sha256"])

        # The unmodified pair is reusable; extraction must not run on an exact
        # recipe + current-byte-hash match.
        with mock.patch.object(
            renderer,
            "extract_segment",
            side_effect=AssertionError("attested cache hit was rendered again"),
        ):
            hit_paths, hit_records = renderer.build_segments(
                [self.plan],
                {"source-1": self.source},
                self.profile,
                self.grade,
                self.cache_dir,
                self.root / "work-hit",
                True,
                self.identity_sha256,
            )
        self.assertEqual(len(hit_paths), 1)
        self.assertEqual(hit_records[0]["sha256"], current_sha256)


if __name__ == "__main__":
    unittest.main()
