from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from pathlib import Path
import time


class TextImageRenderer:
    def __init__(
        self,
        font_size: int = 16,
        padding_x: int = 30,
        padding_y: int = 30,
        line_spacing_extra: int = 6,
        bg=(250, 250, 248),
        fg=(28, 28, 30),
    ):
        self.font_size = font_size
        self.padding_x = padding_x
        self.padding_y = padding_y
        self.line_spacing_extra = line_spacing_extra
        self.bg = bg
        self.fg = fg
        self.font = self._load_mono(font_size)

    def _load_mono(self, size: int):
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
            "/usr/share/fonts/truetype/ubuntu/UbuntuMono-R.ttf",
            "/System/Library/Fonts/Menlo.ttc",  # macOS Menlo
            "/Library/Fonts/Courier New.ttf",   # Windows
        ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def render_to_bytes(self, text: str, image_format: str = "png") -> bytes:
        """Render text into an image and return raw bytes."""
        t1 = time.time()
        lines = text.splitlines()

        # Measure line height and max width
        line_height = (self.font.getbbox("Mg")[3] - self.font.getbbox("Mg")[1]) + self.line_spacing_extra
        tmp = Image.new("RGB", (1, 1))
        draw = ImageDraw.Draw(tmp)
        max_w = max((int(draw.textlength(ln, font=self.font)) for ln in lines), default=0)

        # Compute dimensions
        img_w = max(1000, max_w + 2 * self.padding_x)
        img_h = 2 * self.padding_y + len(lines) * line_height

        # Render
        img = Image.new("RGB", (img_w, img_h), self.bg)
        d = ImageDraw.Draw(img)
        y = self.padding_y
        for ln in lines:
            d.text((self.padding_x, y), ln, font=self.font, fill=self.fg)
            y += line_height

        # Encode to memory
        buf = BytesIO()
        img.save(buf, format=image_format.upper())
        t2 = time.time()
        print(f"Rendered {len(lines)} lines in {t2 - t1:.2f} seconds")
        return buf.getvalue()

    def render_to_file(self, text: str, out_path: str = "distilled_page_long.png"):
        """Render text into an image and save to file."""
        img_bytes = self.render_to_bytes(text, image_format=out_path.split(".")[-1])
        Path(out_path).write_bytes(img_bytes)
        print(f"Saved: {out_path}")


# Example usage:
if __name__ == "__main__":
    with open("distilled_text.txt", "r", encoding="utf-8") as f:
        text = f.read()

    renderer = TextImageRenderer()
    # Save to file
    renderer.render_to_file(text, "distilled_page_long.png")

    # Or get raw bytes for Gemini
    img_bytes = renderer.render_to_bytes(text, image_format="png")
    # Pass `img_bytes` directly to Gemini:
    # types.Part.from_bytes(data=img_bytes, mime_type="image/png")
