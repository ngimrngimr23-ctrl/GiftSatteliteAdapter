import time
import logging
import statistics
import threading

from api_client import ApiError, HISTORY_PAGE_SIZE
from model_picker import (count_eligible, check_pump, has_suspect_chars, parse_sold_at,
                          pick_candidates, trim_to_limit)

log = logging.getLogger("updater")

MARKETS = ["portals", "tonnel", "mrkt"]  # где смотрим актуальную цену
MIN_DELTA = 0.02  # не дёргать PUT, если цена изменилась меньше чем на столько TON
# Во сколько раз разрешено превысить базовое число страниц, добирая сделки
# взамен отброшенных, и жёсткий потолок. Из отчёта: у половины моделей
# выбрасывается около 12% выборки — им добор не понадобится вовсе, лишние
# страницы уйдут только на самые ходовые.
# Окно, которое выборка обязана покрыть, даже если нужное число сделок набралось
# раньше. У ходовой модели depth сделок укладывается в сутки-двое, и оценка
# начинает описывать не рынок, а последний день: живой случай — Gray Smoke, где
# 16 сделок покрыли 3.4 дня и цена разошлась с реальной в полтора раза.
SALES_WINDOW_DAYS = 21
# Потолок выборки. Без него правило «не меньше трёх недель» на бойкой модели
# требовало всю её историю за этот срок: Moon Pendant / Ruby Core торгуется 13
# раз в сутки, то есть три недели — это 289 сделок и 19 страниц. Для оценки
# цены столько не нужно, а проход по рынку из-за этого растягивался втрое.
# Сотни хватает: у самой бойкой модели она закрывает около недели, у остальных
# правило и не срабатывает — они не набирают сотню и за три недели.
SALES_MAX = 100
# Потолок страниц. Три недели самой ходовой модели (~170 сделок в месяц) — это
# около 120 сделок, то есть 6 страниц; 25 оставляет запас и не даёт редкому
# выбросу съесть весь цикл.
HISTORY_MAX_PAGES = 25
HISTORY_CACHE_HOURS = 6.0  # медиана по сотне сделок за час не меняется — не перезапрашиваем каждый цикл
PROBE_CACHE_HOURS = 6.0  # хватает, чтобы все аккаунты в рамках одного прохода взяли цены из кеша

# Цикл с автоподбором идёт минутами, а /setinterval разрешает поставить 1 минуту,
# поэтому запуски нужно защитить от наложения друг на друга.
_CYCLE_LOCK = threading.Lock()

# Рыночные данные не зависят от аккаунта, поэтому кеши общие на процесс:
# несколько аккаунтов, работающих по одним и тем же коллекциям, платят за сбор
# данных один раз, а не по разу каждый.
_HISTORY_CACHE: dict = {}  # {(collection, model): (ts, [продажи])}
_PROBE_CACHE: dict = {}  # {collection: (ts, {model: floor})}
_CATALOG_CACHE: dict = {}  # {collection: (ts, {model: rarity})}

# Поля, которые нужно переслать обратно в PUT /user/update-subscription/:id
# (тело идентично POST /user/subscribe)
SUBSCRIPTION_BODY_FIELDS = [
    "subscriptionName",
    "collectionName",
    "modelNames",
    "backdropNames",
    "symbolNames",
    "numberPattern",
    "portalsNotifyMaxPrice",
    "notifyTg",
    "notifyPortals",
    "notifyTonnel",
    "notifyMrkt",
    "notifyGetgems",
    "portalsAutobuy",
    "portalsAutobuyMaxPrice",
    "portalsAutobuyQuantity",
    "autobuyTg",
    "autobuyPortals",
    "autobuyTonnel",
    "autobuyMrkt",
    "forwardToChat",
    "forwardToTopic",
]


def _is_fon_order(sub: dict) -> bool:
    """Заказ на фон — это подписка с непустым backdropNames (списком конкретных фонов)."""
    return bool(sub.get("backdropNames"))


def _eligible(sub: dict) -> bool:
    """Обновляем только подписки с включённым автобаем и уже заданной ценой."""
    if not sub.get("portalsAutobuy"):
        return False
    if sub.get("portalsAutobuyMaxPrice") is None:
        return False
    if sub.get("collectionName") in (None, "", "all-collections"):
        return False
    return True


def _listing_model(listing: dict) -> str | None:
    """
    Имя модели из листинга. По докам поле называется modelName, но запасные
    варианты оставлены: пустое имя модели тише всего ломает весь отбор.
    """
    for key in ("model", "modelName", "model_name"):
        value = listing.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("value")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _model_floors_of_market(listings: list) -> dict:
    """Минимальная цена листинга в разрезе моделей, в пределах одного маркета."""
    floors = {}
    for listing in listings:
        model = _listing_model(listing)
        price = listing.get("normalizedPrice")
        if model is None or price is None:
            continue
        if model not in floors or price < floors[model]:
            floors[model] = price
    return floors


def _scan_collection(client, sub: dict, account) -> tuple[float | None, dict]:
    """
    Один проход по маркетам, из которого получаем сразу две вещи:

    1. floor коллекции — медиана floor-цен среди трёх маркетов (MARKETS): для
       каждого маркета берём его собственный floor (минимальную цену листинга
       на этом маркете), а затем медиану среди этих floor-цен. Медиана
       устойчивее к разовым ценовым выбросам на одном из маркетов, чем среднее.
    2. floor каждой модели — те же листинги, сгруппированные по модели, и так
       же сведённые медианой по маркетам. Дополнительных запросов к API это не
       стоит, поэтому автоподбор моделей не увеличивает нагрузку на rate limit.

    ВАЖНО: modelNames и numberPattern подписки здесь намеренно
    игнорируются — floor считается по всей коллекции без каких-либо
    фильтров подписки, чтобы автобай ставил цену на самый дешёвый
    подарок в коллекции целиком, а не на дешёвый подарок среди узкой
    подвыборки (конкретных моделей или конкретной длины номера).

    Оговорка про модели: поиск отдаёт максимум 50 листингов, отсортированных по
    цене, поэтому здесь видны только модели у дешёвого края. Дорогие модели —
    а при отборе по премии над floor интересны именно они — доуточняются
    отдельно в _probe_model_floors.
    """
    collection = sub["collectionName"]
    sub_name = sub.get("subscriptionName", sub["_id"])
    backdrops = sub.get("backdropNames") or None

    market_floors = []
    per_market_models = []
    for market in MARKETS:
        try:
            listings = client.search_market(
                market, collection, models=None, backdrops=backdrops, number=None
            )
        except ApiError as e:
            account.record_error(f"[{sub_name}] search {market}/{collection}: {e}")
            continue
        if listings:
            market_floors.append(listings[0]["normalizedPrice"])  # самый дешёвый листинг на этом маркете
            per_market_models.append(_model_floors_of_market(listings))

    if not market_floors:
        return None, {}

    model_floors = {}
    for model in set().union(*per_market_models) if per_market_models else set():
        prices = [floors[model] for floors in per_market_models if model in floors]
        model_floors[model] = statistics.median(prices)

    return statistics.median(market_floors), model_floors


def _collection_catalog(client, collection: str, account, now: float) -> dict:
    """
    Каталог моделей коллекции: {имя: rarity}. rarity — сколько всего подарков
    этой модели выпущено, то есть чем число меньше, тем модель реже.
    Кешируется вместе с ценами: список моделей меняется ещё реже, чем цены.
    """
    cached = _CATALOG_CACHE.get(collection)
    if cached and now - cached[0] < PROBE_CACHE_HOURS * 3600:
        return cached[1]
    try:
        catalog = client.get_models(collection)
    except ApiError as e:
        account.record_error(f"get_models {collection}: {e}")
        return {}
    rarity = {}
    for item in catalog or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            rarity[name.strip()] = item.get("rarity")
    _CATALOG_CACHE[collection] = (now, rarity)
    return rarity


def _probe_model_floors(client, collection: str, known: dict, account, catalog: dict) -> dict:
    """
    Доуточняет цены моделей, не попавших в дешёвую выдачу поиска.

    Полный список моделей даёт GET /gift/models/:collection, а цену каждой —
    точечный поиск с фильтром по одной модели (первый листинг = её floor).
    Именно этот шаг стоит основных запросов за цикл, поэтому ограничен
    probe_limit, а число маркетов задаётся probe_markets (по одному быстрее
    втрое, а на сравнение с порогом и с историей точность третьего знака
    всё равно не влияет).
    """
    names = [name for name in catalog if name not in known]

    if account.probe_limit and len(names) > account.probe_limit:
        log.info("[%s] моделей для добора %d, беру %d (probe_limit)",
                 collection, len(names), account.probe_limit)
        names = names[:account.probe_limit]
    log.info("[%s] доуточняю цены %d моделей по %d маркет(ам)", collection, len(names), len(MARKETS[:max(1, account.probe_markets)]))

    primary = MARKETS[:max(1, account.probe_markets)]
    # Маркеты, до которых обычный проход не доходит. Модель, выставленная только
    # на них, раньше просто исчезала: цены нет — значит нет и строки в отчёте,
    # и понять, что модель вообще существует, было невозможно. Редкие дорогие
    # модели попадают сюда чаще прочих — их и продают редко, и лежат они не на
    # всех площадках сразу.
    fallback = [m for m in MARKETS if m not in primary]

    def floor_on(market: str, model: str):
        try:
            listings = client.search_market(market, collection, models=[model])
        except ApiError as e:
            account.record_error(f"probe {market}/{collection}/{model}: {e}")
            return None
        return listings[0]["normalizedPrice"] if listings else None

    floors = {}
    rescued, missing = [], []
    for name in names:
        prices = [p for p in (floor_on(m, name) for m in primary) if p is not None]
        if not prices:
            # добираем по оставшимся маркетам — но только для этой модели и
            # только пока не найдём хоть один листинг
            for market in fallback:
                price = floor_on(market, name)
                if price is not None:
                    prices.append(price)
                    rescued.append(name)
                    break
        if prices:
            floors[name] = statistics.median(prices)
        else:
            missing.append(name)

    if rescued:
        log.info("[%s] нашлись только на других маркетах (%d): %s",
                 collection, len(rescued), ", ".join(rescued[:8]))
    if missing:
        log.info("[%s] нет активных листингов нигде (%d): %s",
                 collection, len(missing), ", ".join(missing[:8]))
    return floors


def _oldest_ts(sales: list) -> float | None:
    """Время самой старой скачанной продажи — по нему видно, какой период закрыт."""
    stamps = [parse_sold_at(s.get("soldAt")) for s in sales]
    stamps = [t for t in stamps if t is not None]
    return min(stamps) if stamps else None


def _enough_sales(sales: list, depth: int, excluded: set, fresh_hours: float,
                  now: float) -> bool:
    """
    Выборка набрана, когда выполнено И то, и другое: пригодных сделок не меньше
    depth И закрыты последние SALES_WINDOW_DAYS. Что из двух окажется больше —
    то и определит размер выборки, но не больше SALES_MAX сделок.
    """
    got = count_eligible(sales, excluded, fresh_hours, now)
    if got < depth:
        return False
    if got >= SALES_MAX:
        return True   # больше для оценки цены не нужно
    oldest = _oldest_ts(sales)
    return oldest is not None and now - oldest >= SALES_WINDOW_DAYS * 86400


def _fetch_sales(client, collection: str, model: str, depth: int, account, now: float) -> list:
    """
    Продажи модели: не меньше `depth` пригодных И не меньше чем за последние
    SALES_WINDOW_DAYS — берём то из двух, что больше. Для редкой модели правит
    глубина (три недели дают пару сделок), для ходовой — окно (depth сделок
    укладываются в сутки и описывают не рынок, а последний день).

    pageSize у истории жёстко ограничен 20, поэтому набирается страницами с
    ранней остановкой по totalPages, когда продаж меньше запрошенного.

    Результат кешируется на HISTORY_CACHE_HOURS: медиана по сотне сделок за час
    практически не меняется, а перезапрос стоил бы по 5 запросов на модель
    каждый цикл.
    """
    excluded = {b.strip().lower() for b in account.exclude_backdrops if b.strip()}
    fresh_hours = account.fresh_hours

    cached = _HISTORY_CACHE.get((collection, model))
    if cached and now - cached[0] < HISTORY_CACHE_HOURS * 3600:
        _, cached_sales, exhausted = cached
        # кеш годится, если в нём уже набирается нужное число ПРИГОДНЫХ сделок
        # либо история кончилась — иначе аккаунт с другими настройками свежести
        # получил бы чужую, слишком короткую выборку
        if exhausted or _enough_sales(cached_sales, depth, excluded, fresh_hours, now):
            return cached_sales

    # Считаем ПРИГОДНЫЕ сделки, а не скачанные: свежие сутки и исключённые фоны
    # выбрасываются, и у ходовой модели от страницы оставалось три сделки.
    base_pages = max(1, -(-depth // HISTORY_PAGE_SIZE))  # ceil

    sales, exhausted, page = [], False, 0
    while page < HISTORY_MAX_PAGES:
        try:
            data = client.get_history(collection, models=[model], sort_by="date", page=page)
        except ApiError as e:
            account.record_error(f"history {collection}/{model} p{page}: {e}")
            break
        content = (data or {}).get("content") or []
        sales.extend(content)
        total_pages = ((data or {}).get("page") or {}).get("totalPages")
        page += 1
        if not content or (total_pages is not None and page >= total_pages):
            exhausted = True
            break
        if _enough_sales(sales, depth, excluded, fresh_hours, now):
            break

    got = count_eligible(sales, excluded, fresh_hours, now)
    oldest = _oldest_ts(sales)
    days = (now - oldest) / 86400 if oldest else 0
    if page > base_pages:
        log.info("[%s/%s] добрал до %d страниц: пригодных сделок %d, период %.1f дн.",
                 collection, model, page, got, days)
    if not exhausted and page >= HISTORY_MAX_PAGES:
        log.info("[%s/%s] упёрлись в потолок страниц: пригодных %d (нужно %d), "
                 "период %.1f дн. (нужно %d)",
                 collection, model, got, depth, days, SALES_WINDOW_DAYS)

    _HISTORY_CACHE[(collection, model)] = (now, sales, exhausted)
    return sales


def fetch_sales_for(client, collection: str, model: str, depth: int, account) -> list:
    """
    Сделки модели для внешних потребителей (сканер рынка).

    Отдельная обёртка, чтобы сканер ходил через тот же кеш и то же правило
    глубины «не меньше depth и не меньше трёх недель», что и отбор моделей —
    иначе две части бота считали бы цену по разным выборкам.
    """
    try:
        return _fetch_sales(client, collection, model, depth, account, time.time())
    except ApiError as e:
        account.record_error(f"sales {collection}/{model}: {e}")
        return []


def _select_models(client, sub: dict, floor: float, model_floors: dict, account, now: float,
                   buy_price: float | None = None) -> dict:
    """
    Полный отбор моделей для одной подписки: порог по премии, затем проверка
    каждой уцелевшей модели по истории продаж. Возвращает отчёт для /models.
    """
    collection = sub["collectionName"]
    # Добор цен зависит только от коллекции — ни от подписки, ни от аккаунта.
    # Поэтому результат кладём в общий кеш: и другие подписки на ту же
    # коллекцию, и другие аккаунты берут готовое вместо сотен своих запросов.
    catalog = _collection_catalog(client, collection, account, now)
    cached = _PROBE_CACHE.get(collection)
    if cached and now - cached[0] < PROBE_CACHE_HOURS * 3600:
        probed = cached[1]
    else:
        probed = _probe_model_floors(client, collection, model_floors, account, catalog)
        _PROBE_CACHE[collection] = (now, probed)
    all_floors = dict(model_floors)
    all_floors.update(probed)

    all_candidates = pick_candidates(all_floors, floor, account.premium_pct)
    threshold = floor * (1 + account.premium_pct / 100)

    # Модели с подозрительными символами в имени отсеиваем ДО запроса истории:
    # сервер один раз уже отклонял весь modelNames целиком из-за одной такой
    # модели ("Fool's Gold" при отправке отклонила все 7 моделей разом), а раз
    # мы её всё равно не отправим — нет смысла тратить на неё запрос к истории.
    candidates = [m for m in all_candidates if not has_suspect_chars(m)]
    bad_format = [m for m in all_candidates if has_suspect_chars(m)]

    # Диагностика: записываем ВСЕ увиденные модели, включая не дошедшие до
    # порога и отсеянные по имени. Без этого в отчёте не видно, кого и почему
    # отбор не рассматривал вовсе.
    details = {}
    for model, model_floor in all_floors.items():
        details[model] = {
            "floor": model_floor,
            "rarity": catalog.get(model),
            "status": "ниже порога",
            "premium_pct": (model_floor / floor - 1) * 100 if floor else None,
        }
    for model in bad_format:
        details[model]["status"] = "имя не принимается сервисом"

    picked, pumped, no_data = [], [], []
    for model in candidates:
        sales = _fetch_sales(client, collection, model, account.sales_depth, account, now)
        result = check_pump(sales, all_floors[model], threshold, account.tol_pct,
                            account.min_sales, account.fresh_hours, now,
                            set(account.exclude_backdrops), account.ref_percentile,
                            buy_price)
        details[model].update(result)
        details[model]["sales_total"] = len(sales)
        if result["verdict"] == "ok":
            picked.append(model)
            details[model]["status"] = "взята"
        elif result["verdict"] == "pump":
            pumped.append(model)
            details[model]["status"] = "отсев: по сделкам порог не проходит"
        else:
            no_data.append(model)
            details[model]["status"] = "отсев: мало сделок для проверки"

    return {
        "picked": trim_to_limit(picked),
        "pumped": pumped,
        "no_data": no_data,
        "bad_format": bad_format,
        "seen": len(all_floors),
        "candidates": len(candidates),
        "threshold": threshold,
        "buy_price": buy_price,
        "details": details,
    }


def run_cycle(account, force_models: bool = False, wait_seconds: float = 0.0) -> bool:
    """
    account: state.AccountState. Синхронная функция — вызывать через asyncio.to_thread из бота.

    Цикл состоит из двух фаз с разной частотой: цены пересчитываются каждый раз
    (быстро), а состав моделей — раз в models_interval_h или по force_models.
    Возвращает False, если цикл пропущен, потому что предыдущий ещё не закончился.
    wait_seconds > 0 — столько ждать освобождения вместо немедленного отказа.
    """
    # Плановый запуск при занятой блокировке просто пропускаем — он всё равно
    # повторится по расписанию. А вот команда от человека ждёт: цикл теперь идёт
    # минутами, и отказ «повтори позже» приходил почти на каждое нажатие, тем
    # более что сразу после деплоя плановый цикл стартует через 10 секунд.
    if wait_seconds > 0:
        acquired = _CYCLE_LOCK.acquire(timeout=wait_seconds)
    else:
        acquired = _CYCLE_LOCK.acquire(blocking=False)
    if not acquired:
        log.warning("[%s] предыдущий цикл ещё идёт — пропускаю запуск", account.name)
        return False
    try:
        _run_cycle_locked(account, force_models)
    finally:
        _CYCLE_LOCK.release()
    return True


def _models_due(account, now: float, force: bool) -> bool:
    """
    Состав моделей пересматривается по своему, редкому расписанию: он меняется
    медленно, а полный проход по всем моделям стоит сотен запросов. Цены при
    этом обновляются каждым циклом, как и раньше.
    """
    if account.models_mode == "off":
        return False
    if force or account.last_models_ts is None:
        return True
    return now - account.last_models_ts >= account.models_interval_h * 3600


def _push_subscription(client, sub: dict, account, new_price: float, new_models=None) -> bool:
    """Отправляет PUT с новой ценой и (если задан) новым набором моделей."""
    body = {f: sub.get(f) for f in SUBSCRIPTION_BODY_FIELDS}
    body["portalsAutobuyMaxPrice"] = new_price
    if new_models is not None:
        body["modelNames"] = new_models
    # numberPattern: сервер валидирует regex ^[A-Za-z0-9]+$ — пустая
    # строка/null его не проходят, поэтому при отсутствии паттерна
    # поле нужно не отправлять вовсе, а не слать "" или null.
    if not body.get("numberPattern"):
        body.pop("numberPattern", None)
    try:
        client.update_subscription(sub["_id"], body)
        return True
    except ApiError as e:
        # при ошибке именно из-за modelNames показываем сами модели — иначе
        # причину пришлось бы гадать вслепую, не видя, что было отправлено
        extra = ""
        if new_models is not None and "модел" in str(e).lower():
            preview = ", ".join(new_models[:15]) + (f" … ещё {len(new_models) - 15}" if len(new_models) > 15 else "")
            extra = f" | отправлено моделей: {len(new_models)}: {preview}"
        account.record_error(f"[{sub.get('subscriptionName', sub['_id'])}] update_subscription: {e}{extra}")
        return False


def _run_cycle_locked(account, force_models: bool):
    now = time.time()
    account.last_run_ts = now
    account.errors.clear()  # буфер /errors отражает только текущий цикл, а не всю историю
    updated = 0
    skipped = 0
    client = account.client
    client.request_count = 0
    markup_mult_model = 1 + account.markup_pct / 100
    markup_mult_fon = 1 + account.markup_pct_fon / 100

    try:
        subs = client.get_subscriptions()
    except ApiError as e:
        account.record_error(f"get_subscriptions: {e}")
        account.last_updated_count = 0
        account.last_skipped_count = 0
        account.last_requests = client.request_count
        return

    # --- Фаза 1: цены. Быстрая, идёт каждый цикл, 3 запроса на подписку. ---
    scans = {}
    for sub in subs:
        if not _eligible(sub):
            continue

        name = sub.get("subscriptionName", sub["_id"])
        floor, model_floors = _scan_collection(client, sub, account)
        if floor is None:
            log.info("[%s/%s] нет активных листингов под фильтр — пропуск", account.name, name)
            skipped += 1
            continue

        is_fon = _is_fon_order(sub)
        markup_mult = markup_mult_fon if is_fon else markup_mult_model
        markup_pct = account.markup_pct_fon if is_fon else account.markup_pct
        new_price = round(floor * markup_mult, 2)
        old_price = sub.get("portalsAutobuyMaxPrice")
        scans[sub["_id"]] = (sub, floor, model_floors, new_price)

        if old_price is not None and abs(new_price - old_price) < MIN_DELTA:
            skipped += 1
            continue
        if _push_subscription(client, sub, account, new_price):
            updated += 1
            log.info("[%s/%s] цена (%s): %s -> %s TON (floor %.2f, +%.1f%%)",
                     account.name, name, "фон" if is_fon else "модель",
                     old_price, new_price, floor, markup_pct)

    # --- Фаза 2: состав моделей. Медленная, идёт раз в models_interval_h. ---
    if _models_due(account, now, force_models):
        account.last_models = {}
        picked_total = 0
        log.info("[%s] пересматриваю состав моделей (режим %s)", account.name, account.models_mode)
        for sub, floor, model_floors, new_price in scans.values():
            if _is_fon_order(sub):
                continue  # фоны не трогаем: их листинги отфильтрованы по backdropNames
            name = sub.get("subscriptionName", sub["_id"])
            errors_before = account.error_count
            report = _select_models(client, sub, floor, model_floors, account, now, new_price)
            report["errors"] = account.error_count - errors_before
            report["applied"] = False
            report["collection"] = sub.get("collectionName")
            # что именно изменится в заказе по сравнению с тем, что там стоит сейчас
            was = set(sub.get("modelNames") or [])
            now_set = set(report["picked"])
            # при пустом отборе состав не трогаем, значит и изменений нет
            report["added"] = sorted(now_set - was) if report["picked"] else []
            report["removed"] = sorted(was - now_set) if report["picked"] else []
            report["kept"] = len(now_set & was) if report["picked"] else len(was)
            account.last_models[name] = report
            picked_total += len(report["picked"])

            if account.models_mode != "on":
                continue
            if not report["picked"]:
                # пустой modelNames означает ВСЕ модели коллекции (см. доки),
                # поэтому при пустом отборе набор моделей оставляем как есть
                log.warning("[%s/%s] отбор не дал ни одной модели — modelNames не трогаем",
                            account.name, name)
                continue
            if report["errors"]:
                # Упавший запрос = модель без цены, а модель без цены выпадает из
                # отбора молча и была бы вычеркнута из живого заказа. При сбое
                # сервиса так вычёркивается сразу десятками, поэтому состав не
                # переписываем: устаревший список лучше обрезанного случайно.
                log.warning("[%s/%s] во время отбора упало запросов: %d — "
                            "modelNames не трогаю, чтобы не вычеркнуть модели без цены",
                            account.name, name, report["errors"])
                continue
            if sorted(report["picked"]) == sorted(sub.get("modelNames") or []):
                continue  # состав не изменился, PUT не нужен
            # запоминаем ручной список до первой перезаписи — вернуть его иначе неоткуда
            account.original_models.setdefault(sub["_id"], sub.get("modelNames") or [])
            if _push_subscription(client, sub, account, new_price, report["picked"]):
                report["applied"] = True
                updated += 1
                log.info("[%s/%s] состав моделей обновлён: %d шт.",
                         account.name, name, len(report["picked"]))

        account.last_models_ts = now
        log.info("[%s] пересмотр моделей закончен, подобрано суммарно %d моделей", account.name, picked_total)

    account.last_updated_count = updated
    account.last_skipped_count = skipped
    account.last_requests = client.request_count
    log.info("[%s] цикл завершён: обновлено %d, пропущено %d, запросов к API %d, заняло %.0f сек",
             account.name, updated, skipped, client.request_count, time.time() - now)

