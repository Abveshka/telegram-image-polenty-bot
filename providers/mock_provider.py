import io
import textwrap
from PIL import Image, ImageDraw, ImageFont

from providers.base import ImageProvider


class MockImageProvider(ImageProvider):
    """
    Заглушка-провайдер для бесплатной разработки и тестирования.

    Не обращается ни к каким внешним API. Рисует картинку локально
    средствами PIL с текстом промпта поверх — чтобы визуально видеть,
    что промпт действительно дошёл от пользователя до провайдера.
    """

    async def generate(self, prompt: str) -> bytes:
        width, height = 512, 512
        image = Image.new("RGB", (width, height), color=(40, 40, 60))
        draw = ImageDraw.Draw(image)

        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

        wrapped_text = textwrap.fill(prompt, width=30)
        draw.multiline_text(
            (20, 20),
            f"MOCK IMAGE\n\nPrompt:\n{wrapped_text}",
            fill=(255, 255, 255),
            font=font,
            spacing=6,
        )

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()