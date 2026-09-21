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
from model_picker import (PRICE_EXCLUDED_MARKETS, parse_sold_at,
                          percentile, sales_stats)

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
    # Самый дешёвый текущий офер по каждой модели — кладём в базу. Без него
    # нельзя посчитать главное: насколько аски на рынке отстоят от цен, по
    # которым вещи реально уходят, а от этого зависит, бывают ли находки вообще.
    cheapest = _cheapest_per_model(offers)
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
        best_offer = cheapest.get(model)
        if best_offer:
            models_data[model]["offer"] = best_offer["price"]
            models_data[model]["offer_backdrop"] = best_offer["backdrop"]
            models_data[model]["offer_market"] = best_offer["market"]
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
                "scanned": 0, "skipped": 0, "requests": 0}
    if on_progress:
        on_progress(note)

    started_requests = getattr(client, "total_requests", 0)
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
                        f"находок {len(all_finds)}, запросов "
                        f"{getattr(client, 'total_requests', 0) - started_requests}")

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
        "requests": getattr(client, "total_requests", 0) - started_requests,
    }


# --- Дозор ------------------------------------------------------------------
#
# Скан ищет оферы дешевле цены по сделкам и на это тратит часы: история —
# ~14 000 запросов на рынок, листинги — всего 393. Полный проход выходит
# ~15 часов, то есть на каждую коллекцию скан смотрит раз в 15 часов, а дешёвый
# лот живёт минуты. Поймать просадку при таком темпе нельзя в принципе.
#
# Дозор делает ровно обратное: историю не трогает совсем, гоняет по кругу одни
# листинги и сравнивает текущий флор модели с её же флором час назад. Просадка
# в чистом виде, без опорной цены по сделкам — и круг занимает минуты, а не часы.

WATCH_MARKETS = ("portals", "tonnel", "mrkt")


@dataclass
class WatchParams:
    """Настройки дозора."""
    drop_pct: float = 20.0          # насколько флор должен просесть против своего уровня
    price_min: float = 0.0
    price_max: float = 0.0
    markets: tuple = WATCH_MARKETS
    # Сколько держим замеров флора. Уровень — медиана по ним: один просевший
    # замер среди двух десятков медиану не двигает, и находка не «съедает» сама
    # себя на следующем круге.
    memory_hours: float = 12.0
    min_samples: int = 3            # меньше — уровню не верим, идёт прогрев
    repeat_hours: float = 6.0       # один и тот же лот не показываем чаще
    # До какого возраста доверяем цене по сделкам из базы скана. Она тут не
    # критерий, а страховка: лот дешевле вчерашнего флора, но дороже реальных
    # сделок — это не просадка, а возврат к норме после дорогого лота.
    ref_max_age_h: float = 72.0
    # Коллекции, чей флор в базе заведомо выше верхней границы, не опрашиваем.
    # С запасом: флор мог уехать вниз как раз из-за просадки.
    skip_factor: float = 2.0
    illiquid_per_month: float = 4.0
    illiquid_factor: float = 1.3
    pause_seconds: float = 0.0      # пауза между кругами
    # За сколько дней находка обязана быть минимумом. Своей памяти у дозора
    # хватает на часы, а минимум за три часа ничего не значит: это с тем же
    # успехом памп, вернувшийся к обычной цене. Настоящий многонедельный
    # минимум есть только в истории сделок, поэтому кандидат сверяется с ней.
    low_days: float = 14.0
    require_low: bool = True        # не минимум за low_days — не показываем
    low_min_sales: int = 4          # меньше сделок за срок — сверять не с чем
    # Сколько последних замеров на дорожку сохранять между перезапусками.
    # Ровно столько, сколько нужно, чтобы после деплоя уровень был готов сразу,
    # а не через три круга. В памяти замеров держится больше — медиана по ним
    # устойчивее, — но тащить всё в хранилище смысла нет.
    keep_samples: int = 3
    # Выгрузка тысяч дорожек — сотни килобайт. Каждый круг её писать незачем:
    # круг занимает минуты, а деплои случаются раз в сутки.
    save_every: int = 3


def watch_collections(client, account, baseline, params: WatchParams) -> list:
    """Какие коллекции обходить: всё, что есть в базе, минус заведомо дорогие."""
    stored = (baseline or {}).get("collections") or {}
    names = sorted(stored)
    if not names:
        try:
            names = sorted(c["name"] for c in (client.get_collections() or [])
                           if isinstance(c, dict) and c.get("name"))
        except ApiError as e:
            account.record_error(f"watch collections: {e}")
            return []
    if params.price_max and params.skip_factor:
        limit = params.price_max * params.skip_factor
        names = [n for n in names
                 if not ((stored.get(n) or {}).get("floor") or 0) > limit]
    return names


def _watch_offers(client, account, collection: str, params: WatchParams) -> list:
    """Дешёвый край коллекции по всем маркетам — один запрос на маркет."""
    offers = []
    for market in params.markets:
        if SHUTDOWN.is_set():
            break
        try:
            listings = client.search_market(market, collection)
        except ApiError as e:
            account.record_error(f"watch search {market}/{collection}: {e}")
            continue
        offers += _offers_from_listings(listings, market)
    return offers


def _by_track(offers: list) -> dict:
    """Все оферы по каждой паре «модель + чёрный/не чёрный»."""
    out = {}
    for offer in offers:
        if offer["price"] <= 0:
            continue
        out.setdefault((offer["model"], is_black(offer["backdrop"])), []).append(offer)
    return out


def _floor_by_track(offers: list) -> dict:
    """
    Самый дешёвый офер по каждой паре «модель + чёрный/не чёрный».

    Чёрные фоны ведём отдельной дорожкой: они дороже обычных в разы, и один
    чёрный лот среди обычных то поднимал бы уровень, то ронял — просадка
    мерещилась бы на ровном месте.

    Ценовое окно здесь НЕ применяется, хотя соблазн есть. Модель, которая
    обычно стоит 400 при потолке 350, иначе не набрала бы ни одного замера — и
    её падение до 300 осталось бы незамеченным, а это самая ценная находка из
    возможных. Окно проверяем позже, уже у самой просадки.
    """
    best = {}
    for offer in offers:
        price = offer["price"]
        if price <= 0:
            continue
        key = (offer["model"], is_black(offer["backdrop"]))
        cur = best.get(key)
        if cur is None or price < cur["price"]:
            best[key] = offer
    return best


def _in_window(price: float, params: WatchParams) -> bool:
    if params.price_min and price < params.price_min:
        return False
    if params.price_max and price > params.price_max:
        return False
    return True


def _sane_against_sales(offer: dict, known: dict, premiums: dict,
                        ref_fresh: bool) -> tuple[bool, float | None]:
    """
    Не возврат ли это к норме. Флор мог стоять высоко просто потому, что
    дешёвые лоты разобрали и остался один дорогой; новый обычный лот тогда
    «просаживает» флор, ничего при этом не стоя дешевле реальных сделок.

    Возвращает (брать ли, насколько дешевле цены по сделкам).
    """
    ref = (known or {}).get("ref")
    if not ref_fresh or not ref or ref <= 0:
        return True, None          # сравнить не с чем — верим просадке как есть
    expected = ref
    if is_black(offer["backdrop"]):
        info = (premiums or {}).get("Onyx Black" if "onyx" in offer["backdrop"].lower()
                                    else "Black")
        if not info or not info.get("k"):
            return True, None      # надбавки за чёрный не знаем — не судим
        expected = ref * info["k"]
    if expected <= 0:
        return True, None
    return offer["price"] < expected, (1 - offer["price"] / expected) * 100


def levels_to_store(levels: dict, keep: int) -> dict:
    """
    Замеры флоров в вид, который переживёт перезапуск.

    Вложенный словарь, а не склеенный ключ: имя модели или коллекции может
    содержать любой символ, и разделитель однажды попался бы внутри имени.

    Время замера хранится одно на всю выгрузку, а не при каждой цене. Дорожек
    тысячи, и отметка у каждой удваивала бы объём записи впустую: замеры идут
    подряд, круг за кругом, и на фоне 12-часового окна разница между ними
    роли не играет.
    """
    tracks = {}
    for (collection, model, black), history in levels.items():
        if not history:
            continue
        slot = tracks.setdefault(collection, {}).setdefault(model, {})
        slot["b" if black else "n"] = [round(price, 2) for _, price in history[-keep:]]
    return {"ts": int(time.time()), "tracks": tracks}


def levels_from_store(data: dict) -> dict:
    """Обратно в рабочий вид. Битые записи молча пропускаем — они не критичны."""
    saved_at = float((data or {}).get("ts") or 0) or time.time()
    out = {}
    for collection, models in ((data or {}).get("tracks") or {}).items():
        if not isinstance(models, dict):
            continue
        for model, slots in models.items():
            if not isinstance(slots, dict):
                continue
            for mark, prices in slots.items():
                pairs = [(saved_at, float(p)) for p in (prices or [])
                         if isinstance(p, (int, float)) and p > 0]
                if pairs:
                    out[(collection, model, mark == "b")] = pairs
    return out


def sales_low(sales: list, days: float, now: float) -> tuple:
    """
    Самая дешёвая сделка за последние days дней и сколько их было.

    Площадки, чьи цены в расчёт не идут, выбрасываются тем же правилом, что и
    везде: на Telegram Market цены живут своей жизнью.
    """
    edge = now - days * 86400
    prices = []
    for sale in sales or []:
        price = sale.get("normalizedPrice")
        if price is None or price <= 0:
            continue
        if (sale.get("market") or "").strip().lower() in PRICE_EXCLUDED_MARKETS:
            continue
        when = parse_sold_at(sale.get("soldAt"))
        if when is None or when < edge:
            continue
        prices.append(price)
    return (min(prices) if prices else None), len(prices)


def watch_market(client, account, params: WatchParams, baseline=None,
                 on_finds=None, on_progress=None, should_stop=None,
                 on_pass=None, on_levels=None, levels_seed=None,
                 fetch_sales=None, max_passes: int = 0) -> dict:
    """
    Бесконечный круг по листингам. Находка — модель, чей флор ушёл ниже своего
    же уровня за последние часы.

    on_finds(коллекция, находки) зовётся сразу, не дожидаясь конца круга:
    смысл дозора в том, чтобы сказать быстро.
    """
    stored = (baseline or {}).get("collections") or {}
    names = watch_collections(client, account, baseline, params)
    if not names:
        return {"error": "не из чего строить обход", "passes": 0}

    # замеры прошлой жизни процесса: с ними дозор ищет с первого круга, без них
    # три круга молчит
    levels: dict = levels_from_store(levels_seed) if levels_seed else {}
    reported: dict = {}   # лот -> когда показали
    passes = 0
    total_finds = 0
    if on_progress:
        on_progress(
            f"👁 Дозор запущен.\n"
            f"Коллекций в обходе: {len(names)}, маркетов {len(params.markets)}.\n"
            f"Ищу просадку флора от {params.drop_pct:g}% против уровня за "
            f"последние {params.memory_hours:g} ч.\n"
            f"И требую, чтобы лот был дешевле любой сделки за "
            f"{params.low_days:g} дней — иначе это не просадка, а возврат "
            f"к обычной цене после пампа.\n"
            + (f"Замеров из прошлого запуска: {len(levels)} — прогрев не нужен.\n"
               if levels else
               f"Первые {params.min_samples} круга — прогрев: набираю уровни, "
               f"находок не будет.\n")
            +
            f"Остановить: /watchstop")

    while not SHUTDOWN.is_set() and not (should_stop and should_stop()):
        passes += 1
        started = time.time()
        req0 = getattr(client, "total_requests", 0)
        pass_finds = warming = skipped_norm = not_low = 0

        for collection in names:
            if SHUTDOWN.is_set() or (should_stop and should_stop()):
                break
            offers = _watch_offers(client, account, collection, params)
            if not offers:
                continue
            snapshot = stored.get(collection) or {}
            known_models = snapshot.get("models") or {}
            premiums = snapshot.get("premiums_full") or {}
            ref_fresh = time.time() - snapshot.get("ts", 0) <= params.ref_max_age_h * 3600

            now = time.time()
            finds = []
            tracks = _by_track(offers)
            for (model, black), offer in _floor_by_track(offers).items():
                key = (collection, model, black)
                history = [(ts, price) for ts, price in levels.get(key, ())
                           if now - ts <= params.memory_hours * 3600]
                known = known_models.get(model) or {}
                # уровень считаем по прошлым замерам, текущий в него не входит —
                # иначе просадка сравнивалась бы сама с собой
                if len(history) < params.min_samples:
                    warming += 1
                else:
                    level = statistics.median(p for _, p in history)
                    drop = (1 - offer["price"] / level) * 100 if level > 0 else 0.0
                    per_month = known.get("per_month")
                    required = params.drop_pct
                    illiquid = per_month is not None and per_month < params.illiquid_per_month
                    if illiquid:
                        required *= params.illiquid_factor
                    if drop + 1e-9 >= required and _in_window(offer["price"], params):
                        ok, vs_ref = _sane_against_sales(offer, known, premiums, ref_fresh)
                        low, low_n, low_why = None, 0, "история не запрошена"
                        if ok and fetch_sales:
                            # Сверка с историей: лот обязан быть дешевле любой
                            # сделки за last_days, иначе это не просадка, а
                            # возврат к обычной цене после пампа.
                            sales = fetch_sales(collection, model)
                            low, low_n = sales_low(sales, params.low_days, now)
                            if low_n < params.low_min_sales:
                                # сверять не с чем — но молчать об этом нельзя,
                                # иначе находка выглядит проверенной, а она нет
                                low_why = (f"сделок за {params.low_days:g} дней "
                                           f"всего {low_n}"
                                           + (f" из {len(sales)} полученных"
                                              if sales else ", история пуста"))
                                low = None
                            elif params.require_low and offer["price"] >= low:
                                ok = False
                                not_low += 1
                        if not ok:
                            if low is None:
                                skipped_norm += 1   # дороже цены по сделкам
                        else:
                            hit = dict(offer)
                            age_h = (now - min(ts for ts, _ in history)) / 3600
                            # когда флор в последний раз был ещё на старом
                            # уровне: между тем замером и сейчас просадка и
                            # случилась, и это самое важное в находке
                            higher = [ts for ts, price in history
                                      if price > offer["price"] * 1.02]
                            last_high = max(higher) if higher else min(
                                ts for ts, _ in history)
                            # за какой срок эта цена — минимум: с последнего
                            # замера, где было столько же или дешевле
                            lower = [ts for ts, price in history
                                     if price <= offer["price"]]
                            min_since = max(lower) if lower else min(
                                ts for ts, _ in history)
                            hit.update({
                                "collection": collection,
                                "expected": level,
                                "benefit": drop,
                                "within_min": (now - last_high) / 60,
                                "min_span_min": (now - min_since) / 60,
                                "required": required,
                                "illiquid": illiquid,
                                "per_month": per_month,
                                "model_ref": known.get("ref"),
                                "rarity": known.get("rarity"),
                                "vs_ref": vs_ref,
                                "low_sale": low,
                                "low_days": params.low_days,
                                "low_sales_n": low_n,
                                "low_why": None if low else low_why,
                                # сколько лотов стоит по этой же сниженной цене
                                # и сколько всего у модели
                                "cheap_n": sum(
                                    1 for o in tracks.get((model, black), ())
                                    if o["price"] <= offer["price"] * 1.02),
                                "lots_n": len(tracks.get((model, black), ())),
                                "samples": len(history),
                                "level_age_h": age_h,
                                "rule": (f"флор был {level:.2f} — {len(history)} "
                                         f"замеров за {age_h:.0f} ч"),
                            })
                            finds.append(hit)
                history.append((now, offer["price"]))
                levels[key] = history

            if finds:
                fresh = []
                for hit in finds:
                    lot = (hit["market"], hit.get("slug") or hit.get("link") or hit["model"],
                           round(hit["price"], 2))
                    if now - reported.get(lot, 0) < params.repeat_hours * 3600:
                        continue
                    reported[lot] = now
                    fresh.append(hit)
                if fresh:
                    fresh.sort(key=lambda f: -f["benefit"])
                    pass_finds += len(fresh)
                    total_finds += len(fresh)
                    if on_finds:
                        on_finds(collection, fresh)

        # чистим хвосты, иначе за сутки словарь распухнет на весь рынок
        now = time.time()
        levels = {k: v for k, v in levels.items()
                  if v and now - v[-1][0] <= params.memory_hours * 3600}
        reported = {k: ts for k, ts in reported.items()
                    if now - ts <= params.repeat_hours * 3600}

        stats = {
            "pass": passes,
            "seconds": now - started,
            "requests": getattr(client, "total_requests", 0) - req0,
            "finds": pass_finds,
            "warming": warming,
            "skipped_norm": skipped_norm,
            "not_low": not_low,
            "tracks": len(levels),
            "collections": len(names),
        }
        if on_levels and passes % max(1, params.save_every) == 0:
            try:
                on_levels(levels_to_store(levels, params.keep_samples))
            except Exception as e:
                log.warning("не смог сохранить замеры флоров: %s", e)
        if on_pass:
            on_pass(stats)
        if max_passes and passes >= max_passes:
            break
        if params.pause_seconds and not SHUTDOWN.is_set():
            SHUTDOWN.wait(params.pause_seconds)

    return {"passes": passes, "finds": total_finds, "collections": len(names)}


# --- подходящий фон без наценки ---------------------------------------------
#
# Обычно фон, подходящий модели по цвету, стоит дороже обычного — за сочетание
# платят. Но лот выставляет человек, и цену он ставит по модели, а на фон часто
# не смотрит. Тогда вещь с нужным фоном стоит ровно столько же, сколько такая же
# с любым другим, — и это та самая покупка, которую стоит заметить.


@dataclass
class MatchParams:
    """Настройки поиска лотов с подходящим фоном без наценки."""
    tolerance_pct: float = 10.0   # насколько дороже обычного ещё считается «без наценки»
    price_min: float = 0.0
    price_max: float = 0.0
    markets: tuple = WATCH_MARKETS
    top_backdrops: int = 3
    tol: float = 15.0             # ближе этого цвет модели совпал с фоном
    min_coverage: float = 0.5     # столько площади модели должно совпасть
    # Сколько нужно обычных лотов, чтобы считать их уровнем цены. Минимум по
    # одному лоту уровнем не является: в прошлом прогоне так вышла находка
    # «дешевле на 67%» — у модели просто был ровно один другой лот, и он
    # переоценён.
    min_plain: int = 3
    # Разброс среди обычных лотов меньше этого — значит все они стоят на флоре
    # с точностью до копеек, и надбавку за фон там измерить нечем.
    min_spread_pct: float = 3.0
    # До какого возраста верить цене по сделкам. Она тут запасной вариант: если
    # у модели нет ни одного лота на обычном фоне, сравнивать больше не с чем.
    ref_max_age_h: float = 72.0


def scan_matches(client, account, params: MatchParams, colors_base: dict,
                 baseline=None, on_finds=None, on_progress=None,
                 should_stop=None) -> dict:
    """
    Один проход по листингам: где модель продаётся с подходящим ей фоном по
    цене обычной.

    Сравниваем в первую очередь с другими лотами той же модели прямо сейчас —
    это одна и та же минута, один и тот же рынок, и разница между ними значит
    ровно надбавку за фон. Цена по сделкам идёт в дело, только когда других
    лотов нет вовсе.
    """
    import colors as colours

    table = colours.matching_table(colors_base, params.top_backdrops,
                                   params.tol, params.min_coverage)
    if not table:
        return {"error": "база цветов пуста или в ней нет фонов", "finds": []}
    names = sorted({collection for collection, _ in table})
    stored = (baseline or {}).get("collections") or {}

    if on_progress:
        on_progress(f"🎯 Ищу подходящий фон без наценки.\n"
                    f"Моделей с подобранным фоном: {len(table)}, "
                    f"коллекций: {len(names)}.\n"
                    f"Совпадение: не меньше {params.min_coverage * 100:.0f}% площади "
                    f"модели при ΔE {params.tol:g}.\n"
                    f"Порог «без наценки»: не дороже обычного лота на "
                    f"{params.tolerance_pct:g}%.")

    found, checked, no_base, flat = [], 0, 0, 0
    for index, collection in enumerate(names, 1):
        if SHUTDOWN.is_set() or (should_stop and should_stop()):
            break
        offers = []
        for market in params.markets:
            try:
                offers += _offers_from_listings(
                    client.search_market(market, collection), market)
            except ApiError as e:
                account.record_error(f"match search {market}/{collection}: {e}")
        if not offers:
            continue

        snapshot = stored.get(collection) or {}
        known_models = snapshot.get("models") or {}
        ref_fresh = time.time() - snapshot.get("ts", 0) <= params.ref_max_age_h * 3600

        by_model = {}
        for offer in offers:
            by_model.setdefault(offer["model"], []).append(offer)

        hits = []
        for model, lots in by_model.items():
            info = table.get((collection, model))
            if not info:
                continue
            checked += 1
            suits = {name.strip().lower(): (share, delta)
                     for name, share, delta in info["backdrops"]}
            fitting = [l for l in lots if l["backdrop"].strip().lower() in suits]
            if not fitting:
                continue
            plain = sorted(l["price"] for l in lots
                           if l["backdrop"].strip().lower() not in suits and l["price"] > 0)
            if len(plain) >= params.min_plain:
                # медиана, а не минимум: минимум — это флор, и относительно
                # него «без наценки» выходит у чего угодно
                ordinary, source = statistics.median(plain), f"медиана {len(plain)} лотов"
                if (plain[-1] - plain[0]) / plain[0] * 100 < params.min_spread_pct:
                    flat += 1      # все лоты на флоре — надбавку мерить нечем
                    continue
            else:
                ref = (known_models.get(model) or {}).get("ref") if ref_fresh else None
                if not ref:
                    no_base += 1
                    continue
                ordinary, source = ref, "по сделкам модели"

            for lot in fitting:
                price = lot["price"]
                if price <= 0 or price > ordinary * (1 + params.tolerance_pct / 100):
                    continue
                if params.price_min and price < params.price_min:
                    continue
                if params.price_max and price > params.price_max:
                    continue
                hit = dict(lot)
                share, delta = suits[lot["backdrop"].strip().lower()]
                hit.update({
                    "collection": collection,
                    "delta": delta,
                    "coverage": share,
                    "ordinary": ordinary,
                    "source": source,
                    "premium": (price / ordinary - 1) * 100,
                    "model_rgb": info["rgb"],
                    "samples": info["samples"],
                    "rarity": (known_models.get(model) or {}).get("rarity"),
                })
                hits.append(hit)

        if hits:
            hits.sort(key=lambda h: h["premium"])
            found += hits
            if on_finds:
                on_finds(collection, hits)
        if on_progress and index % 20 == 0:
            on_progress(f"Пройдено {index} из {len(names)}: "
                        f"моделей с подобранным фоном проверено {checked}, "
                        f"найдено {len(found)}")

    return {"finds": sorted(found, key=lambda h: h["premium"]),
            "collections": len(names), "checked": checked,
            "no_base": no_base, "flat": flat}
