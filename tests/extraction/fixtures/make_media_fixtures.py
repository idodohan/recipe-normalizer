"""Generate the committed PDF/image fixtures for the PDF + image extractors.

Run from the repo root to (re)create the fixtures used by test_pdf_plugin.py,
test_image_plugin.py, and the golden corpus:

    uv run python tests/extraction/fixtures/make_media_fixtures.py

Outputs (deterministic for a given Pillow version):
- pdf/text_layer_recipe.pdf  — a 1-page PDF WITH a selectable text layer
- pdf/scanned_recipe.pdf      — an image-only "scanned" PDF (no text layer)
- images/recipe_photo.jpg     — a stand-in dish photo
- images/handwritten_card.png — a stand-in handwritten recipe card
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw

FIXTURES = Path(__file__).parent
RECIPE_LINES = [
    "Grandmother's Banana Bread",
    "Ingredients:",
    "2 cups all-purpose flour",
    "1 cup mashed ripe banana",
    "0.5 cup melted butter",
    "2 eggs, lightly beaten",
    "1 teaspoon baking soda",
    "Method:",
    "Preheat oven to 175C. Mix the dry ingredients, fold in the wet,",
    "pour into a loaf pan and bake for 55 minutes until golden.",
]


def _text_layer_pdf(lines: list[str]) -> bytes:
    """Hand-build a minimal PDF whose content stream draws selectable text."""
    ops = ["BT", "/F1 12 Tf", "16 TL", "72 740 Td"]
    for line in lines:
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        ops.append(f"({escaped}) Tj")
        ops.append("T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % index + body + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objects) + 1))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
        % (len(objects) + 1, xref_pos)
    )
    return out.getvalue()


def _scanned_pdf(lines: list[str]) -> bytes:
    """An image-only PDF (Pillow rasterizes — no text layer survives)."""
    image = Image.new("RGB", (850, 1100), "white")
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(lines):
        draw.text((60, 60 + row * 40), line, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=100.0)
    return buffer.getvalue()


def _recipe_photo() -> bytes:
    image = Image.new("RGB", (1200, 800), (210, 180, 140))
    draw = ImageDraw.Draw(image)
    draw.ellipse((400, 250, 800, 550), fill=(180, 120, 70))
    draw.text((60, 60), "banana bread on a plate", fill=(60, 40, 20))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def _handwritten_card() -> bytes:
    image = Image.new("RGB", (900, 1200), (250, 245, 230))
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(RECIPE_LINES):
        draw.text((50, 80 + row * 60), line, fill=(40, 40, 90))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def main() -> None:
    (FIXTURES / "pdf").mkdir(parents=True, exist_ok=True)
    (FIXTURES / "images").mkdir(parents=True, exist_ok=True)
    (FIXTURES / "pdf" / "text_layer_recipe.pdf").write_bytes(_text_layer_pdf(RECIPE_LINES))
    (FIXTURES / "pdf" / "scanned_recipe.pdf").write_bytes(_scanned_pdf(RECIPE_LINES))
    (FIXTURES / "images" / "recipe_photo.jpg").write_bytes(_recipe_photo())
    (FIXTURES / "images" / "handwritten_card.png").write_bytes(_handwritten_card())
    print("wrote PDF + image fixtures under", FIXTURES)


if __name__ == "__main__":
    main()
