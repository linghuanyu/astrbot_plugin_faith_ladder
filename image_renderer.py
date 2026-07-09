"""
Image renderer service for faith ladder plugin.
Uses PIL/Pillow for local image rendering — no remote t2i service needed.
"""

import io
import os
import platform
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from astrbot_plugin_faith_ladder.models import Player


# --- Font Discovery ---

def _find_cjk_font() -> Optional[str]:
    """Find a CJK-capable font on the system."""
    candidates = []

    system = platform.system()
    if system == "Windows":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        fonts_dir = os.path.join(windir, "Fonts")
        candidates = [
            os.path.join(fonts_dir, "msyh.ttc"),       # Microsoft YaHei
            os.path.join(fonts_dir, "msyhbd.ttc"),      # Microsoft YaHei Bold
            os.path.join(fonts_dir, "simhei.ttf"),      # SimHei (黑体)
            os.path.join(fonts_dir, "simsun.ttc"),      # SimSun (宋体)
        ]
    elif system == "Darwin":  # macOS
        candidates = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    else:  # Linux
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        ]

    # Also check for bundled fonts in the fonts/ directory
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    fonts_dir = os.path.join(plugin_dir, "fonts")
    if os.path.isdir(fonts_dir):
        for fname in os.listdir(fonts_dir):
            if fname.lower().endswith(('.ttf', '.ttc', '.otf')):
                candidates.insert(0, os.path.join(fonts_dir, fname))

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


# Cache the font path
_FONT_PATH = _find_cjk_font()


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Load a font at the given size, falling back to default if needed."""
    if _FONT_PATH:
        try:
            return ImageFont.truetype(_FONT_PATH, size)
        except Exception:
            pass
    return ImageFont.load_default()


# Font cache: size -> Font object
_font_cache: dict = {}


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    """Get a cached font instance for the given size."""
    if size not in _font_cache:
        _font_cache[size] = _load_font(size)
    return _font_cache[size]


# --- Color Palette ---

class Colors:
    """Color palette for leaderboard images."""
    BG_TOP = (18, 18, 40)
    BG_BOTTOM = (15, 30, 60)
    ROW_BG = (38, 38, 68)
    ROW_BORDER_GOLD = (255, 215, 0)
    ROW_BORDER_SILVER = (192, 192, 192)
    ROW_BORDER_BRONZE = (205, 127, 50)
    ROW_BORDER_NORMAL = (74, 111, 165)
    TEXT_WHITE = (255, 255, 255)
    TEXT_DIM = (170, 170, 170)
    TEXT_FOOTER = (100, 100, 100)
    RANK_GOLD = (255, 215, 0)
    RANK_SILVER = (192, 192, 192)
    RANK_BRONZE = (205, 127, 50)
    RANK_NORMAL = (136, 136, 136)
    SCORE_LADDER = (79, 195, 247)      # Light blue
    SCORE_PILGRIMAGE = (206, 147, 216) # Light purple
    BADGE_CLASS_BG = (100, 150, 255, 60)
    BADGE_CLASS_BORDER = (100, 150, 255, 100)
    BADGE_FAITH_BG = (200, 100, 255, 60)
    BADGE_FAITH_BORDER = (200, 100, 255, 100)
    BADGE_TEXT_CLASS = (138, 180, 248)
    BADGE_TEXT_FAITH = (212, 165, 255)
    HEADER_LADDER = (255, 215, 0)
    HEADER_PILGRIMAGE = (206, 147, 216)


# --- Layout Constants ---

IMG_WIDTH = 800
PADDING = 30
ROW_HEIGHT = 80
ROW_SPACING = 10
HEADER_HEIGHT = 80
FOOTER_HEIGHT = 40
RANK_WIDTH = 60
BADGE_HEIGHT = 24
BADGE_PADDING_X = 10
BADGE_SPACING = 8


class ImageRenderer:
    """Renders leaderboard data to images using PIL/Pillow (local, no remote service)."""

    def __init__(self, plugin_instance=None):
        self.plugin = plugin_instance
        self._font_cache: dict = {}

    def _draw_gradient_bg(self, draw: ImageDraw.Draw, width: int, height: int):
        """Draw a vertical gradient background."""
        for y in range(height):
            ratio = y / max(height - 1, 1)
            r = int(Colors.BG_TOP[0] + (Colors.BG_BOTTOM[0] - Colors.BG_TOP[0]) * ratio)
            g = int(Colors.BG_TOP[1] + (Colors.BG_BOTTOM[1] - Colors.BG_TOP[1]) * ratio)
            b = int(Colors.BG_TOP[2] + (Colors.BG_BOTTOM[2] - Colors.BG_TOP[2]) * ratio)
            draw.line([(0, y), (width, y)], fill=(r, g, b))

    def _draw_rounded_rect(self, draw: ImageDraw.Draw, xy, radius, fill=None, outline=None, width=1):
        """Draw a rounded rectangle."""
        x1, y1, x2, y2 = xy
        radius = min(radius, (x2 - x1) // 2, (y2 - y1) // 2)
        if fill:
            # Fill main body
            draw.rectangle((x1 + radius, y1, x2 - radius, y2), fill=fill)
            draw.rectangle((x1, y1 + radius, x2, y2 - radius), fill=fill)
            # Fill corners
            draw.pieslice((x1, y1, x1 + 2 * radius, y1 + 2 * radius), 180, 270, fill=fill)
            draw.pieslice((x2 - 2 * radius, y1, x2, y1 + 2 * radius), 270, 360, fill=fill)
            draw.pieslice((x1, y2 - 2 * radius, x1 + 2 * radius, y2), 90, 180, fill=fill)
            draw.pieslice((x2 - 2 * radius, y2 - 2 * radius, x2, y2), 0, 90, fill=fill)
        if outline and width > 0:
            draw.arc((x1, y1, x1 + 2 * radius, y1 + 2 * radius), 180, 270, fill=outline, width=width)
            draw.arc((x2 - 2 * radius, y1, x2, y1 + 2 * radius), 270, 360, fill=outline, width=width)
            draw.arc((x1, y2 - 2 * radius, x1 + 2 * radius, y2), 90, 180, fill=outline, width=width)
            draw.arc((x2 - 2 * radius, y2 - 2 * radius, x2, y2), 0, 90, fill=outline, width=width)
            draw.line([(x1 + radius, y1), (x2 - radius, y1)], fill=outline, width=width)
            draw.line([(x1 + radius, y2), (x2 - radius, y2)], fill=outline, width=width)
            draw.line([(x1, y1 + radius), (x1, y2 - radius)], fill=outline, width=width)
            draw.line([(x2, y1 + radius), (x2, y2 - radius)], fill=outline, width=width)

    def _draw_badge(self, draw: ImageDraw.Draw, x: int, y: int, text: str,
                    bg_color: tuple, border_color: tuple, text_color: tuple, font):
        """Draw a badge (rounded pill) with text."""
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        pill_w = tw + BADGE_PADDING_X * 2
        pill_h = BADGE_HEIGHT
        # Draw pill background
        self._draw_rounded_rect(draw, (x, y, x + pill_w, y + pill_h),
                                radius=pill_h // 2, fill=bg_color, outline=border_color, width=1)
        # Draw text centered vertically
        ty = y + (pill_h - th) // 2
        draw.text((x + BADGE_PADDING_X, ty), text, font=font, fill=text_color)
        return pill_w

    def _rank_color(self, rank: int) -> tuple:
        if rank == 1:
            return Colors.RANK_GOLD
        elif rank == 2:
            return Colors.RANK_SILVER
        elif rank == 3:
            return Colors.RANK_BRONZE
        return Colors.RANK_NORMAL

    def _border_color(self, rank: int) -> tuple:
        if rank == 1:
            return Colors.ROW_BORDER_GOLD
        elif rank == 2:
            return Colors.ROW_BORDER_SILVER
        elif rank == 3:
            return Colors.ROW_BORDER_BRONZE
        return Colors.ROW_BORDER_NORMAL

    def _draw_player_row(self, draw: ImageDraw.Draw, img: Image.Image,
                         player: dict, rank: int, y: int,
                         score_labels: List[Tuple[str, str, tuple]]):
        """Draw a single player row.

        Args:
            score_labels: List of (label, value_key, color) tuples for score display.
        """
        x_start = PADDING
        row_w = IMG_WIDTH - PADDING * 2

        # Row background with colored left border
        border_color = self._border_color(rank)
        # Draw the row background
        self._draw_rounded_rect(draw, (x_start, y, x_start + row_w, y + ROW_HEIGHT),
                                radius=8, fill=Colors.ROW_BG)
        # Draw left border accent
        draw.rectangle((x_start, y + 4, x_start + 4, y + ROW_HEIGHT - 4), fill=border_color)

        # Rank number
        rank_font = _get_font(28)
        rank_text = str(rank)
        rank_bbox = draw.textbbox((0, 0), rank_text, font=rank_font)
        rank_tw = rank_bbox[2] - rank_bbox[0]
        rank_x = x_start + (RANK_WIDTH - rank_tw) // 2
        rank_y = y + (ROW_HEIGHT - (rank_bbox[3] - rank_bbox[1])) // 2
        draw.text((rank_x, rank_y), rank_text, font=rank_font, fill=self._rank_color(rank))

        # Player info area
        info_x = x_start + RANK_WIDTH + 10
        name_font = _get_font(20)
        badge_font = _get_font(13)

        # Player name
        display_name = player.get("player_name", "?")
        if player.get("oathbreaker"):
            display_name += "(弃誓者)"
        draw.text((info_x, y + 10), display_name,
                  font=name_font, fill=Colors.TEXT_WHITE)

        # Badges
        badge_y = y + 42
        badge_x = info_x

        class_name = player.get("class_") or "未设定"
        badge_w = self._draw_badge(draw, badge_x, badge_y, class_name,
                                   Colors.BADGE_CLASS_BG, Colors.BADGE_CLASS_BORDER,
                                   Colors.BADGE_TEXT_CLASS, badge_font)
        badge_x += badge_w + BADGE_SPACING

        faith_name = player.get("faith") or "未设定"
        self._draw_badge(draw, badge_x, badge_y, faith_name,
                         Colors.BADGE_FAITH_BG, Colors.BADGE_FAITH_BORDER,
                         Colors.BADGE_TEXT_FAITH, badge_font)

        # Scores (right-aligned)
        score_font = _get_font(14)
        score_value_font = _get_font(17)
        score_x = x_start + row_w - 20
        score_y = y + 12

        for label, value_key, color in score_labels:
            value = player.get(value_key, 0)
            label_text = f"{label}: "
            value_text = str(value)

            label_bbox = draw.textbbox((0, 0), label_text, font=score_font)
            value_bbox = draw.textbbox((0, 0), value_text, font=score_value_font)
            label_w = label_bbox[2] - label_bbox[0]
            value_w = value_bbox[2] - value_bbox[0]
            total_w = label_w + value_w

            text_x = score_x - total_w
            draw.text((text_x, score_y), label_text, font=score_font, fill=Colors.TEXT_DIM)
            draw.text((text_x + label_w, score_y - 1), value_text, font=score_value_font, fill=color)
            score_y += 28

    async def render_leaderboard_image(
        self,
        players: List[Player],
        limit: int = 10,
        image_format: str = "PNG",
        quality: int = 90
    ) -> Optional[bytes]:
        """Render ladder leaderboard as image bytes."""
        return await self._render(
            players, limit, is_ladder=True,
            image_format=image_format, quality=quality
        )

    async def render_pilgrimage_image(
        self,
        players: List[Player],
        limit: int = 10,
        image_format: str = "PNG",
        quality: int = 90
    ) -> Optional[bytes]:
        """Render pilgrimage leaderboard as image bytes."""
        return await self._render(
            players, limit, is_ladder=False,
            image_format=image_format, quality=quality
        )

    async def _render(
        self,
        players: List[Player],
        limit: int,
        is_ladder: bool = True,
        image_format: str = "PNG",
        quality: int = 90
    ) -> Optional[bytes]:
        """Internal: render leaderboard to image bytes using PIL."""
        try:
            from dataclasses import asdict
            displayed = min(len(players), limit)
            player_dicts = [asdict(p) for p in players[:limit]]

            # Calculate image height
            total_height = (
                PADDING + HEADER_HEIGHT +
                displayed * (ROW_HEIGHT + ROW_SPACING) +
                FOOTER_HEIGHT + PADDING
            )
            if displayed == 0:
                total_height = PADDING + HEADER_HEIGHT + 120 + FOOTER_HEIGHT + PADDING

            # Create RGBA image (for transparency support) then convert
            img = Image.new("RGBA", (IMG_WIDTH, total_height), Colors.BG_TOP)
            draw = ImageDraw.Draw(img)

            # Gradient background
            self._draw_gradient_bg(draw, IMG_WIDTH, total_height)

            # Header
            header_font = _get_font(28)
            if is_ladder:
                header_text = "⚔ 登神之路 ⚔"
                header_color = Colors.HEADER_LADDER
            else:
                header_text = "🙏 觐见之梯 🙏"
                header_color = Colors.HEADER_PILGRIMAGE

            header_bbox = draw.textbbox((0, 0), header_text, font=header_font)
            header_tw = header_bbox[2] - header_bbox[0]
            header_x = (IMG_WIDTH - header_tw) // 2
            header_y = PADDING + (HEADER_HEIGHT - (header_bbox[3] - header_bbox[1])) // 2
            draw.text((header_x, header_y), header_text, font=header_font, fill=header_color)

            # Player rows
            if player_dicts:
                # Score labels depend on which leaderboard
                if is_ladder:
                    score_labels = [
                        ("登神之路", "ladder_score", Colors.SCORE_LADDER),
                        ("觐见之梯", "pilgrimage_score", Colors.SCORE_PILGRIMAGE),
                    ]
                else:
                    score_labels = [
                        ("觐见之梯", "pilgrimage_score", Colors.SCORE_PILGRIMAGE),
                        ("登神之路", "ladder_score", Colors.SCORE_LADDER),
                    ]

                row_y = PADDING + HEADER_HEIGHT
                for i, player in enumerate(player_dicts):
                    self._draw_player_row(draw, img, player, i + 1, row_y, score_labels)
                    row_y += ROW_HEIGHT + ROW_SPACING
            else:
                # Empty state
                empty_font = _get_font(18)
                empty_text = "暂无排名数据"
                empty_bbox = draw.textbbox((0, 0), empty_text, font=empty_font)
                empty_x = (IMG_WIDTH - (empty_bbox[2] - empty_bbox[0])) // 2
                empty_y = PADDING + HEADER_HEIGHT + 40
                draw.text((empty_x, empty_y), empty_text, font=empty_font, fill=Colors.TEXT_DIM)

            # Footer
            footer_font = _get_font(13)
            footer_text = f"--- 显示前 {displayed} 名 ---"
            footer_bbox = draw.textbbox((0, 0), footer_text, font=footer_font)
            footer_tw = footer_bbox[2] - footer_bbox[0]
            footer_x = (IMG_WIDTH - footer_tw) // 2
            footer_y = total_height - FOOTER_HEIGHT + (FOOTER_HEIGHT - (footer_bbox[3] - footer_bbox[1])) // 2
            draw.text((footer_x, footer_y), footer_text, font=footer_font, fill=Colors.TEXT_FOOTER)

            # Convert to image bytes
            output = io.BytesIO()
            fmt = image_format.upper()
            if fmt in ("JPEG", "JPG"):
                img.convert("RGB").save(output, format="JPEG", quality=quality, optimize=True)
            else:
                img.convert("RGB").save(output, format="PNG", optimize=True)
            return output.getvalue()

        except Exception as e:
            logger.error(f"Failed to render leaderboard image: {e}")
            return None
