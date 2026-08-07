import time
import logging
import statistics

from api_client import ApiError

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
    """
    Цена по коллекции в целом, взятая как медиана floor-цен среди трёх
    маркетов (MARKETS): для каждого маркета берём его собственный floor
    (минимальную цену листинга на этом маркете), а затем берём медиану
    среди этих floor-цен. Медиана устойчивее к разовым ценовым выбросам
    на одном из маркетов, чем среднее.

    ВАЖНО: modelNames и numberPattern подписки здесь намеренно
    игнорируются — floor считается по всей коллекции без каких-либо
    фильтров подписки, чтобы автобай ставил цену на самый дешёвый
    подарок в коллекции целиком, а не на дешёвый подарок среди узкой
    подвыборки (конкретных моделей или конкретной длины номера).
    """
    collection = sub["collectionName"]
    sub_name = sub.get("subscriptionName", sub["_id"])
    backdrops = sub.get("backdropNames") or None

    market_floors = []
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

    if not market_floors:
        return None
    return statistics.median(market_floors)


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

        body = {f: sub.get(f) for f in SUBSCRIPTION_BODY_FIELDS}
        body["portalsAutobuyMaxPrice"] = new_price
        # numberPattern: сервер валидирует regex ^[A-Za-z0-9]+$ — пустая
        # строка/null его не проходят, поэтому при отсутствии паттерна
        # поле нужно не отправлять вовсе, а не слать "" или null.
        if not body.get("numberPattern"):
            body.pop("numberPattern", None)

        try:
            client.update_subscription(sub["_id"], body)
            updated += 1
            log.info("[%s/%s] обновлено: %s -> %s TON (floor %.2f, +%.1f%%)",
                      account.name, name, old_price, new_price, floor, account.markup_pct)
        except ApiError as e:
            account.record_error(f"[{name}] update_subscription: {e}")

    account.last_updated_count = updated
    account.last_skipped_count = skipped
    
