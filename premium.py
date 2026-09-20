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
from model_picker import parse_sold_at

log = logging.getLogger("premium")

SHUTDOWN = threading.Event()

HISTORY_PAGES = 20          # страниц истории на коллекцию, по 20 продаж
MIN_SALES_PER_MODEL = 6     # меньше — медиана модели ничего не значит
MIN_SALES_PER_BACKDROP = 8  # меньше — уровень фона ничего не значит
# Доля совпавшей площади округляется до этого шага, и продажи копятся вёдрами
# «фон + ведро». Так в хранилище едет одно число на продажу вместо строки с
# именем фона при каждой, и потолок на объём больше не нужен: при потолке
# замер выбрасывал бы половину рынка, причём по алфавиту.
SHARE_STEP = 0.05
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
    cells = {name: {int(bucket): list(values) for bucket, values in buckets.items()}
             for name, buckets in ((known or {}).get("cells") or {}).items()}
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
                bucket = int(round(shares.get(backdrop, 0.0) / SHARE_STEP))
                cells.setdefault(backdrop, {}).setdefault(bucket, []).append(
                    round(price / base, 3))

        if on_progress and index % 10 == 0:
            on_progress(f"Замер: {index} из {len(todo)} коллекций, "
                        f"продаж {stats['sales']}, моделей в счёте {stats['models']}")
        if on_save and index % save_every == 0:
            try:
                on_save({"ts": time.time(), "done": sorted(done), "cells": cells})
            except Exception as e:
                log.warning("не смог сохранить замер на %d-й коллекции: %s", index, e)

    result = {"ts": time.time(), "done": sorted(done), "cells": cells}
    if on_save:
        try:
            on_save(result)
        except Exception as e:
            log.warning("не смог сохранить замер в конце: %s", e)
    result["stats"] = stats
    return result


def _unpack(data: dict) -> tuple:
    """
    Собранное -> {фон: [(доля совпадения, доля цены)]}.

    Понимает и прежний формат со списком пар: первый полный прогон сохранился
    именно так, и перекачивать час ради смены раскладки незачем.
    """
    by_backdrop, total = {}, 0
    for name, buckets in ((data or {}).get("cells") or {}).items():
        items = []
        for bucket, values in buckets.items():
            share = int(bucket) * SHARE_STEP
            items += [(share, value) for value in values]
        by_backdrop[name] = items
        total += len(items)
    for backdrop, share, ratio in ((data or {}).get("pairs") or []):
        by_backdrop.setdefault(backdrop, []).append((share, ratio))
        total += 1
    return by_backdrop, total


def backdrop_levels(data: dict) -> dict:
    """
    Собственный уровень цены каждого фона: во сколько раз вещь с ним дороже
    обычной такой же. Нужен, чтобы отделить цену фона от вклада сочетания.
    """
    by_backdrop, _ = _unpack(data)
    return {name: statistics.median(r for _, r in items)
            for name, items in by_backdrop.items()
            if len(items) >= MIN_SALES_PER_BACKDROP
            and statistics.median(r for _, r in items) > 0}


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
    by_backdrop, total = _unpack(data)
    if not by_backdrop:
        return {"error": "продаж нет — замер не собран"}

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
    rows = [(share, ratio / levels[name])
            for name, items in by_backdrop.items() if name in levels
            for share, ratio in items]
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
        "pairs": total,
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


# --- прицельный замер -------------------------------------------------------
#
# Случайной выборкой сильные совпадения не поймать: их 0.03% рынка, и даже во
# всех 47 тысячах продаж их набирается тринадцать. Но история фильтруется и по
# модели, и по фону — значит можно спросить ровно то, что нужно, вместо того
# чтобы просеивать рынок целиком.

TARGET_PAGES = 2            # страниц на запрос, по 20 продаж
TARGET_MIN_SALES = 3        # меньше — медиана ничего не значит
WINDOW_DAYS = 10.0          # в каком окне вокруг продажи ищем, с чем её сравнить
NEAR_MIN = 3                # меньше соседних продаж — сравнивать не с чем


def _filtered(client, collection, account, models, backdrops, pages):
    out = []
    for page in range(pages):
        if SHUTDOWN.is_set():
            break
        try:
            data = client.get_history(collection, models=models, backdrops=backdrops,
                                      sort_by="date", page=page)
        except ApiError as e:
            account.record_error(f"premium target {collection} {models}/{backdrops}: {e}")
            break
        content = (data or {}).get("content") or []
        out += [s for s in content if s.get("normalizedPrice")]
        meta = (data or {}).get("page") or {}
        if not content or (meta.get("totalPages") is not None
                           and page + 1 >= meta["totalPages"]):
            break
    return out


def targeted(client, account, table: dict, levels: dict,
             pages: int = TARGET_PAGES, min_sales: int = TARGET_MIN_SALES,
             window_days: float = WINDOW_DAYS, near_min: int = NEAR_MIN,
             on_progress=None, should_stop=None) -> dict:
    """
    Спросить историю ровно по парам «модель + подходящий ей фон».

    По каждой модели два запроса: её продажи с этим фоном и её продажи вообще.
    Сравниваем медианы — получается надбавка за сочетание на этой модели.
    Дальше делим на собственный уровень фона, иначе намеряли бы, что Onyx Black
    дорог сам по себе.
    """
    rows, skipped = [], {"мало продаж с фоном": 0, "мало прочих продаж": 0,
                         "уровень фона неизвестен": 0,
                         "не с чем сравнить по времени": 0}
    keys = sorted(table)
    if on_progress:
        on_progress(estimate_text(len(keys), pages, getattr(client, "min_interval", 0.55)))
    for index, key in enumerate(keys, 1):
        if SHUTDOWN.is_set() or (should_stop and should_stop()):
            break
        collection, model = key
        backdrop, share, delta = table[key]["backdrops"][0]
        level = levels.get(backdrop.strip().lower())
        if not level:
            skipped["уровень фона неизвестен"] += 1
            continue
        with_backdrop = _filtered(client, collection, account, [model], [backdrop], pages)
        if len(with_backdrop) < min_sales:
            skipped["мало продаж с фоном"] += 1
            continue
        everything = _filtered(client, collection, account, [model], None, pages + 1)
        others = [(parse_sold_at(s.get("soldAt")), s["normalizedPrice"]) for s in everything
                  if (s.get("backdropName") or "").strip().lower()
                  != backdrop.strip().lower()]
        others = [(ts, price) for ts, price in others if ts]
        if len(others) < min_sales:
            skipped["мало прочих продаж"] += 1
            continue

        # Каждую продажу с нужным фоном сравниваем с продажами той же модели,
        # случившимися рядом по времени, а не со всеми подряд.
        #
        # Без этого замер меряет не фон, а движение рынка. У редкой пары
        # последние двадцать продаж уходят на месяцы назад, у модели целиком —
        # на дни; рынок за это время ушёл, и старое выглядит дороже нового.
        # В первом прогоне это било прямо в ответ: у пар с 3-9 продажами
        # надбавка выходила +19%, у пар с 35-40 продажами -13%. Настоящий
        # эффект от числа продаж зависеть не может.
        ratios = []
        for sale in with_backdrop:
            when = parse_sold_at(sale.get("soldAt"))
            if not when:
                continue
            near = [price for ts, price in others if abs(ts - when) <= window_days * 86400]
            if len(near) < near_min:
                continue
            base_near = statistics.median(near)
            if base_near > 0:
                ratios.append(sale["normalizedPrice"] / base_near)
        if len(ratios) < min_sales:
            skipped["не с чем сравнить по времени"] += 1
            continue
        ratio = statistics.median(ratios)
        matched = statistics.median(s["normalizedPrice"] for s in with_backdrop)
        base = statistics.median(price for _, price in others)
        rows.append({
            "collection": collection, "model": model, "backdrop": backdrop,
            "share": share, "delta": delta,
            "matched_n": len(with_backdrop), "others_n": len(others),
            "paired_n": len(ratios),
            "raw": (matched / base - 1) * 100,
            "premium": (ratio / level - 1) * 100,
            "level": level,
        })
        if on_progress and index % 20 == 0:
            on_progress(f"Прицельный замер: {index} из {len(keys)} моделей, "
                        f"посчитано {len(rows)}")

    answer = statistics.median(r["premium"] for r in rows) if rows else None
    rows.sort(key=lambda r: -r["premium"])
    return {"ts": time.time(), "rows": rows, "premium_pct": answer,
            "checked": len(keys), "skipped": skipped}


def estimate_text(pairs: int, pages: int, interval: float) -> str:
    """
    Сколько это займёт. Считается до старта, а не после.

    Первый прогон я оценил в десять минут, а он шёл два часа: прикидка была на
    сотню пар, а их оказалось 637, и на каждую уходит до пяти запросов.
    """
    # на пару: страницы по фону + страницы по модели; часть пар отсеивается
    # после первого же запроса, поэтому в среднем меньше максимума
    per_pair = pages + (pages + 1) * 0.65
    requests = int(pairs * per_pair)
    # пауза растёт от 429 и почти всегда упирается в потолок
    minutes = requests * max(interval, 1.5) / 60
    return (f"Пар к проверке: {pairs}.\n"
            f"Запросов будет примерно {requests}, это около "
            + (f"{minutes / 60:.1f} ч" if minutes >= 90 else f"{minutes:.0f} мин") + ".\n"
            f"Меньше пар — быстрее: порог задаётся через /matchtable.\n"
            "Остановить: /premiumstop")
