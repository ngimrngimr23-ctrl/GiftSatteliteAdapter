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

# Шесть, а не четыре. То, что отличает модель от соседних, бывает мелким:
# у Eternal Rose стеклянный колпак занимает 35% и одинаков у всех моделей, а
# сама роза — 2.5% и седьмое место. Отсев общего для коллекции делается потом,
# по собранным данным, но для этого цвет должен сначала попасть в палитру.
PALETTE_SIZE = 8      # сколько цветов модели запоминаем
MERGE_DELTA_E = 14.0    # ближе этого цвета считаем одним
BACKDROP_DELTA_E = 12.0 # ближе этого к любому цвету рамки — это фон
RING_MIN_SHARE = 0.005  # цвет рамки реже этого — шум сглаживания

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
    "светло-серый": (190, 190, 193), "белый": (247, 247, 247),
    "красный": (200, 40, 40), "тёмно-красный": (120, 25, 30),
    "розовый": (235, 130, 165), "малиновый": (205, 30, 110),
    "бледно-розовый": (243, 213, 228), "персиковый": (250, 200, 170),
    "оранжевый": (235, 140, 45), "коричневый": (120, 80, 50), "бежевый": (215, 195, 165),
    "кремовый": (245, 236, 214),
    "жёлтый": (235, 210, 60), "светло-жёлтый": (246, 238, 160), "золотой": (200, 165, 70),
    "оливковый": (130, 130, 60),
    "зелёный": (70, 160, 80), "тёмно-зелёный": (35, 90, 55),
    "салатовый": (160, 215, 150), "мятный": (150, 220, 190),
    "бирюзовый": (60, 180, 180), "серо-бирюзовый": (125, 165, 165),
    "голубой": (120, 190, 235), "бело-голубой": (218, 226, 246),
    "синий": (50, 90, 200), "тёмно-синий": (30, 45, 100),
    "фиолетовый": (120, 70, 190), "сиреневый": (185, 160, 225),
    "пурпурный": (170, 55, 140),
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


def _buckets(pixels: list) -> dict:
    """Пиксели по вёдрам 16 уровней на канал: ведро -> список пикселей."""
    out = {}
    for pixel in pixels:
        out.setdefault(tuple(v // 16 for v in pixel), []).append(pixel)
    return out


def _median_rgb(group: list) -> tuple:
    return tuple(sorted(c[i] for c in group)[len(group) // 2] for i in range(3))


def _cluster(buckets: dict, total: int, merge: float = None, top: int = 0) -> list:
    """
    Вёдра -> палитра [{rgb, share}], от самого населённого цвета.

    Склеиваем вёдра, неотличимые на глаз: на живой картинке чёрное тело Плюш
    Пепе разъехалось по трём вёдрам из-за теней и сжатия JPEG, и «главным
    цветом» оказывался кусок тела, а не тело.
    """
    if not total:
        return []
    merge = MERGE_DELTA_E if merge is None else merge
    clusters = []
    for _, group in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        colour = _median_rgb(group)
        lab = rgb_to_lab(colour)
        for cluster in clusters:
            if math.sqrt(sum((a - b) ** 2 for a, b in zip(lab, cluster["lab"]))) < merge:
                cluster["n"] += len(group)
                break
        else:
            clusters.append({"rgb": colour, "lab": lab, "n": len(group)})
    clusters.sort(key=lambda c: -c["n"])
    out = [{"rgb": c["rgb"], "share": c["n"] / total} for c in clusters]
    return out[:top] if top else out


def extract_colors(data: bytes, size: int = 160) -> dict:
    """
    Цвета фона и цвета модели с одной картинки.

    Фон читается по всей рамке, а не по углам. Причина: почти у каждого фона
    есть узор — значки той же гаммы, но светлее. Без него узор уезжает в
    «цвета модели»: на мухоморе он занял там первое место с 20%, обогнав жёлтую
    шляпку.

    Цвета рамки берутся вёдрами, без склейки близких. Склейка их губит: между
    фоном и узором лежат сглаженные пиксели всех промежуточных оттенков, и
    узор пришивается к фону цепочкой, хотя сам от него на ΔE 20.

    Отсеивать узор по оттенку было бы проще, но так выбросило бы целиком
    модель, чей цвет совпадает с фоном, — а это ровно тот случай, ради которого
    всё и затевается. Рамка же отличает их по месту: узор идёт по всему
    квадрату, модель сидит только в центре.
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

    frame = max(4, int(size * 0.12))
    ring = [px[x, y] for x in range(size) for y in range(size)
            if x < frame or y < frame or x >= size - frame or y >= size - frame]
    ring_buckets = _buckets(ring)
    refs = [rgb_to_lab(_median_rgb(group)) for group in ring_buckets.values()
            if len(group) >= len(ring) * RING_MIN_SHARE]
    if not refs:
        raise ColorError("не удалось прочитать фон по рамке")
    backdrop_palette = _cluster(ring_buckets, len(ring), top=3)
    backdrop = backdrop_palette[0]["rgb"]

    # Решаем по ведру, а не по пикселю: пикселей в центре десятки тысяч, вёдер
    # сотни, а результат тот же — внутри ведра цвет одинаковый.
    lo, hi = int(size * 0.18), int(size * 0.82)
    total = (hi - lo) ** 2
    model_buckets, plain_buckets, kept = {}, {}, 0
    for key, group in _buckets([px[x, y] for x in range(lo, hi)
                                for y in range(lo, hi)]).items():
        lab = rgb_to_lab(_median_rgb(group))
        if any(math.sqrt(sum((a - b) ** 2 for a, b in zip(lab, ref))) < BACKDROP_DELTA_E
               for ref in refs):
            plain_buckets[key] = group
            continue
        model_buckets[key] = group
        kept += len(group)
    if kept < total * 0.02:
        raise ColorError("модель не отделилась от фона — почти весь центр совпал с фоном")

    # Фон у телеграма — радиальный градиент: в центре светлее, по краям темнее.
    # Рамка даёт поэтому самый тёмный край, а модель лежит на светлой середине:
    # у Ivory White по рамке выходило (167,164,157) — серый, а не белый.
    # Второй замер берём там, где фон соседствует с моделью.
    near = _cluster(plain_buckets, sum(len(g) for g in plain_buckets.values()), top=1)
    backdrop_center = near[0]["rgb"] if near else backdrop

    palette = _cluster(model_buckets, kept, top=PALETTE_SIZE)
    return {
        "backdrop_rgb": backdrop,
        "backdrop_center_rgb": backdrop_center,
        "backdrop_palette": backdrop_palette,
        "model_rgb": palette[0]["rgb"],
        "palette": palette,
        "coverage": kept / total,
        "dominance": palette[0]["share"],
    }


def probe(slug: str) -> dict:
    """Один слаг целиком: скачать, разобрать, вернуть всё, что вышло."""
    data, url, how = fetch_image(slug)
    out = extract_colors(data)
    out.update({"slug": slug, "url": url, "how": how, "bytes": len(data),
                "image": data})
    return out


# --- полный сбор ------------------------------------------------------------

MIN_INTERVAL = 0.5      # пауза между скачиваниями с телеграма
SAMPLES_PER_MODEL = 2   # сколько экземпляров модели снимаем
SEEN_MIN = 2            # цвет засчитываем, если он повторился в стольких снимках
BACKDROP_SAMPLES = 50   # сколько замеров цвета фона держим на память


def merge_palettes(samples: list) -> list:
    """
    Палитры нескольких экземпляров одной модели -> её цвета.

    Снимки берутся с разных фонов не ради статистики, а ради отсева. В центр
    картинки попадает не только модель: значки узора, полупрозрачные части,
    через которые просвечивает фон. Всё это меняется вместе с фоном, а цвета
    самой модели повторяются от снимка к снимку — значит цвет, встретившийся
    один раз из трёх, к модели скорее всего не относится.

    При единственном снимке отсеивать нечем, и тогда отдаём как есть, пометив
    seen=1 — по этой пометке потом видно, каким записям верить меньше.
    """
    if not samples:
        return []
    groups = []
    for palette in samples:
        for entry in palette:
            lab = rgb_to_lab(entry["rgb"])
            for group in groups:
                if math.sqrt(sum((a - b) ** 2 for a, b in zip(lab, group["lab"]))) < MERGE_DELTA_E:
                    group["shares"].append(entry["share"])
                    group["seen"] += 1
                    break
            else:
                groups.append({"rgb": entry["rgb"], "lab": lab,
                               "shares": [entry["share"]], "seen": 1})
    need = SEEN_MIN if len(samples) >= SEEN_MIN else 1
    kept = [g for g in groups if g["seen"] >= need]
    kept.sort(key=lambda g: -sum(g["shares"]) / len(samples))
    return [{"rgb": list(g["rgb"]),
             "share": round(sum(g["shares"]) / len(samples), 4),
             "seen": g["seen"]}
            for g in kept[:PALETTE_SIZE]]


def pick_samples(offers: list, per_model: int, already=None) -> dict:
    """
    Из листингов — по нескольку экземпляров на модель, с разными фонами.

    Разные фоны здесь не прихоть: именно на них держится отсев узора и
    просвечивающих частей. Если у модели все лоты на одном фоне, берём что
    есть — но снимков будет меньше, и это отразится в пометке seen.

    already(модель) -> фоны, которые уже сняты в прошлый раз. При доборе они
    исключаются: второй снимок на том же фоне ничего не отсеет, узор на нём
    будет ровно тот же самый.
    """
    by_model = {}
    for offer in offers:
        model = (offer.get("model") or "").strip()
        slug = (offer.get("slug") or "").strip()
        if not model or not slug:
            continue
        by_model.setdefault(model, []).append(offer)
    out = {}
    for model, items in by_model.items():
        seen_before = {b.strip().lower() for b in (already(model) if already else ())}
        chosen, used = [], set(seen_before)
        for item in items:
            backdrop = (item.get("backdrop") or "").strip().lower()
            if backdrop in used:
                continue
            used.add(backdrop)
            chosen.append(item)
            if len(chosen) >= per_model:
                break
        if not seen_before:                     # добираем, если фонов не хватило
            for item in items:
                if len(chosen) >= per_model:
                    break
                if item not in chosen:
                    chosen.append(item)
        out[model] = chosen
    return out


def collect(offers_for, collections: list, per_model: int = SAMPLES_PER_MODEL,
            known: dict | None = None, on_progress=None, on_save=None,
            should_stop=None, save_every: int = 5, redo_thin: bool = False) -> dict:
    """
    Цвета моделей и фонов по всему рынку.

    offers_for(коллекция) -> листинги в виде сканера; берётся снаружи, чтобы
    сбор пользовался тем же клиентом и той же очередью запросов.

    Картинки качаются с телеграма, к gift-satellite идут только запросы за
    листингами — по три на коллекцию.
    """
    models = dict((known or {}).get("models") or {})
    backdrops = dict((known or {}).get("backdrops") or {})
    stats = {"models": 0, "images": 0, "errors": 0, "skipped": 0}
    last = 0.0

    for index, collection in enumerate(collections, 1):
        if SHUTDOWN.is_set() or (should_stop and should_stop()):
            break
        done = models.setdefault(collection, {})
        try:
            offers = offers_for(collection)
        except Exception as e:
            log.warning("листинги %s: %s", collection, e)
            stats["errors"] += 1
            continue

        def already(model):
            return (done.get(model) or {}).get("backdrops") or ()

        for model, items in pick_samples(offers, per_model,
                                         already if redo_thin else None).items():
            if SHUTDOWN.is_set() or (should_stop and should_stop()):
                break
            have = done.get(model) or {}
            prior = have.get("palette") or []
            if prior and (not redo_thin or have.get("samples", 0) >= per_model):
                stats["skipped"] += 1
                continue
            if prior and not items:
                # добирать нечем: ни одного лота на новом фоне
                stats["skipped"] += 1
                continue
            # Уже собранную палитру заводим в слияние как ещё один снимок:
            # тогда сегодняшний снимок с другого фона отсеет в ней узор, а не
            # заменит её собой.
            palettes = [[{"rgb": tuple(e["rgb"]), "share": e.get("share", 0)}
                         for e in prior]] if prior else []
            shot_backdrops = list(have.get("backdrops") or [])
            for item in items:
                wait = MIN_INTERVAL - (time.monotonic() - last)
                if wait > 0:
                    time.sleep(wait)
                last = time.monotonic()
                try:
                    data, _, _ = fetch_image(item["slug"])
                    got = extract_colors(data)
                except ColorError as e:
                    log.debug("%s/%s %s: %s", collection, model, item["slug"], e)
                    stats["errors"] += 1
                    continue
                stats["images"] += 1
                palettes.append(got["palette"])
                backdrop = (item.get("backdrop") or "").strip()
                if backdrop:
                    shot_backdrops.append(backdrop)
                    seen = backdrops.setdefault(backdrop, {})
                    samples = seen.setdefault("samples", [])
                    centre = seen.setdefault("center_samples", [])
                    samples.append(list(got["backdrop_rgb"]))
                    centre.append(list(got["backdrop_center_rgb"]))
                    del samples[:-BACKDROP_SAMPLES]
                    del centre[:-BACKDROP_SAMPLES]
            palette = merge_palettes(palettes)
            if palette:
                done[model] = {"palette": palette, "samples": len(palettes),
                               "backdrops": shot_backdrops[:8]}
                stats["models"] += 1

        if on_progress and index % 5 == 0:
            on_progress(f"Цвета: {index} из {len(collections)} коллекций, "
                        f"моделей {stats['models']}, картинок {stats['images']}, "
                        f"пропущено готовых {stats['skipped']}, ошибок {stats['errors']}")
        if on_save and index % save_every == 0:
            try:
                on_save(_pack(models, backdrops))
            except Exception as e:
                log.warning("не смог сохранить цвета на %d-й коллекции: %s", index, e)

    packed = _pack(models, backdrops)
    if on_save:
        try:
            on_save(packed)
        except Exception as e:
            log.warning("не смог сохранить цвета в конце: %s", e)
    packed["stats"] = stats
    return packed


def _pack(models: dict, backdrops: dict) -> dict:
    """
    Собранное в вид для хранения. Цвет фона — медиана по каналам, а не среднее:
    один экземпляр с крупной моделью, залезшей в рамку, среднее бы утащил.

    Замеры хранятся вместе с итогом, чтобы прерванный сбор можно было
    продолжить: иначе после перезапуска медиана считалась бы с нуля.
    """
    out = {}
    for name, seen in backdrops.items():
        samples = (seen or {}).get("samples") or []
        centre = (seen or {}).get("center_samples") or []
        if not samples and not centre:
            continue
        entry = {"n": len(samples) or len(centre)}
        if samples:
            entry["rgb"] = [sorted(s[i] for s in samples)[len(samples) // 2]
                            for i in range(3)]
            entry["samples"] = samples[-BACKDROP_SAMPLES:]
        if centre:
            entry["center_rgb"] = [sorted(s[i] for s in centre)[len(centre) // 2]
                                   for i in range(3)]
            entry["center_samples"] = centre[-BACKDROP_SAMPLES:]
        out[name] = entry
    return {"ts": time.time(), "models": models, "backdrops": out}


BACKDROP_TARGET = 12    # сколько замеров на фон достаточно


def collect_backdrops(offers_for, collections: list, target: int = BACKDROP_TARGET,
                      known: dict | None = None, on_progress=None, on_save=None,
                      should_stop=None, save_every: int = 10) -> dict:
    """
    Только цвета фонов. Отдельный проход нужен потому, что фонов восемь
    десятков, а моделей почти пять тысяч: пересобрать фоны стоит сотню
    картинок, а не восемь тысяч.

    Берём по одному лоту на фон, пока у каждого не наберётся target замеров.
    Палитры моделей при этом не трогаются — они от цвета фона не зависят.
    """
    models = dict((known or {}).get("models") or {})
    backdrops = dict((known or {}).get("backdrops") or {})
    stats = {"images": 0, "errors": 0, "backdrops": 0}
    last = 0.0

    for index, collection in enumerate(collections, 1):
        if SHUTDOWN.is_set() or (should_stop and should_stop()):
            break
        try:
            offers = offers_for(collection)
        except Exception as e:
            log.warning("листинги %s: %s", collection, e)
            stats["errors"] += 1
            continue

        wanted = {}
        for offer in offers:
            backdrop = (offer.get("backdrop") or "").strip()
            slug = (offer.get("slug") or "").strip()
            if not backdrop or not slug or backdrop in wanted:
                continue
            have = len(((backdrops.get(backdrop) or {}).get("center_samples")) or [])
            if have < target:
                wanted[backdrop] = offer

        for backdrop, offer in wanted.items():
            if SHUTDOWN.is_set() or (should_stop and should_stop()):
                break
            wait = MIN_INTERVAL - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            last = time.monotonic()
            try:
                data, _, _ = fetch_image(offer["slug"])
                got = extract_colors(data)
            except ColorError as e:
                log.debug("фон %s (%s): %s", backdrop, offer["slug"], e)
                stats["errors"] += 1
                continue
            stats["images"] += 1
            seen = backdrops.setdefault(backdrop, {})
            edge = seen.setdefault("samples", [])
            centre = seen.setdefault("center_samples", [])
            edge.append(list(got["backdrop_rgb"]))
            centre.append(list(got["backdrop_center_rgb"]))
            del edge[:-BACKDROP_SAMPLES]
            del centre[:-BACKDROP_SAMPLES]

        if on_progress and index % 10 == 0:
            ready = sum(1 for v in backdrops.values()
                        if len((v or {}).get("center_samples") or []) >= target)
            on_progress(f"Фоны: {index} из {len(collections)} коллекций, "
                        f"набрано полностью {ready} из {len(backdrops)}, "
                        f"картинок {stats['images']}, ошибок {stats['errors']}")
        if on_save and index % save_every == 0:
            try:
                on_save(_pack(models, backdrops))
            except Exception as e:
                log.warning("не смог сохранить фоны на %d-й коллекции: %s", index, e)

    packed = _pack(models, backdrops)
    stats["backdrops"] = len(packed.get("backdrops") or {})
    if on_save:
        try:
            on_save(packed)
        except Exception as e:
            log.warning("не смог сохранить фоны в конце: %s", e)
    packed["stats"] = stats
    return packed
