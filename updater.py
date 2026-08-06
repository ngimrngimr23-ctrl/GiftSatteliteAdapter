import time
import logging

from api_client import ApiError

log = logging.getLogger("updater")

MARKETS = ["portals", "tonnel", "mrkt"]  # где смотрим актуальную цену
MIN_DELTA = 0.02  # не дёргать PUT, если цена изменилась меньше чем на столько TON
NUMBER_MAX_LEN_SEARCH = 6  # /search ограничивает numberPattern 6 символами

# Поля, которые нужно переслать обратно в PUT /user/update-subscription/:id
# (тело идентично POST /user/subscribe)
SUBSCRIPTION_BODY_FIELDS = [
    "subscriptionName",
    "collectionName",
    "modelNames",
    "backdropNames",
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

# Поля-массивы: сервер требует, чтобы они присутствовали в теле, но не как null.
ARRAY_FIELDS = {"modelNames", "backdropNames"}


def _build_update_body(sub: dict, new_price: float) -> dict:
    """
    Собирает тело PUT-запроса из данных подписки.
    - Массивы всегда включаются в тело (null -> []).
    - Остальные необязательные поля со значением None не включаются вообще,
      т.к. валидатор Fastify на бэкенде, похоже, не разрешает null для них.
    """
    body = {}
    for field in SUBSCRIPTION_BODY_FIELDS:
        value = sub.get(field)
        if field in ARRAY_FIELDS:
            body[field] = value if value is not None else []
        elif value is not None:
            body[field] = value
        # если value is None и поле не массив — просто не добавляем его в body

    body["portalsAutobuyMaxPrice"] = new_price
    return body


def _eligible(sub: dict) -> bool:
    """Обновляем только подписки с включённым автобаем и уже заданной ценой."""
    if not sub.get("portalsAutobuy"):
        return False
    if sub.get("portalsAutobuyMaxPrice") is None:
        return False
    if sub.get("collectionName") in (None, "", "all-collections"):
        return False
    return True


def _current_floor(client, sub: dict, account) -> float | None:
    collection = sub["collectionName"]
    sub_name = sub.get("subscriptionName", sub["_id"])
    models = sub.get("modelNames") or None
    backdrops = sub.get("backdropNames") or None
    number = sub.get("numberPattern")
    if number and len(number) > NUMBER_MAX_LEN_SEARCH:
        number = None

    best = None
    for market in MARKETS:
        try:
            listings = client.search_market(
                market, collection, models=models, backdrops=backdrops, number=number
            )
        except ApiError as e:
            account.record_error(f"[{sub_name}] search {market}/{collection}: {e}")
            continue
        if listings:
            price = listings[0]["normalizedPrice"]
            if best is None or price < best:
                best = price
    return best


def run_cycle(account):
    """account: state.AccountState. Синхронная функция — вызывать через asyncio.to_thread из бота."""
    account.last_run_ts = time.time()
    updated = 0
    skipped = 0
    client = account.client
    markup_mult = 1 + account.markup_pct / 100

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
        floor = _current_floor(client, sub, account)
        if floor is None:
            log.info("[%s/%s] нет активных листингов под фильтр — пропуск", account.name, name)
            skipped += 1
            continue

        new_price = round(floor * markup_mult, 2)
        old_price = sub.get("portalsAutobuyMaxPrice")
        if old_price is not None and abs(new_price - old_price) < MIN_DELTA:
            skipped += 1
            continue

        body = _build_update_body(sub, new_price)

        try:
            client.update_subscription(sub["_id"], body)
            updated += 1
            log.info("[%s/%s] обновлено: %s -> %s TON (floor %.2f, +%.1f%%)",
                      account.name, name, old_price, new_price, floor, account.markup_pct)
        except ApiError as e:
            account.record_error(f"[{name}] update_subscription: {e}")

    account.last_updated_count = updated
    account.last_skipped_count = skipped
