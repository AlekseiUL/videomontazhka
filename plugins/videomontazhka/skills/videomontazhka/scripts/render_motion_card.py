#!/usr/bin/env python3
"""Render reusable SPRUT brand cards locally with Pillow and FFmpeg."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate
from visual_asset_provenance import (
    VisualProvenanceError,
    assert_snapshots_current,
    atomic_write_json,
    build_motion_card_provenance,
    capture_generator_snapshots,
    invalidate_provenance,
    load_approved_visual_contract,
    load_json_object_snapshot,
    provenance_path_for,
    verify_visual_asset_provenance,
)


KINDS = {"title", "chapter", "definition", "compare", "process", "quote", "cta"}
Image: Any = None
ImageDraw: Any = None
ImageFont: Any = None


class CardError(RuntimeError):
    pass


def load_pillow() -> None:
    global Image, ImageDraw, ImageFont
    try:
        from PIL import Image as pillow_image
        from PIL import ImageDraw as pillow_draw
        from PIL import ImageFont as pillow_font
    except ImportError as exc:
        raise CardError("Pillow is required") from exc
    Image = pillow_image
    ImageDraw = pillow_draw
    ImageFont = pillow_font


def color(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    text = value.lstrip("#")
    if len(text) != 6:
        raise CardError(f"invalid color: {value}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), alpha


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def ease_out_cubic(value: float) -> float:
    t = clamp(value)
    return 1 - (1 - t) ** 3


def ease_in_out_cubic(value: float) -> float:
    t = clamp(value)
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def progress(time_s: float, start: float, duration: float) -> float:
    return ease_out_cubic((time_s - start) / max(duration, 0.001))


def find_font(bold: bool, requested: str | None = None) -> Path:
    candidates: list[Path] = []
    if requested:
        candidates.append(Path(requested).expanduser())
    skill_root = Path(__file__).resolve().parent.parent
    candidates.extend([
        skill_root / "assets/fonts/Inter-Bold.ttf" if bold else skill_root / "assets/fonts/Inter-Regular.ttf",
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf") if bold else Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/System/Library/Fonts/Helvetica.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf") if bold else Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise CardError("no usable local font found")


def font(path: Path, size: int, *, index: int = 0) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(path), size=size, index=index)
    except OSError as exc:
        raise CardError(f"cannot load font {path}: {exc}") from exc


def essential_safe_rect(width: int, height: int) -> tuple[int, int, int, int]:
    """Return the documented output-space essential-content rectangle."""
    if height > width:
        scale_x = width / 1080
        scale_y = height / 1920
        top = round(150 * scale_y)
        right = round(150 * scale_x)
        bottom = round(420 * scale_y)
        left = round(80 * scale_x)
    else:
        scale_x = width / 1920
        scale_y = height / 1080
        top = round(70 * scale_y)
        right = round(100 * scale_x)
        bottom = round(70 * scale_y)
        left = round(100 * scale_x)
    rectangle = (left, top, width - right, height - bottom)
    if rectangle[2] - rectangle[0] < 64 or rectangle[3] - rectangle[1] < 64:
        raise CardError("documented safe-area insets leave too little usable space")
    return rectangle


def layout_scale(width: int, height: int) -> float:
    reference = (1080, 1920) if height > width else (1920, 1080)
    return min(width / reference[0], height / reference[1])


def text_width(draw: ImageDraw.ImageDraw, text: str, face: ImageFont.ImageFont) -> int:
    if not text:
        return 0
    box = draw.textbbox((0, 0), text, font=face, anchor="lt")
    return max(0, box[2] - min(0, box[0]))


def line_height(face: ImageFont.ImageFont) -> int:
    if hasattr(face, "getmetrics"):
        ascent, descent = face.getmetrics()
        return max(1, ascent + descent)
    box = face.getbbox("Ag")
    return max(1, box[3] - box[1])


def block_height(face: ImageFont.ImageFont, lines: list[str], spacing: int) -> int:
    if not lines:
        return 0
    return line_height(face) * len(lines) + spacing * max(0, len(lines) - 1)


def wrap(draw: ImageDraw.ImageDraw, text: str, face: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.strip().splitlines():
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if text_width(draw, candidate, face) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    path: Path,
    max_size: int,
    min_size: int,
    max_width: int,
    max_lines: int,
    *,
    field: str = "text",
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    if max_width <= 0 or max_lines < 1:
        raise CardError(f"{field} has no usable layout area")
    max_size = max(1, int(max_size))
    min_size = max(1, min(int(min_size), max_size))
    minimum_face = font(path, min_size)
    for token in text.split():
        if text_width(draw, token, minimum_face) > max_width:
            raise CardError(
                f"{field} contains an overlong single token ({len(token)} characters) "
                f"that exceeds the safe width at the minimum font size"
            )
    sizes = list(range(max_size, min_size - 1, -2))
    if not sizes or sizes[-1] != min_size:
        sizes.append(min_size)
    minimum_lines: list[str] = []
    for size in sizes:
        face = font(path, size)
        lines = wrap(draw, text, face, max_width)
        if size == min_size:
            minimum_lines = lines
        if len(lines) <= max_lines and all(text_width(draw, line, face) <= max_width for line in lines):
            return face, lines
    raise CardError(
        f"{field} does not fit the safe width without truncation "
        f"({len(minimum_lines)} lines required; maximum is {max_lines})"
    )


def draw_text_block(
    canvas: Image.Image,
    position: tuple[int, int],
    lines: list[str],
    face: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
    *,
    spacing: int,
    opacity: float,
    slide_x: int = 0,
) -> int:
    if not lines or opacity <= 0:
        return position[1]
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    x, y = position
    rgba = (*fill[:3], round(fill[3] * clamp(opacity)))
    step = line_height(face) + spacing
    for line in lines:
        draw.text((x + slide_x, y), line, font=face, fill=rgba, anchor="lt")
        y += step
    canvas.alpha_composite(layer)
    return y - spacing


def _assert_inside(
    safe: tuple[int, int, int, int],
    bounds: tuple[int, int, int, int],
    field: str,
) -> None:
    left, top, right, bottom = safe
    x0, y0, x1, y1 = bounds
    if x0 < left or y0 < top or x1 > right or y1 > bottom:
        raise CardError(
            f"{field} falls outside the essential-content safe rectangle: "
            f"{bounds} not within {safe}"
        )


def _fit_item_rows(
    draw: ImageDraw.ImageDraw,
    items: list[str],
    path: Path,
    max_size: int,
    min_size: int,
    max_width: int,
    max_lines: int,
    bullet_size: int,
    gap: int,
    available_height: int,
) -> tuple[ImageFont.FreeTypeFont, list[list[str]], list[int], int]:
    minimum_face = font(path, min_size)
    for index, item in enumerate(items):
        for token in item.split():
            if text_width(draw, token, minimum_face) > max_width:
                raise CardError(
                    f"items[{index}] contains an overlong single token ({len(token)} characters) "
                    f"that exceeds the safe width at the minimum font size"
                )
    sizes = list(range(max_size, min_size - 1, -2))
    if not sizes or sizes[-1] != min_size:
        sizes.append(min_size)
    for size in sizes:
        face = font(path, size)
        lines = [wrap(draw, item, face, max_width) for item in items]
        if any(len(item_lines) > max_lines for item_lines in lines):
            continue
        if any(text_width(draw, line, face) > max_width for item_lines in lines for line in item_lines):
            continue
        spacing = max(3, round(6 * size / max(1, max_size)))
        row_heights = [max(bullet_size, block_height(face, item_lines, spacing)) for item_lines in lines]
        total = sum(row_heights) + gap * max(0, len(items) - 1)
        if total <= available_height:
            return face, lines, row_heights, spacing
    raise CardError("items do not fit the available safe-area height without truncation")


def preflight_layout(spec: dict[str, Any], fonts: tuple[Path, Path]) -> dict[str, Any]:
    """Resolve and validate every essential-content box before rendering frames."""
    width = int(spec["width"])
    height = int(spec["height"])
    regular_path, bold_path = fonts
    safe = essential_safe_rect(width, height)
    safe_left, safe_top, safe_right, safe_bottom = safe
    content_w = safe_right - safe_left
    safe_h = safe_bottom - safe_top
    scale = layout_scale(width, height)
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
    kind = str(spec["kind"])

    # Validate colors here too, so malformed input fails before a frame exists.
    color(str(spec.get("background", "#070707")))
    color(str(spec.get("accent", "#FF6A00")))
    color(str(spec.get("primary", "#FFFFFF")))
    color(str(spec.get("secondary", "#A8A8A8")))
    color(str(spec.get("panel", "#121212")))

    mark_size = max(8, round(12 * scale))
    line_height_px = max(4, round(6 * scale))
    line_y = safe_top + round(52 * scale)
    if line_y + line_height_px > safe_bottom:
        raise CardError("brand line does not fit the essential-content safe rectangle")

    kicker_text = str(spec.get("kicker") or "").strip().upper()
    kicker_layout: dict[str, Any] | None = None
    if kicker_text:
        kicker_slide = max(8, round(24 * scale))
        kicker_face, kicker_lines = fit_font(
            measure,
            kicker_text,
            bold_path,
            max(18, round(31 * scale)),
            max(14, round(18 * scale)),
            content_w - kicker_slide,
            1,
            field="kicker",
        )
        kicker_y = line_y + round(30 * scale)
        kicker_h = block_height(kicker_face, kicker_lines, 0)
        kicker_w = max(text_width(measure, line, kicker_face) for line in kicker_lines)
        _assert_inside(
            safe,
            (safe_left, kicker_y, safe_left + kicker_w + kicker_slide, kicker_y + kicker_h),
            "kicker",
        )
        kicker_layout = {
            "text": kicker_text,
            "lines": kicker_lines,
            "face": kicker_face,
            "x": safe_left,
            "y": kicker_y,
            "height": kicker_h,
            "slide_x": kicker_slide,
        }

    title_text = str(spec.get("title") or "").strip()
    title_slide = max(10, round(42 * scale))
    title_top = line_y + round((90 if kicker_layout else 55) * scale)
    if kicker_layout:
        title_top = max(title_top, kicker_layout["y"] + kicker_layout["height"] + max(8, round(18 * scale)))
    max_title = round((94 if kind in {"title", "chapter", "cta"} else 78) * scale)
    title_spacing = max(6, round(10 * scale))
    title_face, title_lines = fit_font(
        measure,
        title_text,
        bold_path,
        max(32, max_title),
        max(24, round(48 * scale)),
        content_w - title_slide,
        3,
        field="title",
    )
    title_h = block_height(title_face, title_lines, title_spacing)
    title_w = max(text_width(measure, line, title_face) for line in title_lines)
    _assert_inside(
        safe,
        (safe_left, title_top, safe_left + title_w + title_slide, title_top + title_h),
        "title",
    )
    title_layout = {
        "lines": title_lines,
        "face": title_face,
        "x": safe_left,
        "y": title_top,
        "height": title_h,
        "spacing": title_spacing,
        "slide_x": title_slide,
    }

    body_text = str(spec.get("body") or "").strip()
    body_layout: dict[str, Any] | None = None
    cluster_bottom = title_top + title_h
    if body_text:
        body_slide = max(8, round(24 * scale))
        body_spacing = max(4, round(8 * scale))
        body_face, body_lines = fit_font(
            measure,
            body_text,
            regular_path,
            max(24, round(39 * scale)),
            max(18, round(27 * scale)),
            content_w - body_slide,
            3,
            field="body",
        )
        body_y = cluster_bottom + round(28 * scale)
        body_h = block_height(body_face, body_lines, body_spacing)
        body_w = max(text_width(measure, line, body_face) for line in body_lines)
        _assert_inside(
            safe,
            (safe_left, body_y, safe_left + body_w + body_slide, body_y + body_h),
            "body",
        )
        body_layout = {
            "lines": body_lines,
            "face": body_face,
            "x": safe_left,
            "y": body_y,
            "height": body_h,
            "spacing": body_spacing,
            "slide_x": body_slide,
        }
        cluster_bottom = body_y + body_h

    cta_text = str(spec.get("cta") or "").strip()
    cta_layout: dict[str, Any] | None = None
    cta_gap = max(14, round(28 * scale))
    item_limit = safe_bottom
    if cta_text:
        cta_pad_x = max(18, round(29 * scale))
        cta_face, cta_lines = fit_font(
            measure,
            cta_text,
            bold_path,
            max(17, round(27 * scale)),
            max(13, round(17 * scale)),
            content_w - 2 * cta_pad_x,
            1,
            field="cta",
        )
        cta_text_w = text_width(measure, cta_lines[0], cta_face)
        cta_text_h = line_height(cta_face)
        cta_h = max(max(38, round(58 * scale)), cta_text_h + max(12, round(20 * scale)))
        cta_w = cta_text_w + 2 * cta_pad_x
        cta_x = safe_left
        cta_y = safe_bottom - cta_h
        cta_text_y = cta_y + (cta_h - cta_text_h) // 2
        _assert_inside(safe, (cta_x, cta_y, cta_x + cta_w, cta_y + cta_h), "cta button")
        _assert_inside(
            safe,
            (cta_x + cta_pad_x, cta_text_y, cta_x + cta_pad_x + cta_text_w, cta_text_y + cta_text_h),
            "cta text",
        )
        cta_layout = {
            "text": cta_text,
            "face": cta_face,
            "x": cta_x,
            "y": cta_y,
            "width": cta_w,
            "height": cta_h,
            "text_x": cta_x + cta_pad_x,
            "text_y": cta_text_y,
        }
        item_limit = cta_y - cta_gap

    items = list(spec.get("items") or [])
    items_layout: dict[str, Any] | None = None
    if items:
        cluster_gap = max(16, round(34 * scale))
        earliest_start = cluster_bottom + cluster_gap
        available_h = item_limit - earliest_start
        if available_h <= 0:
            raise CardError("title/body leave no safe-area height for items")
        preferred_start = safe_top + round(safe_h * 0.52)
        if kind == "compare":
            cell_gap = max(14, round(28 * scale))
            cell_w = (content_w - cell_gap) // 2
            pad_x = max(14, round(28 * scale))
            pad_top = max(20, round(42 * scale))
            pad_bottom = max(16, round(32 * scale))
            item_text_w = cell_w - 2 * pad_x
            motion_y = max(8, round(24 * scale))
            max_item_size = max(22, round(36 * scale))
            min_item_size = max(16, round(22 * scale))
            minimum_face = font(bold_path, min_item_size)
            for index, item in enumerate(items):
                for token in item.split():
                    if text_width(measure, token, minimum_face) > item_text_w:
                        raise CardError(
                            f"items[{index}] contains an overlong single token ({len(token)} characters) "
                            f"that exceeds the compare-cell safe width"
                        )
            item_face: ImageFont.FreeTypeFont | None = None
            item_lines: list[list[str]] = []
            item_spacing = max(4, round(7 * scale))
            cell_h = 0
            sizes = list(range(max_item_size, min_item_size - 1, -2))
            if not sizes or sizes[-1] != min_item_size:
                sizes.append(min_item_size)
            for size in sizes:
                candidate_face = font(bold_path, size)
                candidate_lines = [wrap(measure, item, candidate_face, item_text_w) for item in items]
                if any(len(lines) > 4 for lines in candidate_lines):
                    continue
                if any(
                    text_width(measure, line, candidate_face) > item_text_w
                    for lines in candidate_lines
                    for line in lines
                ):
                    continue
                candidate_h = max(
                    max(90, round(150 * scale)),
                    pad_top + max(block_height(candidate_face, lines, item_spacing) for lines in candidate_lines) + pad_bottom,
                )
                if candidate_h + motion_y <= available_h:
                    item_face = candidate_face
                    item_lines = candidate_lines
                    cell_h = candidate_h
                    break
            if item_face is None:
                raise CardError("compare items do not fit the safe-area cells without truncation")
            start_y = min(max(earliest_start, preferred_start), item_limit - cell_h - motion_y)
            if start_y < earliest_start:
                raise CardError("compare layout cannot fit vertically inside the essential safe rectangle")
            entries = []
            for index, lines in enumerate(item_lines):
                x0 = safe_left + index * (cell_w + cell_gap)
                entry = {
                    "lines": lines,
                    "x": x0,
                    "y": start_y,
                    "width": cell_w,
                    "height": cell_h,
                    "text_x": x0 + pad_x,
                    "text_y": start_y + pad_top,
                }
                _assert_inside(safe, (x0, start_y, x0 + cell_w, start_y + cell_h + motion_y), f"items[{index}]")
                entries.append(entry)
            items_layout = {
                "mode": "compare",
                "face": item_face,
                "spacing": item_spacing,
                "motion_y": motion_y,
                "entries": entries,
            }
        else:
            gap = max(10, round(16 * scale))
            bullet_size = max(34, round(46 * scale))
            indent = max(52, round(68 * scale))
            text_slide = max(7, round(20 * scale))
            item_text_w = content_w - indent - text_slide
            max_item_size = max(20, round(32 * scale))
            min_item_size = max(15, round(20 * scale))
            item_face, item_lines, row_heights, item_spacing = _fit_item_rows(
                measure,
                items,
                bold_path if kind == "process" else regular_path,
                max_item_size,
                min_item_size,
                item_text_w,
                3,
                bullet_size,
                gap,
                available_h,
            )
            pack_h = sum(row_heights) + gap * max(0, len(items) - 1)
            start_y = min(max(earliest_start, preferred_start), item_limit - pack_h)
            if start_y < earliest_start:
                raise CardError("item rows cannot fit vertically inside the essential safe rectangle")
            entries = []
            cursor_y = start_y
            for index, (lines, row_h) in enumerate(zip(item_lines, row_heights)):
                item_h = block_height(item_face, lines, item_spacing)
                bullet_y = cursor_y + (row_h - bullet_size) // 2
                text_y = cursor_y + (row_h - item_h) // 2
                item_w = max(text_width(measure, line, item_face) for line in lines)
                _assert_inside(
                    safe,
                    (safe_left, cursor_y, safe_left + indent + text_slide + item_w, cursor_y + row_h),
                    f"items[{index}]",
                )
                entries.append({
                    "lines": lines,
                    "row_y": cursor_y,
                    "row_height": row_h,
                    "bullet_y": bullet_y,
                    "text_y": text_y,
                })
                cursor_y += row_h + gap
            items_layout = {
                "mode": "rows",
                "face": item_face,
                "spacing": item_spacing,
                "bullet_size": bullet_size,
                "indent": indent,
                "text_slide": text_slide,
                "entries": entries,
            }

    cluster_limit = item_limit if not items_layout else min(
        entry.get("row_y", entry.get("y", item_limit)) for entry in items_layout["entries"]
    ) - max(8, round(16 * scale))
    if cluster_bottom > cluster_limit:
        raise CardError("title/body collide with items or CTA inside the essential safe rectangle")

    panel_rect = (
        max(20, safe_left - round(55 * scale)),
        max(20, safe_top - round(55 * scale)),
        min(width - 20, safe_right + round(55 * scale)),
        min(height - 20, safe_bottom + round(55 * scale)),
    )
    return {
        "safe_rect": safe,
        "scale": scale,
        "content_width": content_w,
        "panel_rect": panel_rect,
        "mark_rect": (safe_left, safe_top, safe_left + mark_size, safe_top + mark_size),
        "line_y": line_y,
        "line_height": line_height_px,
        "kicker": kicker_layout,
        "title": title_layout,
        "body": body_layout,
        "items": items_layout,
        "cta": cta_layout,
    }


def render_frame(
    spec: dict[str, Any],
    frame_index: int,
    transparent: bool,
    fonts: tuple[Path, Path],
    layout: dict[str, Any] | None = None,
) -> Image.Image:
    width = int(spec["width"])
    height = int(spec["height"])
    fps = float(spec["fps"])
    time_s = frame_index / fps
    background = color(str(spec.get("background", "#070707")), 0 if transparent else 255)
    accent = color(str(spec.get("accent", "#FF6A00")))
    primary = color(str(spec.get("primary", "#FFFFFF")))
    secondary = color(str(spec.get("secondary", "#A8A8A8")))
    panel = color(str(spec.get("panel", "#121212")), 238 if not transparent else 220)
    _, bold_path = fonts
    layout = layout or preflight_layout(spec, fonts)

    image = Image.new("RGBA", (width, height), background)
    draw = ImageDraw.Draw(image)
    scale = float(layout["scale"])
    safe_left, _, _, _ = layout["safe_rect"]

    # Keep the first frame intentionally dark but visibly designed. A large
    # #0F0F0F field prevents the card boundary from looking like an accidental
    # one-frame black blink before text animation begins.
    if not transparent:
        draw.rounded_rectangle(
            layout["panel_rect"],
            radius=max(18, round(34 * scale)),
            fill=(15, 15, 15, 255),
        )

    # A visible first-frame brand mark prevents accidental pure-black bridge frames.
    mark_rect = layout["mark_rect"]
    mark_w = mark_rect[2] - mark_rect[0]
    draw.rounded_rectangle(mark_rect, radius=mark_w // 2, fill=accent)

    line_p = progress(time_s, 0.00, 0.42)
    line_y = int(layout["line_y"])
    draw.rounded_rectangle(
        (
            safe_left,
            line_y,
            safe_left + max(mark_w, round(int(layout["content_width"]) * 0.18 * line_p)),
            line_y + int(layout["line_height"]),
        ),
        radius=max(2, round(3 * scale)),
        fill=accent,
    )

    kicker_layout = layout["kicker"]
    if kicker_layout:
        kp = progress(time_s, 0.08, 0.38)
        draw_text_block(
            image,
            (kicker_layout["x"], kicker_layout["y"]),
            kicker_layout["lines"],
            kicker_layout["face"],
            accent,
            spacing=0,
            opacity=kp,
            slide_x=round((1 - kp) * kicker_layout["slide_x"]),
        )

    kind = str(spec.get("kind", "chapter"))
    title_layout = layout["title"]
    tp = progress(time_s, 0.22, 0.58)
    draw_text_block(
        image,
        (title_layout["x"], title_layout["y"]),
        title_layout["lines"],
        title_layout["face"],
        primary,
        spacing=title_layout["spacing"],
        opacity=tp,
        slide_x=round((1 - tp) * title_layout["slide_x"]),
    )

    body_layout = layout["body"]
    if body_layout:
        bp = progress(time_s, 0.65, 0.48)
        draw_text_block(
            image,
            (body_layout["x"], body_layout["y"]),
            body_layout["lines"],
            body_layout["face"],
            secondary,
            spacing=body_layout["spacing"],
            opacity=bp,
            slide_x=round((1 - bp) * body_layout["slide_x"]),
        )

    items_layout = layout["items"]
    if items_layout:
        if items_layout["mode"] == "compare":
            for index, entry in enumerate(items_layout["entries"]):
                ip = progress(time_s, 0.82 + index * 0.18, 0.45)
                y_offset = round((1 - ip) * items_layout["motion_y"])
                x0 = entry["x"]
                y0 = entry["y"] + y_offset
                layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
                ld = ImageDraw.Draw(layer)
                ld.rounded_rectangle(
                    (x0, y0, x0 + entry["width"], y0 + entry["height"]),
                    radius=max(12, round(22 * scale)),
                    fill=(*panel[:3], round(panel[3] * ip)),
                    outline=(*accent[:3], round(180 * ip)),
                    width=max(2, round(3 * scale)),
                )
                image.alpha_composite(layer)
                draw_text_block(
                    image,
                    (entry["text_x"], entry["text_y"] + y_offset),
                    entry["lines"],
                    items_layout["face"],
                    primary,
                    spacing=items_layout["spacing"],
                    opacity=ip,
                )
        else:
            bullet_size = items_layout["bullet_size"]
            bullet_face = font(bold_path, max(17, round(24 * scale)))
            for index, entry in enumerate(items_layout["entries"]):
                ip = progress(time_s, 0.78 + index * 0.16, 0.40)
                bullet = str(index + 1) if kind == "process" else "•"
                bullet_y = entry["bullet_y"]
                layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
                ld = ImageDraw.Draw(layer)
                ld.rounded_rectangle(
                    (safe_left, bullet_y, safe_left + bullet_size, bullet_y + bullet_size),
                    radius=max(8, round(13 * scale)),
                    fill=(*accent[:3], round(255 * ip)),
                )
                box = ld.textbbox((0, 0), bullet, font=bullet_face)
                bx = safe_left + bullet_size // 2 - (box[2] - box[0]) // 2 - box[0]
                by = bullet_y + bullet_size // 2 - (box[3] - box[1]) // 2 - box[1]
                ld.text((bx, by), bullet, font=bullet_face, fill=(*background[:3], round(255 * ip)))
                image.alpha_composite(layer)
                draw_text_block(
                    image,
                    (safe_left + items_layout["indent"], entry["text_y"]),
                    entry["lines"],
                    items_layout["face"],
                    primary,
                    spacing=items_layout["spacing"],
                    opacity=ip,
                    slide_x=round((1 - ip) * items_layout["text_slide"]),
                )

    cta_layout = layout["cta"]
    if cta_layout:
        cp = progress(time_s, max(0.9, float(spec["duration_s"]) - 1.5), 0.45)
        if cp > 0:
            layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            ld.rounded_rectangle(
                (
                    cta_layout["x"],
                    cta_layout["y"],
                    cta_layout["x"] + round(cta_layout["width"] * cp),
                    cta_layout["y"] + cta_layout["height"],
                ),
                radius=cta_layout["height"] // 2,
                fill=(*accent[:3], round(255 * cp)),
            )
            if cp > 0.82:
                ld.text(
                    (cta_layout["text_x"], cta_layout["text_y"]),
                    cta_layout["text"],
                    font=cta_layout["face"],
                    fill=(*background[:3], round(255 * ((cp - 0.82) / 0.18))),
                    anchor="lt",
                )
            image.alpha_composite(layer)

    return image


def validate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise CardError("spec must be a JSON object")
    kind = str(spec.get("kind") or "")
    if kind not in KINDS:
        raise CardError(f"kind must be one of {sorted(KINDS)}")
    normalized = dict(spec)
    normalized.setdefault("width", 1920)
    normalized.setdefault("height", 1080)
    normalized.setdefault("fps", 30)
    normalized.setdefault("duration_s", 4.0)
    title = str(normalized.get("title") or "").strip()
    if not title:
        raise CardError("title is required")
    normalized["title"] = title
    for field in ("kicker", "body", "cta"):
        if field in normalized and normalized[field] is not None:
            normalized[field] = str(normalized[field]).strip()
    normalized["width"] = int(normalized["width"])
    normalized["height"] = int(normalized["height"])
    normalized["fps"] = float(normalized["fps"])
    normalized["duration_s"] = float(normalized["duration_s"])
    if normalized["width"] < 320 or normalized["height"] < 320:
        raise CardError("minimum dimensions are 320x320")
    if not 20 <= normalized["fps"] <= 60:
        raise CardError("fps must be 20..60")
    if not 1.0 <= normalized["duration_s"] <= 30:
        raise CardError("duration must be 1..30 seconds")
    raw_items = normalized.get("items")
    if raw_items is None:
        items: list[str] = []
    elif not isinstance(raw_items, list):
        raise CardError("items must be a JSON array")
    else:
        items = []
        for index, value in enumerate(raw_items):
            item = str(value).strip()
            if not item:
                raise CardError(f"items[{index}] must not be blank")
            items.append(item)
    if len(items) > 5:
        raise CardError("at most 5 items are supported; refusing to truncate the item list")
    if kind == "compare" and len(items) != 2:
        raise CardError("compare cards require exactly 2 items")
    if kind == "process" and not 2 <= len(items) <= 5:
        raise CardError("process cards require 2..5 items")
    normalized["items"] = items
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a data-driven SPRUT motion card after semantic approval",
        epilog=(
            "example: python render_motion_card.py --edit-dir /project/edit "
            "--visual-id visual_chapter_2 "
            "/project/edit/animations/chapter.json -o /project/edit/animations/chapter.mp4 "
            "--poster /project/edit/animations/chapter.png"
        ),
    )
    parser.add_argument("--edit-dir", type=Path, required=True)
    parser.add_argument(
        "--visual-id",
        required=True,
        help="exact semantic_plan.visual_plan ID whose approved words this card renders",
    )
    parser.add_argument("spec", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--transparent", action="store_true", help="render ProRes 4444 alpha MOV")
    parser.add_argument("--poster", type=Path, help="also save a representative PNG")
    parser.add_argument("--font-regular")
    parser.add_argument("--font-bold")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    edit_dir = canonical_edit_dir(args.edit_dir)
    spec_path = path_under_edit(edit_dir, args.spec, "spec")
    output = path_under_edit(edit_dir, args.output, "output")
    poster = path_under_edit(edit_dir, args.poster, "poster") if args.poster else None
    provenance = path_under_edit(
        edit_dir, provenance_path_for(output), "motion-card provenance sidecar"
    )
    require_asset_gate(edit_dir)

    raw_spec, spec_snapshot = load_json_object_snapshot(spec_path, "motion-card spec")
    spec = validate_spec(raw_spec)
    contract = load_approved_visual_contract(edit_dir, args.visual_id, spec)
    generator_snapshot, helper_snapshot = capture_generator_snapshots(Path(__file__))
    control_snapshots = [
        spec_snapshot,
        contract.plan_snapshot,
        contract.approval_snapshot,
        generator_snapshot,
        helper_snapshot,
    ]
    regular = find_font(False, args.font_regular)
    bold = find_font(True, args.font_bold)
    load_pillow()
    if shutil.which("ffmpeg") is None:
        raise CardError("ffmpeg is required")
    layout = preflight_layout(spec, (regular, bold))
    fps = float(spec["fps"])
    frames = max(1, math.ceil(float(spec["duration_s"]) * fps))
    if output == spec_path or (poster and (output == poster or poster == spec_path)):
        raise CardError("output, poster, and input spec must use distinct paths")
    if output.exists() and not args.force:
        raise CardError(f"output exists; use --force to replace: {output}")
    if poster and poster.exists() and not args.force:
        raise CardError(f"poster exists; use --force to replace: {poster}")
    if provenance.exists() and not args.force:
        raise CardError(f"provenance sidecar exists; use --force to replace: {provenance}")
    if args.transparent and output.suffix.lower() != ".mov":
        raise CardError("transparent output must use .mov")
    if not args.transparent and output.suffix.lower() != ".mp4":
        raise CardError("full-frame output must use .mp4")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="sprut-card-", dir=str(output.parent)) as temp:
        frame_dir = Path(temp)
        rendered_path = frame_dir / ("render.mov" if args.transparent else "render.mp4")
        poster_temporary = frame_dir / "poster.png" if poster else None
        poster_index = round(frames * 0.72)
        for index in range(frames):
            image = render_frame(spec, index, args.transparent, (regular, bold), layout)
            image.save(frame_dir / f"frame_{index:05d}.png")
            if poster_temporary and index == min(frames - 1, poster_index):
                image.save(poster_temporary)
        command = [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-framerate", str(fps), "-i", str(frame_dir / "frame_%05d.png"),
        ]
        if args.transparent:
            command += ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"]
        else:
            command += ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        command.append(str(rendered_path))
        result = subprocess.run(command, check=False)
        if result.returncode:
            raise CardError(f"ffmpeg render failed with exit {result.returncode}")
        assert_snapshots_current(control_snapshots)
        if provenance.exists():
            invalidate_provenance(provenance, "motion-card output replacement started")
        rendered_path.replace(output)
        if poster and poster_temporary:
            poster.parent.mkdir(parents=True, exist_ok=True)
            poster_temporary.replace(poster)
        payload = build_motion_card_provenance(
            edit_dir=edit_dir,
            output=output,
            spec_snapshot=spec_snapshot,
            contract=contract,
            generator_snapshot=generator_snapshot,
            helper_snapshot=helper_snapshot,
            poster=poster,
        )
        atomic_write_json(provenance, payload)
        try:
            verify_visual_asset_provenance(
                edit_dir,
                provenance,
                asset_path=output,
            )
        except (OSError, VisualProvenanceError) as exc:
            invalidate_provenance(provenance, f"post-write verification failed: {exc}")
            raise
    print(f"rendered: {output} | {frames} frames | {spec['width']}x{spec['height']}@{fps:g}")
    print(f"provenance: {provenance}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        AssetGateError,
        CardError,
        OSError,
        VisualProvenanceError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        print(f"render_motion_card: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
