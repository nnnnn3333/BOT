"""Render the heading from the current section name and stable section ID."""
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont
from aiogram.types import BufferedInputFile


@dataclass(frozen=True)
class SectionHeader:
    id: int
    name: str


PALETTES = (
    ("#111f32", "#72b7ff"), ("#112c32", "#6ce3c3"),
    ("#30223b", "#d5a8ff"), ("#34272a", "#ffc187"),
    ("#172d43", "#8be6ff"), ("#203025", "#c6e88c"),
    ("#302537", "#ffabcd"),
)


def _font(size: int, bold: bool = False):
    for family in (("DejaVuSans-Bold.ttf", "segoeuib.ttf") if bold else ("DejaVuSans.ttf", "segoeui.ttf")):
        try:
            return ImageFont.truetype(family, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


@lru_cache(maxsize=64)
def _jpeg(header: SectionHeader) -> bytes:
    background, accent = PALETTES[header.id % len(PALETTES)]
    image = Image.new("RGB", (1040, 268), background)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((46, 45, 57, 222), radius=5, fill=accent)
    draw.text((83, 51), "WORKBOT   /   " + str(header.id).zfill(2),
              font=_font(26, bold=True), fill=accent)
    title_font = _font(62, bold=True)
    title = header.name
    while len(title) > 1 and draw.textlength(title, font=title_font) > 720:
        title = title.rstrip("…")[:-1] + "…"
    draw.text((82, 112), title, font=title_font, fill="#F5F7FC")
    draw.rounded_rectangle((866, 109, 989, 201), radius=14, fill=accent)
    draw.rounded_rectangle((866, 91, 924, 142), radius=10, fill=accent)
    draw.line((891, 149, 961, 149), fill=background, width=4)
    draw.line((891, 168, 942, 168), fill=background, width=4)
    output = BytesIO()
    image.save(output, format="JPEG", quality=88)
    return output.getvalue()


def header_upload(header: SectionHeader) -> BufferedInputFile:
    return BufferedInputFile(_jpeg(header), filename=f"section-{header.id}.jpg")
