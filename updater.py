import time
import logging
import statistics

from api_client import ApiError
from model_picker import analyze_models, pick_models, prune_history
from state import load_price_history, save_price_history

log = logging.getLogger("updater")

MARKETS = ["portals", "tonnel", "mrkt"]  # где смотрим актуальную цену
MIN_DELTA = 0.02  # не дёргать PUT, если цена изменилась меньше чем на столько TON

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
    """Имя модели из листинга. Точное имя поля в разных маркетах может отличаться, поэтому пробуем варианты."""
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

    Оговорка про модели: маркет отдаёт не весь стакан, а страницу самых
    дешёвых листингов, поэтому видны только модели, представленные у дешёвого
    края. Для выбора моделей в заказ этого и достаточно — именно они и могут
    сработать по цене заказа.
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


def check_purchases(account) -> list[dict]:
    """
    Проверяет GET /user/purchases и возвращает новые (ещё не виденные) покупки
    в хронологическом порядке (сначала старые). Обновляет account.last_purchase_ts,
    но не сохраняет состояние — save_persisted должен вызвать вызывающий код.

    На самом первом запуске (last_purchase_ts ещё не задан) ничего не возвращает —
    просто запоминает текущую последнюю покупку, чтобы не спамить всей историей.
    """
    try:
        data = account.client.get_purchases(page=0)
    except ApiError as e:
        account.record_error(f"get_purchases: {e}")
        return []

    purchases = (data or {}).get("purchases", [])
    if not purchases:
        return []

    # сортируем сами — сортировка ответа сервером явно не гарантирована в доках
    purchases = sorted(purchases, key=lambda p: p.get("timestamp", ""), reverse=True)

    if account.last_purchase_ts is None:
        account.last_purchase_ts = purchases[0].get("timestamp")
        return []

    new_ones = [p for p in purchases if p.get("timestamp", "") > account.last_purchase_ts]
    if not new_ones:
        return []

    account.last_purchase_ts = purchases[0].get("timestamp")
    return list(reversed(new_ones))  # старые сначала


def run_cycle(account):
    """account: state.AccountState. Синхронная функция — вызывать через asyncio.to_thread из бота."""
    account.last_run_ts = time.time()
    account.errors.clear()  # буфер /errors отражает только текущий цикл, а не всю историю
    account.last_models = {}
    updated = 0
    skipped = 0
    client = account.client
    markup_mult_model = 1 + account.markup_pct / 100
    markup_mult_fon = 1 + account.markup_pct_fon / 100
    history = None  # грузим лениво: если подходящих подписок нет, лишний запрос в Upstash не нужен
    history_dirty = False

    try:
        subs = client.get_subscriptions()
    except ApiError as e:
        account.record_error(f"get_subscriptions: {e}")
        account.last_updated_count = 0
        account.last_skipped_count = 0
        return

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

        new_models = None
        # Историю ведём только для заказов на модели: у заказа на фон листинги
        # отфильтрованы по backdropNames, и его floor-цены моделей несопоставимы
        # с ценами той же коллекции без фильтра — смешивать их в одну серию нельзя.
        if not is_fon and model_floors:
            if history is None:
                history = load_price_history()
            history_dirty = True
            stats = analyze_models(
                history, sub["collectionName"], model_floors, account.last_run_ts,
                account.pump_window_h, account.pump_pct, account.pump_cooldown_h,
            )
            picked, pumped, too_expensive = pick_models(stats, floor, account.model_count)
            account.last_models[name] = {
                "picked": picked,
                "pumped": pumped,
                "too_expensive": len(too_expensive),
                "seen": len(model_floors),
                "applied": bool(picked) and account.auto_models,
                "stats": stats,
            }
            if account.auto_models:
                if picked:
                    new_models = picked
                else:
                    # пустой modelNames = заказ на всю коллекцию; это не то, чего просили,
                    # поэтому оставляем прежний набор моделей нетронутым
                    log.warning("[%s/%s] автоподбор не дал ни одной модели — modelNames не трогаем",
                                account.name, name)

        new_price = round(floor * markup_mult, 2)
        old_price = sub.get("portalsAutobuyMaxPrice")
        models_changed = new_models is not None and sorted(new_models) != sorted(sub.get("modelNames") or [])
        if old_price is not None and abs(new_price - old_price) < MIN_DELTA and not models_changed:
            skipped += 1
            continue

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
            updated += 1
            log.info("[%s/%s] обновлено (%s): %s -> %s TON (floor %.2f, +%.1f%%)%s",
                      account.name, name, "фон" if is_fon else "модель", old_price, new_price, floor, markup_pct,
                      f", моделей: {len(new_models)}" if new_models is not None else "")
        except ApiError as e:
            account.record_error(f"[{name}] update_subscription: {e}")

    if history_dirty:
        prune_history(history, account.last_run_ts)
        save_price_history(history)

    account.last_updated_count = updated
    account.last_skipped_count = skipped

