"""
Скан рынка: ищет листинги, выставленные заметно ниже цены, по которой модель
реально уходит.

Цена модели берётся по сделкам (тем же процентилем, что и в отборе для
ордеров), а не по чужим аскам — мы уже намерили, что на неликвиде аск
расходится с реальной ценой в 1.5-2.6 раза. Площадки, чьи сделки в расчёт не
идут, перечислены в model_picker.PRICE_EXCLUDED_MARKETS — правило общее для
сканера и для отбора моделей.

Чёрные фоны считаются отдельно. Причина простая: Black и Onyx Black почти
всегда дороже обычных, причём по-разному — в Toy Bear на модели El Rojo
обычные фоны давали ~36, Onyx Black ~61, Black ~175. Мешать их с остальными
нельзя ни при расчёте нормальной цены, ни при оценке офера.
"""
import logging
import statistics
import threading
import time
from dataclasses import dataclass, field

from api_client import ApiError
from model_picker import percentile, sales_stats

log = logging.getLogger("scanner")

ALL_MARKETS = ("portals", "tonnel", "mrkt", "tg", "getgems")
BLACK_BACKDROPS = ("Black", "Onyx Black")
_BLACK_LOWER = {b.lower() for b in BLACK_BACKDROPS}

# Сколько страниц истории коллекции брать на замер надбавки за фон. Чёрные
# сделки запрашиваются с фильтром по фону, поэтому страница целиком состоит из
# них и трёх хватает на устойчивую медиану.
COLLECTION_HISTORY_PAGES = 3

# Флаг остановки на весь процесс. Нужен потому, что скан живёт в потоке пула, а
# потоки пула дожидаются завершения при выходе — то есть процесс не может
# умереть, пока идёт проход. При деплое Render поднимает новый инстанс раньше,
# чем гаснет старый: старый перестаёт опрашивать телеграм, /scanstop до него уже
# не доходит, а скан всё это время продолжает ходить в API тем же токеном.
SHUTDOWN = threading.Event()


@dataclass
class ScanParams:
    """Настройки прогона. Всё, что задаётся командой, лежит здесь."""
    min_benefit_pct: float = 20.0      # минимальная выгода, ниже которой не показываем
    price_min: float = 0.0             # границы цены самого офера
    price_max: float = 0.0             # 0 = без верхней границы
    illiquid_per_month: float = 4.0    # реже этого — модель считается неликвидом
    illiquid_factor: float = 1.3       # и требование по выгоде для неё умножается
    ref_percentile: float = 45.0
    fresh_hours: float = 24.0
    sales_depth: int = 16
    markets: tuple = ALL_MARKETS
    probe_all: bool = True             # опрашивать модели, не попавшие в дешёвый скан
    # Сколько часов доверять сохранённой базе. История продаж за сутки почти не
    # меняется, а её сбор — самая дорогая часть прохода: именно отсюда берётся
    # разница между первым прогоном в часы и следующими в минуты.
    baseline_max_age_h: float = 48.0
    collections: list = field(default_factory=list)  # пусто = все коллекции сервиса
    # Пропускать коллекции, по которым база уже собрана. Нужно, чтобы достроить
    # базу после обрыва: искать оферы так нельзя (листинги меняются), а вот
    # добрать недостающие коллекции — минуты вместо часа.
    only_missing: bool = False


def is_black(backdrop: str) -> bool:
    return (backdrop or "").strip().lower() in _BLACK_LOWER


def _collection_history(client, collection: str, backdrops, account, pages: int) -> list:
    """Сделки коллекции целиком (без фильтра по модели) — для замера надбавки фона."""
    out = []
    for page in range(pages):
        try:
            data = client.get_history(collection, backdrops=backdrops,
                                      sort_by="date", page=page)
        except ApiError as e:
            account.record_error(f"scan history {collection} {backdrops or 'все'} p{page}: {e}")
            break
        content = (data or {}).get("content") or []
        out.extend(content)
        meta = (data or {}).get("page") or {}
        if not content or (meta.get("totalPages") is not None and page + 1 >= meta["totalPages"]):
            break
    return out


def backdrop_premiums(client, collection: str, account, params: ScanParams) -> dict:
    """
    Во сколько раз чёрный фон дороже обычного в этой коллекции.

    K = медиана чёрных сделок / медиана обычных сделок, отдельно для Black и
    Onyx Black: разница между ними бывает трёхкратной, и общий коэффициент
    сделал бы любой Onyx «выгодным», а любой Black «ложным».

    Сравниваются медианы по разным наборам моделей — если чёрный чаще попадает
    на дорогие модели, надбавка выйдет завышенной. Поэтому рядом отдаётся число
    сделок, по которым она посчитана.
    """
    plain = _collection_history(client, collection, None, account, COLLECTION_HISTORY_PAGES)
    normal_prices = [s["normalizedPrice"] for s in plain
                     if s.get("normalizedPrice") is not None and not is_black(s.get("backdropName"))]
    if not normal_prices:
        return {}
    base = statistics.median(normal_prices)
    if base <= 0:
        return {}

    out = {"_base": base, "_base_n": len(normal_prices)}
    for backdrop in BLACK_BACKDROPS:
        sales = _collection_history(client, collection, [backdrop], account,
                                    COLLECTION_HISTORY_PAGES)
        prices = [s["normalizedPrice"] for s in sales if s.get("normalizedPrice") is not None]
        if not prices:
            continue
        med = statistics.median(prices)
        out[backdrop] = {"k": med / base, "median": med, "n": len(prices)}
    return out


def evaluate_offer(offer: dict, model_ref: float | None, premiums: dict,
                   per_month: float | None, params: ScanParams) -> dict | None:
    """
    Насколько офер ниже цены, по которой модель реально уходит.

    Обычный фон — сравниваем с ценой модели по сделкам (чёрные сделки в неё не
    входят).

    Чёрный фон — два независимых правила, срабатывает любое:
      1) офер дешевле медианы чёрных сделок по коллекции. Работает на рядовых
         моделях, у которых чёрный стоит примерно как у всех;
      2) надбавка самого офера за чёрный ниже типичной для коллекции. Нужна для
         дорогих моделей: у них чёрный законно дороже медианы по коллекции, и
         первое правило не сработает никогда. Истории по паре «редкая модель +
         чёрный» не существует, поэтому надбавку берём прямо из офера.
    """
    price = offer["price"]
    if price <= 0:
        return None
    if params.price_min and price < params.price_min:
        return None
    if params.price_max and price > params.price_max:
        return None

    best = None
    if is_black(offer["backdrop"]):
        info = premiums.get(offer["backdrop"].strip().title()) or \
            premiums.get("Onyx Black" if "onyx" in offer["backdrop"].lower() else "Black")
        # правило 1: дешевле медианы чёрных сделок коллекции
        if info and info["median"] > 0:
            best = {"benefit": (1 - price / info["median"]) * 100,
                    "expected": info["median"], "rule": "дешевле чёрных по коллекции"}
        # правило 2: надбавка офера ниже типичной
        if info and model_ref and model_ref > 0:
            expected = model_ref * info["k"]
            own_k = price / model_ref
            if own_k < info["k"]:
                cand = {"benefit": (1 - price / expected) * 100, "expected": expected,
                        "rule": f"надбавка ×{own_k:.2f} против ×{info['k']:.2f} по коллекции"}
                if best is None or cand["benefit"] > best["benefit"]:
                    best = cand
    else:
        if model_ref and model_ref > 0:
            best = {"benefit": (1 - price / model_ref) * 100,
                    "expected": model_ref, "rule": "дешевле цены по сделкам"}

    if best is None:
        return None

    # неликвид не вычёркиваем, но планку для него поднимаем: скидка на модели,
    # которая торгуется раз в месяц, — это ещё не деньги, из неё надо выйти
    required = params.min_benefit_pct
    illiquid = per_month is not None and per_month < params.illiquid_per_month
    if illiquid:
        required *= params.illiquid_factor
    # 1e-9 — против погрешности float: выгода ровно в planку считается как
    # 19.999999999999996 и без допуска отсеивалась бы
    if best["benefit"] + 1e-9 < required:
        return None

    best.update({"required": required, "illiquid": illiquid, "per_month": per_month,
                 "model_ref": model_ref})
    best.update(offer)
    return best


def _offers_from_listings(listings: list, market: str) -> list:
    """Листинги маркета -> оферы в едином виде."""
    out = []
    for item in listings or []:
        price = item.get("normalizedPrice")
        model = item.get("modelName")
        if price is None or not model:
            continue
        out.append({
            "market": market,
            "model": model.strip(),
            "backdrop": (item.get("backdropName") or "").strip(),
            "symbol": (item.get("symbolName") or "").strip(),
            "price": price,
            "slug": item.get("slug") or "",
            "link": item.get("link") or "",
        })
    return out


def _cheapest_per_model(offers: list) -> dict:
    """Самый дешёвый офер по каждой модели — из него берём floor коллекции."""
    best = {}
    for offer in offers:
        cur = best.get(offer["model"])
        if cur is None or offer["price"] < cur["price"]:
            best[offer["model"]] = offer
    return best


def scan_collection(client, collection: str, account, params: ScanParams,
                    known: dict | None, fetch_sales, should_stop=None) -> tuple[list, dict]:
    """
    Один подарок целиком. Возвращает (находки, что запомнить в базу).

    fetch_sales(collection, model) -> список сделок; передаётся снаружи, чтобы
    сканер пользовался тем же кешем и тем же правилом глубины, что и отбор
    моделей для ордеров.

    known — данные по этой коллекции из прошлого прогона. Цена модели по сделкам
    и надбавки за фоны берутся оттуда, если они не старше baseline_max_age_h:
    листинги всё равно опрашиваем заново, а вот историю — самую дорогую часть —
    второй раз не собираем.
    """
    offers = []
    for market in params.markets:
        try:
            listings = client.search_market(market, collection)
        except ApiError as e:
            account.record_error(f"scan search {market}/{collection}: {e}")
            continue
        offers += _offers_from_listings(listings, market)

    if not offers:
        return [], {}

    # floor коллекции считаем по обычным фонам: чёрный дороже по определению и
    # сместил бы точку отсчёта вверх
    plain = [o["price"] for o in offers if not is_black(o["backdrop"])]
    floor = min(plain) if plain else min(o["price"] for o in offers)

    # Коллекция целиком дороже верхней границы — дальше смотреть незачем,
    # ни один её офер в окно не попадёт. Это главная экономия на полном скане.
    if params.price_max and floor > params.price_max:
        return [], {"floor": floor, "skipped": "дороже верхней границы"}

    seen = set(o["model"] for o in offers)
    catalog = {}
    if params.probe_all:
        try:
            catalog = {m["name"].strip(): m.get("rarity")
                       for m in (client.get_models(collection) or [])
                       if isinstance(m, dict) and (m.get("name") or "").strip()}
        except ApiError as e:
            account.record_error(f"scan models {collection}: {e}")
        # дешёвый скан отдаёт только 50 самых дешёвых лотов, поэтому модели
        # подороже в него не попадают — их опрашиваем поштучно
        for name in catalog:
            if name in seen:
                continue
            for market in params.markets:
                try:
                    listings = client.search_market(market, collection, models=[name])
                except ApiError as e:
                    account.record_error(f"scan probe {market}/{collection}/{name}: {e}")
                    continue
                got = _offers_from_listings(listings, market)
                if got:
                    offers += got
                    break  # нашли листинг — остальные маркеты не опрашиваем

    premiums = (known or {}).get("premiums_full")
    if not premiums:
        premiums = backdrop_premiums(client, collection, account, params)

    excluded = set(_BLACK_LOWER)  # чёрные сделки не идут в «нормальную» цену модели
    models_data, finds = {}, []
    known_models = (known or {}).get("models") or {}
    # Историю — самую дорогую часть прохода — качаем только для моделей, у
    # которых есть офер внутри ценового окна. У остальных находки быть не может
    # по определению, и цена по сделкам им не нужна.
    def in_window(price: float) -> bool:
        if params.price_min and price < params.price_min:
            return False
        if params.price_max and price > params.price_max:
            return False
        return True

    wanted = sorted({o["model"] for o in offers if in_window(o["price"])})
    for model in wanted:
        # проверяем и внутри коллекции: одна коллекция идёт минутами, и ждать
        # её окончания ради остановки бессмысленно
        if SHUTDOWN.is_set() or (should_stop and should_stop()):
            break
        saved = known_models.get(model)
        if saved and saved.get("ref"):
            models_data[model] = dict(saved)
            models_data[model].setdefault("rarity", catalog.get(model))
        else:
            stats = None
            sales = fetch_sales(collection, model)
            if sales:
                stats = sales_stats(sales, excluded, params.fresh_hours, time.time(),
                                    params.ref_percentile)
            models_data[model] = {
                "ref": stats["ref"] if stats else None,
                "per_month": stats["per_month"] if stats else None,
                "used": stats["used"] if stats else 0,
                "rarity": catalog.get(model),
            }
        for offer in offers:
            if offer["model"] != model or not in_window(offer["price"]):
                continue
            hit = evaluate_offer(offer, models_data[model]["ref"], premiums,
                                 models_data[model]["per_month"], params)
            if hit:
                hit["collection"] = collection
                hit["rarity"] = catalog.get(model)
                finds.append(hit)

    finds.sort(key=lambda f: -f["benefit"])
    snapshot = {
        "ts": time.time(),   # возраст считаем по коллекции: база пополняется
        "floor": floor,      # по частям, и общая отметка врала бы про старые
        "premiums_full": premiums,
        "models": models_data,
    }
    return finds, snapshot

def scan_market(client, account, params: ScanParams, fetch_sales,
                baseline: dict | None = None, on_progress=None, on_finds=None,
                should_stop=None, on_baseline=None, baseline_every: int = 5) -> dict:
    """
    Полный проход. Результаты отдаются порциями через on_finds, а не одним
    куском в конце: прогон по всем коллекциям идёт часами, и обрыв посередине
    не должен обнулять уже найденное.

    should_stop() -> True прерывает проход.

    on_baseline(собранное) зовётся каждые baseline_every коллекций: полный
    проход идёт часами, и терять его из-за перезапуска или обрыва нельзя.
    """
    names = list(params.collections)
    if not names:
        try:
            names = [c["name"] for c in (client.get_collections() or [])
                     if isinstance(c, dict) and c.get("name")]
        except ApiError as e:
            account.record_error(f"scan collections: {e}")
            return {"error": str(e), "collections": 0}
    names.sort()

    # база прошлого прогона: каждая коллекция живёт со своим возрастом, потому
    # что база пополняется по частям и общая отметка врала бы про старые записи
    now_ts = time.time()
    stored = (baseline or {}).get("collections") or {}
    saved = {name: snap for name, snap in stored.items()
             if isinstance(snap, dict)
             and now_ts - snap.get("ts", 0) <= params.baseline_max_age_h * 3600}
    # Одно сообщение на оба режима: раньше их было два, и в режиме missing они
    # противоречили друг другу — «пропускаю 15 собранных» и тут же «из базы
    # возьму цены по 15 коллекциям», хотя эти коллекции и не посещаются.
    known = len(saved)
    offset = 0   # сколько коллекций уже за спиной до начала этого прохода
    if params.only_missing:
        names = [n for n in names if n not in saved]
        offset = known
        note = (f"Достраиваю базу.\nВ базе уже {known} коллекций — пропускаю их.\n"
                f"К проходу: {len(names)}.")
    elif known:
        note = (f"Коллекций к проходу: {len(names)}.\n"
                f"По {known} из них цены возьму из базы — историю заново не собираю, "
                f"это самая долгая часть.")
    else:
        note = (f"Коллекций к проходу: {len(names)}.\n"
                f"Базы нет, историю собираю с нуля — это долго.")
    if not names:
        if on_progress:
            on_progress(note + "\n\nНовых коллекций нет, проходить нечего.")
        return {"finds": [], "baseline": {}, "collections": 0,
                "scanned": 0, "skipped": 0, "requests": client.request_count}
    if on_progress:
        on_progress(note)

    collected, all_finds = {}, []
    done = skipped = 0
    for index, collection in enumerate(names, 1):
        if SHUTDOWN.is_set():
            log.info("процесс гасится — прерываю скан на %d из %d", index - 1, len(names))
            break
        if should_stop and should_stop():
            if on_progress:
                on_progress(f"Остановлено на {index - 1} из {len(names)}")
            break
        try:
            finds, snapshot = scan_collection(client, collection, account, params,
                                              saved.get(collection), fetch_sales,
                                              should_stop)
        except Exception as e:  # одна кривая коллекция не должна ронять весь проход
            account.record_error(f"scan {collection}: {e}")
            log.exception("скан %s упал", collection)
            continue
        if snapshot:
            collected[collection] = snapshot
        # сохраняем по ходу, а не в конце: проход идёт часами
        if on_baseline and collected and index % baseline_every == 0:
            try:
                on_baseline(dict(collected))
            except Exception as e:
                log.warning("не смог сохранить базу на %d-й коллекции: %s", index, e)
        if snapshot.get("skipped"):
            skipped += 1
        else:
            done += 1
        if finds:
            all_finds += finds
            if on_finds:
                on_finds(collection, finds)
        if on_progress and index % 10 == 0:
            # Счёт всегда по рынку целиком: в режиме missing уже собранные
            # коллекции тоже пройдены, просто раньше, и сбрасывать счётчик на
            # ноль — значит показывать откат назад.
            on_progress(f"Пройдено {offset + index} из {offset + len(names)}: "
                        f"разобрано {done}, пропущено по цене {skipped}, "
                        f"находок {len(all_finds)}, запросов {client.request_count}")

    if on_baseline and collected:
        try:
            on_baseline(dict(collected))
        except Exception as e:
            log.warning("не смог сохранить базу в конце прохода: %s", e)

    return {
        "finds": sorted(all_finds, key=lambda f: -f["benefit"]),
        "baseline": collected,
        "collections": len(names),
        "scanned": done,
        "skipped": skipped,
        "requests": client.request_count,
    }
