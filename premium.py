"""
Замер: платит ли рынок за то, что фон совпадает с моделью по цвету.

Это проверка предпосылки, а не поиск лотов. Мы уже один раз искали «лоты без
наценки за сочетание», ни разу не убедившись, что наценка вообще существует, —
и получили мусор. Здесь получается одно число, и по нему решается, есть ли
смысл в остальном.

Как считается. Цена вещи зависит в первую очередь от модели, поэтому сравнивать
цены между моделями бессмысленно. Каждую продажу делим на медиану продаж той же
модели — получается, во сколько раз эта вещь дороже обычной такой же.

Дальше главная ловушка: фон бывает дорогим сам по себе. Onyx Black дороже
прочих всегда, независимо от того, к какой модели он попал, — и если просто
сравнить «совпавшие» продажи с «несовпавшими», мы намеряем стоимость самих
фонов, а не сочетания. Поэтому у каждого фона отдельно считается его
собственный уровень, и остаток от деления на него — это уже вклад совпадения.

    доля      = цена продажи / медиана продаж этой модели
    K[фон]    = медиана долей по всем продажам с этим фоном
    остаток   = доля / K[фон]          <- здесь цены самого фона уже нет

Ответ — медиана остатков по совпавшим против медианы по несовпавшим.
"""
import logging
import statistics
import threading
import time

from api_client import ApiError

log = logging.getLogger("premium")

SHUTDOWN = threading.Event()

HISTORY_PAGES = 20          # страниц истории на коллекцию, по 20 продаж
MIN_SALES_PER_MODEL = 6     # меньше — медиана модели ничего не значит
MIN_SALES_PER_BACKDROP = 8  # меньше — уровень фона ничего не значит
PAIRS_CAP = 15000           # сколько пар хранить для отчёта
# Полосы по доле совпавшей площади. Верхняя — то, что мы называем сочетанием.
BANDS = ((0.0, 0.10), (0.10, 0.25), (0.25, 0.40), (0.40, 0.60), (0.60, 1.01))


def _history(client, collection: str, account, pages: int) -> list:
    """Все продажи коллекции подряд, без фильтра по модели."""
    out = []
    for page in range(pages):
        if SHUTDOWN.is_set():
            break
        try:
            data = client.get_history(collection, sort_by="date", page=page)
        except ApiError as e:
            account.record_error(f"premium history {collection} p{page}: {e}")
            break
        content = (data or {}).get("content") or []
        out.extend(content)
        meta = (data or {}).get("page") or {}
        if not content or (meta.get("totalPages") is not None
                           and page + 1 >= meta["totalPages"]):
            break
    return out


def _coverage_map(colors_base: dict, tol: float) -> dict:
    """
    (коллекция, модель) -> {фон: доля совпавшей площади}.

    Считается один раз на весь прогон: пар «цвет модели × фон» два с половиной
    миллиона, и внутри цикла по продажам это стоило бы часы.
    """
    import colors as colours
    backdrops = colours.backdrop_colors(colors_base)
    ready = [(name, colours.rgb_to_lab(rgb)) for name, rgb in backdrops.items()]
    limit = tol * tol
    out = {}
    for collection, models in ((colors_base or {}).get("models") or {}).items():
        if not isinstance(models, dict):
            continue
        for model, info in models.items():
            palette = (info or {}).get("palette") or []
            if not palette:
                continue
            entries = [(p.get("share", 0), colours.rgb_to_lab(p["rgb"])) for p in palette]
            shares = {}
            for name, lab in ready:
                share = sum(s for s, pl in entries
                            if (pl[0] - lab[0]) ** 2 + (pl[1] - lab[1]) ** 2
                            + (pl[2] - lab[2]) ** 2 <= limit)
                if share > 0:
                    shares[name.strip().lower()] = share
            out[(collection, model.strip())] = shares
    return out


def collect(client, account, collections: list, colors_base: dict,
            pages: int = HISTORY_PAGES, tol: float = 15.0,
            known: dict | None = None, on_progress=None, on_save=None,
            should_stop=None, save_every: int = 5) -> dict:
    """
    Пройти историю коллекций и собрать пары «совпадение площади -> доля цены».

    Сохраняется по ходу: проход стоит пару тысяч запросов и идёт около часа.
    Повторный запуск не ходит в уже пройденные коллекции.
    """
    covers = _coverage_map(colors_base, tol)
    if not covers:
        return {"error": "база цветов пуста", "pairs": []}

    done = set((known or {}).get("done") or [])
    pairs = list((known or {}).get("pairs") or [])
    stats = {"collections": 0, "sales": 0, "models": 0, "skipped": 0}

    todo = [c for c in collections if c not in done]
    if on_progress:
        on_progress(f"📐 Замер надбавки за совпадение цвета.\n"
                    f"Коллекций к проходу: {len(todo)}"
                    + (f", пройдено раньше: {len(done)}" if done else "") + "\n"
                    f"Страниц истории на коллекцию: {pages} — около "
                    f"{len(todo) * pages} запросов.\n"
                    "Остановить: /premiumstop")

    for index, collection in enumerate(todo, 1):
        if SHUTDOWN.is_set() or (should_stop and should_stop()):
            break
        sales = _history(client, collection, account, pages)
        done.add(collection)
        if not sales:
            continue
        stats["collections"] += 1
        stats["sales"] += len(sales)

        by_model = {}
        for sale in sales:
            price = sale.get("normalizedPrice")
            model = (sale.get("modelName") or "").strip()
            backdrop = (sale.get("backdropName") or "").strip().lower()
            if not price or price <= 0 or not model or not backdrop:
                continue
            by_model.setdefault(model, []).append((backdrop, price))

        for model, items in by_model.items():
            if len(items) < MIN_SALES_PER_MODEL:
                stats["skipped"] += 1
                continue
            base = statistics.median(price for _, price in items)
            if base <= 0:
                continue
            stats["models"] += 1
            shares = covers.get((collection, model)) or {}
            for backdrop, price in items:
                if len(pairs) >= PAIRS_CAP:
                    break
                pairs.append([backdrop, round(shares.get(backdrop, 0.0), 3),
                              round(price / base, 4)])

        if on_progress and index % 10 == 0:
            on_progress(f"Замер: {index} из {len(todo)} коллекций, "
                        f"продаж {stats['sales']}, моделей в счёте {stats['models']}, "
                        f"пар {len(pairs)}")
        if on_save and index % save_every == 0:
            try:
                on_save({"ts": time.time(), "done": sorted(done), "pairs": pairs})
            except Exception as e:
                log.warning("не смог сохранить замер на %d-й коллекции: %s", index, e)

    result = {"ts": time.time(), "done": sorted(done), "pairs": pairs}
    if on_save:
        try:
            on_save(result)
        except Exception as e:
            log.warning("не смог сохранить замер в конце: %s", e)
    result["stats"] = stats
    return result


def summarise(data: dict, match_from: float = 0.5) -> dict:
    """
    Из пар «совпадение -> доля цены» в ответ.

    Считаем внутри каждого фона отдельно: берём фон, сравниваем его продажи на
    моделях, с которыми он совпал, против его же продаж на моделях, с которыми
    не совпал. Собственная цена фона при таком сравнении сокращается сама — она
    одна и та же в обеих половинах.

    Общий способ — поделить всё на уровень фона и сравнить кучей — на проверке
    провалился: если фон совпадает почти всегда, его «уровень» вбирает наценку
    целиком, и разница схлопывается в ноль. Такие фоны просто не годятся для
    замера, и здесь они отсеиваются явно, а не портят ответ молча.
    """
    pairs = (data or {}).get("pairs") or []
    if not pairs:
        return {"error": "пар нет — замер не собран"}

    by_backdrop = {}
    for backdrop, share, ratio in pairs:
        by_backdrop.setdefault(backdrop, []).append((share, ratio))

    per_backdrop, unusable = [], 0
    for name, items in by_backdrop.items():
        matched = [r for s, r in items if s >= match_from]
        plain = [r for s, r in items if s < 0.10]
        if len(matched) < MIN_SALES_PER_BACKDROP or len(plain) < MIN_SALES_PER_BACKDROP:
            unusable += 1
            continue
        base = statistics.median(plain)
        if base <= 0:
            continue
        per_backdrop.append({
            "backdrop": name,
            "premium": (statistics.median(matched) / base - 1) * 100,
            "matched": len(matched), "plain": len(plain),
        })

    # Полосы по доле совпавшей площади — для картины целиком. Здесь уровень
    # фона вычитается делением, иначе полосы показали бы цену самих фонов.
    levels = {name: statistics.median(r for _, r in items)
              for name, items in by_backdrop.items()
              if len(items) >= MIN_SALES_PER_BACKDROP
              and statistics.median(r for _, r in items) > 0}
    rows = [(share, ratio / levels[backdrop])
            for backdrop, share, ratio in pairs if backdrop in levels]
    bands = []
    for lo, hi in BANDS:
        band = [res for share, res in rows if lo <= share < hi]
        if band:
            bands.append({"lo": lo, "hi": hi, "n": len(band),
                          "median": statistics.median(band)})

    answer = None
    if per_backdrop:
        answer = statistics.median(p["premium"] for p in per_backdrop)
    per_backdrop.sort(key=lambda p: -p["premium"])

    return {
        "pairs": len(pairs),
        "backdrops": len(by_backdrop),
        "usable": len(per_backdrop),
        "unusable": unusable,
        "bands": bands,
        "per_backdrop": per_backdrop,
        "matched_n": sum(p["matched"] for p in per_backdrop),
        "plain_n": sum(p["plain"] for p in per_backdrop),
        "premium_pct": answer,
        "match_from": match_from,
    }
