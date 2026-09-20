"""
Цвета моделей и фонов — из картинок самих вещей.

Ни один эндпоинт gift-satellite цветов не отдаёт: в справочниках лежат только
имя и редкость. Цвет модели — свойство картинки, а не прайс-API.

Зато картинка есть у каждой вещи, и она уже размечена: листинг отдал и slug, и
modelName, и backdropName. То есть мы заранее знаем, что на ней изображено, и
остаётся только разделить два слоя:

    ┌─────────────┐
    │ ███ фон ███ │   углы — гарантированно фон
    │ ██┌─────┐██ │
    │ ██│модель│██ │   центр — модель поверх фона
    │ ██└─────┘██ │
    └─────────────┘

Цвет фона берём по углам, цвет модели — по центру, выкинув всё, что близко к
фону. Качается это с телеграма, а не с gift-satellite, поэтому лимит API не
трогает вовсе.

Самопроверка встроена в саму затею: один фон встречается на сотнях разных
вещей, и если извлечение работает, его цвет обязан выходить одинаковым каждый
раз. Большой разброс = извлечение сломано, и это видно цифрой, а не на глаз.
"""
import io
import logging
import math
import re
import threading
import time

import requests

log = logging.getLogger("colors")

# Телеграм отдаёт страницу вещи только браузерному заголовку
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PAGE_URL = "https://t.me/nft/{slug}"
# Прямые адреса картинки — пробуем их первыми: это один запрос вместо двух.
# Если ни один не подойдёт, адрес вынимается из og:image самой страницы.
DIRECT_PATTERNS = (
    "https://nft.fragment.com/gift/{low}.medium.jpg",
    "https://nft.fragment.com/gift/{low}.small.jpg",
)
_OG_IMAGE = re.compile(rb'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                       re.I)

SHUTDOWN = threading.Event()


class ColorError(Exception):
    pass


# --- цвет как число ---------------------------------------------------------

def _srgb_to_linear(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb) -> tuple:
    """
    sRGB -> CIELAB (D65). Нужен потому, что в RGB расстояние не совпадает с
    тем, что видит глаз: зелёный и синий там «различаются» так же сильно, как
    светло-зелёный и тёмно-зелёный. В Lab одинаковая разница чисел означает
    одинаковую разницу на глаз, и только там расстояние между цветами что-то
    значит.
    """
    r, g, b = (_srgb_to_linear(v) for v in rgb)
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = (r * 0.2126 + g * 0.7152 + b * 0.0722)
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t + 16 / 116)

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(rgb_a, rgb_b) -> float:
    """Расстояние между цветами. <10 — глаз почти не отличает, >50 — явно разные."""
    a, b = rgb_to_lab(rgb_a), rgb_to_lab(rgb_b)
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


# Словарь для читаемости отчётов. На расчёты не влияет — считается всё по
# числам, имя приклеивается в самом конце.
PALETTE = {
    "чёрный": (18, 18, 20), "тёмно-серый": (70, 70, 74), "серый": (128, 128, 130),
    "светло-серый": (190, 190, 193), "белый": (245, 245, 245),
    "красный": (200, 40, 40), "тёмно-красный": (120, 25, 30), "розовый": (235, 140, 170),
    "оранжевый": (235, 140, 45), "коричневый": (120, 80, 50), "бежевый": (215, 195, 165),
    "жёлтый": (235, 210, 60), "золотой": (200, 165, 70), "оливковый": (130, 130, 60),
    "зелёный": (70, 160, 80), "тёмно-зелёный": (35, 90, 55), "мятный": (150, 220, 190),
    "бирюзовый": (60, 180, 180), "голубой": (120, 190, 235), "синий": (50, 90, 200),
    "тёмно-синий": (30, 45, 100), "фиолетовый": (120, 70, 190),
    "сиреневый": (185, 160, 225), "пурпурный": (170, 55, 140),
}


def color_name(rgb) -> str:
    return min(PALETTE, key=lambda name: delta_e(rgb, PALETTE[name]))


# --- картинка ---------------------------------------------------------------

def fetch_image(slug: str, timeout: float = 15.0) -> tuple:
    """
    Картинка вещи. Возвращает (байты, адрес, как нашли).

    Сначала прямые адреса — это один запрос. Не вышло — открываем страницу и
    берём адрес из og:image.
    """
    low = slug.strip().lower()
    headers = {"User-Agent": UA}
    for pattern in DIRECT_PATTERNS:
        url = pattern.format(low=low, slug=slug)
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            log.debug("прямой адрес %s не ответил: %s", url, e)
            continue
        if resp.status_code == 200 and resp.content[:2] in (b"\xff\xd8", b"\x89P"):
            return resp.content, url, "прямой адрес"

    page = PAGE_URL.format(slug=slug)
    try:
        resp = requests.get(page, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise ColorError(f"страница не открылась: {e}")
    if resp.status_code != 200:
        raise ColorError(f"страница ответила {resp.status_code}")
    found = _OG_IMAGE.search(resp.content)
    if not found:
        raise ColorError("на странице нет og:image")
    url = found.group(1).decode("utf-8", "replace")
    try:
        img = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise ColorError(f"картинка не скачалась: {e}")
    if img.status_code != 200:
        raise ColorError(f"картинка ответила {img.status_code}")
    return img.content, url, "через og:image"


def extract_colors(data: bytes, size: int = 128) -> dict:
    """
    Цвет фона и цвет модели с одной картинки.

    Порог отделения модели от фона — в Lab, а не в RGB: на тёмном фоне разница
    в RGB маленькая даже там, где глаз видит явно другой цвет.
    """
    try:
        from PIL import Image
    except ImportError:
        raise ColorError("нет Pillow — добавь его в requirements.txt")
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB").resize((size, size))
    except Exception as e:
        raise ColorError(f"картинка не разобралась: {e}")
    px = img.load()

    # углы — фон. Берём медиану по каналам: так одиночный блик не сдвинет цвет.
    corner = max(4, size // 10)
    corners = []
    for x0, y0 in ((0, 0), (size - corner, 0), (0, size - corner), (size - corner, size - corner)):
        for x in range(x0, x0 + corner):
            for y in range(y0, y0 + corner):
                corners.append(px[x, y])
    backdrop = tuple(sorted(c[i] for c in corners)[len(corners) // 2] for i in range(3))

    # центр — модель поверх фона
    lo, hi = int(size * 0.20), int(size * 0.80)
    model_px = [px[x, y] for x in range(lo, hi) for y in range(lo, hi)
                if delta_e(px[x, y], backdrop) > 18]
    total = (hi - lo) ** 2
    if len(model_px) < total * 0.02:
        raise ColorError("модель не отделилась от фона — почти весь центр совпал с фоном")

    # доминирующий цвет: огрубляем до 32 уровней на канал и берём самое
    # населённое ведро. Среднее по всем пикселям дало бы грязно-серый — оно
    # смешивает разные части рисунка.
    buckets = {}
    for pixel in model_px:
        buckets.setdefault(tuple(v // 8 for v in pixel), []).append(pixel)
    best = max(buckets.values(), key=len)
    model = tuple(sorted(c[i] for c in best)[len(best) // 2] for i in range(3))

    return {
        "backdrop_rgb": backdrop,
        "model_rgb": model,
        "coverage": len(model_px) / total,
        "dominance": len(best) / len(model_px),
    }


def probe(slug: str) -> dict:
    """Один слаг целиком: скачать, разобрать, вернуть всё, что вышло."""
    data, url, how = fetch_image(slug)
    out = extract_colors(data)
    out.update({"slug": slug, "url": url, "how": how, "bytes": len(data),
                "image": data})
    return out
