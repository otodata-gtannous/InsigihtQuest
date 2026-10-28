"""Derive the light, transparent logo used on the dark theme from the original black-on-white artwork."""

from pathlib import Path

from PIL import Image

ASSETS = Path(__file__).resolve().parents[1] / "assets"
LIGHT = (230, 237, 243)  # the theme text colour


def main() -> None:
    gray = Image.open(ASSETS / "insightquest-logo.png").convert("L")
    # Dark ink becomes opaque, the near-white paper becomes fully transparent (with a little noise floor).
    alpha = gray.point(lambda v: 0 if v > 235 else min(255, int((235 - v) * 255 / 200)))
    logo = Image.new("RGBA", gray.size, LIGHT + (0,))
    logo.putalpha(alpha)
    logo.crop(logo.getbbox()).save(ASSETS / "insightquest-logo-dark.png", optimize=True)


if __name__ == "__main__":
    main()
