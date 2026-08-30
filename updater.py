import time
import logging
import statistics
import threading

from api_client import ApiError, HISTORY_PAGE_SIZE
from model_picker import check_pump, has_suspect_chars, pick_candidates, trim_to_limit

log = logging.getLogger("updater")

MARKETS = ["portals", "tonnel", "mrkt"]  # где смотрим актуальную цену
MIN_DELTA = 0.02  # не дёргать PUT, если цена изменилась меньше чем на столько TON
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


def _probe_model_floors(client, collection: str, known: dict, account) -> dict:
    """
    Доуточняет цены моделей, не попавших в дешёвую выдачу поиска.

    Полный список моделей даёт GET /gift/models/:collection, а цену каждой —
    точечный поиск с фильтром по одной модели (первый листинг = её floor).
    Именно этот шаг стоит основных запросов за цикл, поэтому ограничен
    probe_limit, а число маркетов задаётся probe_markets (по одному быстрее
    втрое, а на сравнение с порогом и с историей точность третьего знака
    всё равно не влияет).
    """
    try:
        catalog = client.get_models(collection)
    except ApiError as e:
        account.record_error(f"get_models {collection}: {e}")
        return {}

    names = []
    for item in catalog or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip() and name.strip() not in known:
            names.append(name.strip())

    if account.probe_limit and len(names) > account.probe_limit:
        log.info("[%s] моделей для добора %d, беру %d (probe_limit)",
                 collection, len(names), account.probe_limit)
        names = names[:account.probe_limit]
    log.info("[%s] доуточняю цены %d моделей по %d маркет(ам)", collection, len(names), len(MARKETS[:max(1, account.probe_markets)]))

    markets = MARKETS[:max(1, account.probe_markets)]
    floors = {}
    for name in names:
        prices = []
        for market in markets:
            try:
                listings = client.search_market(market, collection, models=[name])
            except ApiError as e:
                account.record_error(f"probe {market}/{collection}/{name}: {e}")
                continue
            if listings:
                prices.append(listings[0]["normalizedPrice"])
        if prices:
            floors[name] = statistics.median(prices)
    return floors


def _fetch_sales(client, collection: str, model: str, depth: int, account, now: float) -> list:
    """
    Последние `depth` продаж модели. pageSize у истории жёстко ограничен 20,
    поэтому глубина набирается страницами, с ранней остановкой по totalPages,
    когда продаж меньше запрошенного.

    Результат кешируется на HISTORY_CACHE_HOURS: медиана по сотне сделок за час
    практически не меняется, а перезапрос стоил бы по 5 запросов на модель
    каждый цикл.
    """
    cached = _HISTORY_CACHE.get((collection, model))
    if cached and now - cached[0] < HISTORY_CACHE_HOURS * 3600:
        return cached[1]

    sales = []
    pages = max(1, -(-depth // HISTORY_PAGE_SIZE))  # ceil
    for page in range(pages):
        try:
            data = client.get_history(collection, models=[model], sort_by="date", page=page)
        except ApiError as e:
            account.record_error(f"history {collection}/{model} p{page}: {e}")
            break
        content = (data or {}).get("content") or []
        sales.extend(content)
        total_pages = ((data or {}).get("page") or {}).get("totalPages")
        if not content or (total_pages is not None and page + 1 >= total_pages):
            break

    sales = sales[:depth]
    _HISTORY_CACHE[(collection, model)] = (now, sales)
    return sales


def _select_models(client, sub: dict, floor: float, model_floors: dict, account, now: float) -> dict:
    """
    Полный отбор моделей для одной подписки: порог по премии, затем проверка
    каждой уцелевшей модели по истории продаж. Возвращает отчёт для /models.
    """
    collection = sub["collectionName"]
    # Добор цен зависит только от коллекции — ни от подписки, ни от аккаунта.
    # Поэтому результат кладём в общий кеш: и другие подписки на ту же
    # коллекцию, и другие аккаунты берут готовое вместо сотен своих запросов.
    cached = _PROBE_CACHE.get(collection)
    if cached and now - cached[0] < PROBE_CACHE_HOURS * 3600:
        probed = cached[1]
    else:
        probed = _probe_model_floors(client, collection, model_floors, account)
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

    picked, pumped, no_data = [], [], []
    details = {}
    for model in candidates:
        sales = _fetch_sales(client, collection, model, account.sales_depth, account, now)
        result = check_pump(sales, all_floors[model], threshold, account.tol_pct,
                            account.min_sales, account.fresh_hours, now,
                            set(account.exclude_backdrops))
        details[model] = {"floor": all_floors[model], **result}
        if result["verdict"] == "ok":
            picked.append(model)
        elif result["verdict"] == "pump":
            pumped.append(model)
        else:
            no_data.append(model)

    return {
        "picked": trim_to_limit(picked),
        "pumped": pumped,
        "no_data": no_data,
        "bad_format": bad_format,
        "seen": len(all_floors),
        "candidates": len(candidates),
        "threshold": threshold,
        "details": details,
    }


def run_cycle(account, force_models: bool = False) -> bool:
    """
    account: state.AccountState. Синхронная функция — вызывать через asyncio.to_thread из бота.

    Цикл состоит из двух фаз с разной частотой: цены пересчитываются каждый раз
    (быстро), а состав моделей — раз в models_interval_h или по force_models.
    Возвращает False, если цикл пропущен, потому что предыдущий ещё не закончился.
    """
    if not _CYCLE_LOCK.acquire(blocking=False):
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
            report = _select_models(client, sub, floor, model_floors, account, now)
            report["applied"] = False
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

