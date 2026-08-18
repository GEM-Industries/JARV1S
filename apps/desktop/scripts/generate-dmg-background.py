# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10", "numpy>=1.26"]
# ///
"""Generate the DMG installer background (background.png + background@2x.png).

Layout matches the create-dmg invocation in release-macos.sh:
660x400 window, app icon centered at (165,175), /Applications drop link
at (495,175). Colors are the JARV1S OKLCH theme tokens from
frontend/src/index.css, styled after HolographicBorder/TacticalButton.

Run: uv run apps/desktop/scripts/generate-dmg-background.py
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# Logical canvas (points) and supersampling factor. Output is 2x (retina)
# plus a downscaled 1x.
W, H = 660, 400
SS = 4  # render at 4x logical, downsample

OUT_DIR = Path(__file__).resolve().parent.parent / "src-tauri" / "dmg"


def oklch_to_srgb(l: float, c: float, h_deg: float) -> tuple[int, int, int]:
    """OKLCH -> sRGB 8-bit, matching CSS oklch()."""
    h = math.radians(h_deg)
    a, b = c * math.cos(h), c * math.sin(h)
    l_ = l + 0.3963377774 * a + 0.2158037573 * b
    m_ = l - 0.1055613458 * a - 0.0638541728 * b
    s_ = l - 0.0894841775 * a - 1.2914855480 * b
    lms = (l_**3, m_**3, s_**3)
    r = 4.0767416621 * lms[0] - 3.3077115913 * lms[1] + 0.2309699292 * lms[2]
    g = -1.2684380046 * lms[0] + 2.6097574011 * lms[1] - 0.3413193965 * lms[2]
    bl = -0.0041960863 * lms[0] - 0.7034186147 * lms[1] + 1.7076147010 * lms[2]

    def encode(x: float) -> int:
        x = max(0.0, min(1.0, x))
        x = 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055
        return round(max(0.0, min(1.0, x)) * 255)

    return encode(r), encode(g), encode(bl)


# Theme tokens (frontend/src/index.css)
RICH_BLACK_DEEP = oklch_to_srgb(0.1417, 0.0164, 248.93)
RICH_BLACK_MEDIUM = oklch_to_srgb(0.2036, 0.0304, 247.65)
BLUE_GREEN = oklch_to_srgb(0.6373, 0.1122, 224.66)
CELADON = oklch_to_srgb(0.8686, 0.1066, 150.22)
WHITE_SECONDARY = oklch_to_srgb(0.9203, 0.011, 234.83)

# Icon centers in the create-dmg layout.
APP_POS = (165, 175)
DROP_POS = (495, 175)

# Finder always draws black labels; bake light pills behind them.
LABEL_Y = 258.5
LABEL_CHIPS = (
    (APP_POS[0], 96, BLUE_GREEN),  # "JARV1S"
    (DROP_POS[0], 144, CELADON),  # "Applications"
)


def radial_background(w: int, h: int) -> Image.Image:
    """rich-black-medium center fading to rich-black-deep at the edges."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w / 2, h * 0.42
    dist = np.sqrt(((xx - cx) / (w * 0.62)) ** 2 + ((yy - cy) / (h * 0.85)) ** 2)
    t = np.clip(dist, 0.0, 1.0) ** 1.4
    med = np.array(RICH_BLACK_MEDIUM, dtype=np.float32)
    deep = np.array(RICH_BLACK_DEEP, dtype=np.float32)
    rgb = med[None, None, :] * (1 - t[..., None]) + deep[None, None, :] * t[..., None]
    return Image.fromarray(rgb.astype(np.uint8), "RGB").convert("RGBA")


def glow_layers(mask: Image.Image, color: tuple[int, int, int],
                core_alpha: int, glow_alpha: int, blur: float) -> Image.Image:
    """Stroke mask -> holographic layer: blurred glow underneath a crisp core."""
    layer = Image.new("RGBA", mask.size, (0, 0, 0, 0))
    glow_mask = mask.filter(ImageFilter.GaussianBlur(blur)).point(
        lambda a: min(255, int(a * glow_alpha / 255))
    )
    layer.paste(Image.new("RGBA", mask.size, (*color, 255)), (0, 0), glow_mask)
    core_mask = mask.point(lambda a: min(255, int(a * core_alpha / 255)))
    layer.paste(Image.new("RGBA", mask.size, (*color, 255)), (0, 0), core_mask)
    return layer


def gradient_colorize(mask: Image.Image, x0: float, x1: float) -> Image.Image:
    """Colorize a stroke mask with a horizontal blue-green -> celadon gradient."""
    w, h = mask.size
    xx = np.mgrid[0:h, 0:w][1].astype(np.float32)
    t = np.clip((xx - x0) / max(1.0, x1 - x0), 0.0, 1.0)
    bg = np.array(BLUE_GREEN, dtype=np.float32)
    cel = np.array(CELADON, dtype=np.float32)
    rgb = bg[None, None, :] * (1 - t[..., None]) + cel[None, None, :] * t[..., None]
    out = np.dstack([rgb.astype(np.uint8), np.array(mask, dtype=np.uint8)])
    return Image.fromarray(out, "RGBA")


def bracket_border_mask(w: int, h: int, s: int) -> Image.Image:
    """Left/right holographic brackets like HolographicBorder: rounded-rect arcs
    hugging the sides, with gaps centered on the top and bottom edges."""
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    inset, radius, stroke = 16 * s, 18 * s, int(1.25 * s)
    box = (inset, inset, w - inset, h - inset)
    # Arc sweep: each bracket covers its rounded corners plus a stretch of the
    # vertical edge; drawn as two corner arcs joined by a straight segment.
    ext = 56 * s  # how far the bracket extends along top/bottom from each corner
    x0, y0, x1, y1 = box
    for side in ("left", "right"):
        if side == "left":
            d.arc((x0, y0, x0 + 2 * radius, y0 + 2 * radius), 180, 270, fill=255, width=stroke)
            d.arc((x0, y1 - 2 * radius, x0 + 2 * radius, y1), 90, 180, fill=255, width=stroke)
            d.line((x0, y0 + radius, x0, y1 - radius), fill=255, width=stroke)
            d.line((x0 + radius, y0, x0 + radius + ext, y0), fill=255, width=stroke)
            d.line((x0 + radius, y1, x0 + radius + ext, y1), fill=255, width=stroke)
        else:
            d.arc((x1 - 2 * radius, y0, x1, y0 + 2 * radius), 270, 360, fill=255, width=stroke)
            d.arc((x1 - 2 * radius, y1 - 2 * radius, x1, y1), 0, 90, fill=255, width=stroke)
            d.line((x1, y0 + radius, x1, y1 - radius), fill=255, width=stroke)
            d.line((x1 - radius - ext, y0, x1 - radius, y0), fill=255, width=stroke)
            d.line((x1 - radius - ext, y1, x1 - radius, y1), fill=255, width=stroke)
    return mask


def faint_outline_mask(w: int, h: int, s: int) -> Image.Image:
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    inset, radius = 16 * s, 18 * s
    d.rounded_rectangle((inset, inset, w - inset, h - inset),
                        radius=radius, outline=255, width=max(1, s))
    return mask


def arrow_mask(w: int, h: int, s: int) -> Image.Image:
    """Shaft with open chevron head plus two trailing chevrons, holographic weight."""
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    y = APP_POS[1] * s
    x_start, x_end = 252 * s, 408 * s
    stroke = int(2.25 * s)
    head = 13 * s
    d.line((x_start + 26 * s, y, x_end - head * 0.35, y), fill=255, width=stroke)
    d.line((x_end - head, y - head, x_end, y), fill=255, width=stroke)
    d.line((x_end - head, y + head, x_end, y), fill=255, width=stroke)
    for i, alpha in ((0, 255), (1, 150)):
        cx = x_start + i * 12 * s
        col = alpha
        d.line((cx, y - 8 * s, cx + 8 * s, y), fill=col, width=int(1.5 * s))
        d.line((cx, y + 8 * s, cx + 8 * s, y), fill=col, width=int(1.5 * s))
    return mask


def label_chips(img: Image.Image, s: int) -> None:
    """Pale holographic pills behind the Finder label positions so the
    system-drawn black label text stays readable on the dark canvas."""
    w, h = img.size
    chip_h = 24 * s
    for cx, chip_w, tint in LABEL_CHIPS:
        fill = tuple(
            round(0.82 * wc + 0.18 * tc) for wc, tc in zip(WHITE_SECONDARY, tint)
        )
        box = (
            cx * s - chip_w * s // 2,
            int(LABEL_Y * s - chip_h / 2),
            cx * s + chip_w * s // 2,
            int(LABEL_Y * s + chip_h / 2),
        )
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(box, radius=chip_h // 2, fill=255)
        # Soft tinted glow around the pill, then the solid light fill.
        glow = mask.filter(ImageFilter.GaussianBlur(5 * s)).point(
            lambda a: min(255, int(a * 0.45))
        )
        img.paste(Image.new("RGBA", (w, h), (*tint, 255)), (0, 0), glow)
        core = mask.point(lambda a: min(255, int(a * 0.94)))
        img.paste(Image.new("RGBA", (w, h), (*fill, 255)), (0, 0), core)


def compose() -> Image.Image:
    w, h = W * SS, H * SS
    img = radial_background(w, h)

    # Holographic side brackets (brand primary, ~45% core alpha + soft glow)
    brackets = bracket_border_mask(w, h, SS)
    img.alpha_composite(glow_layers(brackets, BLUE_GREEN, core_alpha=115, glow_alpha=60, blur=3.5 * SS))
    outline = faint_outline_mask(w, h, SS)
    img.alpha_composite(glow_layers(outline, BLUE_GREEN, core_alpha=32, glow_alpha=0, blur=0))

    # Holographic arrow with the icon's blue-green -> celadon gradient
    arrow = arrow_mask(w, h, SS)
    glow = arrow.filter(ImageFilter.GaussianBlur(4 * SS)).point(lambda a: min(255, int(a * 0.55)))
    img.alpha_composite(gradient_colorize(glow, 252 * SS, 408 * SS))
    core = arrow.point(lambda a: min(255, int(a * 0.85)))
    img.alpha_composite(gradient_colorize(core, 252 * SS, 408 * SS))

    label_chips(img, SS)
    return img


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    full = compose()
    two_x = full.resize((W * 2, H * 2), Image.LANCZOS).convert("RGB")
    one_x = full.resize((W, H), Image.LANCZOS).convert("RGB")
    two_x.save(OUT_DIR / "background@2x.png", dpi=(144, 144))
    one_x.save(OUT_DIR / "background.png", dpi=(72, 72))
    print(f"Wrote {OUT_DIR / 'background.png'} and background@2x.png")


if __name__ == "__main__":
    main()
