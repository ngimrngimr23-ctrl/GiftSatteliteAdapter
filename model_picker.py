"""
Автоподбор моделей для заказа + отсев моделей, которые недавно пампанулись.

У API нет истории цен, поэтому историю бот копит сам: каждый цикл на каждую
модель коллекции пишется снапшот [timestamp, floor]. Памп определяется как
превышение текущего floor над медианой снапшотов за окно (pump_window_h).
"""
import logging
import statistics

log = logging.getLogger("model_picker")

MIN_PUMP_SAMPLES = 3  # меньше — статистики не хватает, памп не считаем
HISTORY_MAX_POINTS = 60  # сколько снапшотов на модель храним максимум
HISTORY_TTL_HOURS = 72  # снапшоты старше — выкидываем
MAX_OVER_FLOOR_PCT = 100.0  # модель дороже floor коллекции более чем на столько % в заказ не попадает


def _entry(history: dict, collection: str, model: str) -> dict:
    """История хранится вложенно {collection: {model: {"p": [[ts, price]], "b": ban_until_ts}}}."""
    return history.setdefault(collection, {}).setdefault(model, {"p": [], "b": 0})


def analyze_models(history, collection, model_floors, now, window_h, pump_pct, cooldown_h) -> dict:
    """
    Дописывает текущие floor-цены моделей в историю и возвращает по каждой модели:
    {"floor", "baseline", "pumped", "just_pumped", "until", "samples"}.

    baseline считается ДО добавления текущей точки, иначе свежий памп сам бы
    поднимал планку, с которой сравнивается. Пампанувшая модель помечается
    баном до now + cooldown_h: без этого через окно медиана подтягивается к
    новой цене, памп «рассасывается» и модель вернулась бы в заказ слишком рано.
    """
    stats = {}
    for model, mfloor in model_floors.items():
        entry = _entry(history, collection, model)
        window_pts = [pt for pt in entry.get("p", []) if now - pt[0] <= window_h * 3600]
        prices = [pt[1] for pt in window_pts]

        baseline = statistics.median(prices) if len(prices) >= MIN_PUMP_SAMPLES else None
        just_pumped = baseline is not None and baseline > 0 and mfloor > baseline * (1 + pump_pct / 100)
        if just_pumped:
            entry["b"] = max(entry.get("b", 0), now + cooldown_h * 3600)
            log.info("[%s/%s] памп: floor %.2f против медианы %.2f за %.0fч — исключаем",
                     collection, model, mfloor, baseline, window_h)

        pts = entry.get("p", []) + [[now, mfloor]]
        entry["p"] = [pt for pt in pts if now - pt[0] <= HISTORY_TTL_HOURS * 3600][-HISTORY_MAX_POINTS:]

        stats[model] = {
            "floor": mfloor,
            "baseline": baseline,
            "pumped": entry.get("b", 0) > now,
            "just_pumped": just_pumped,
            "until": entry.get("b", 0),
            "samples": len(prices),
        }
    return stats


def pick_models(stats: dict, collection_floor, count: int, band_pct: float = MAX_OVER_FLOOR_PCT):
    """
    Возвращает (picked, pumped, too_expensive):
      picked — до count самых дешёвых моделей, не помеченных пампом;
      pumped — отсеянные как недавно пампанувшие;
      too_expensive — отсеянные как слишком дорогие относительно floor коллекции.

    Берём именно дешёвый конец, потому что цена в заказе одна на всю подписку и
    считается по floor всей коллекции: модели заметно дороже этого floor всё
    равно никогда не сработают, только засоряют фильтр подписки.
    """
    candidates = []
    pumped = []
    too_expensive = []
    for model, st in stats.items():
        if st["pumped"]:
            pumped.append(model)
            continue
        if collection_floor and st["floor"] > collection_floor * (1 + band_pct / 100):
            too_expensive.append(model)
            continue
        candidates.append((st["floor"], model))

    candidates.sort()
    picked = [model for _, model in candidates[:count]]
    return picked, pumped, too_expensive


def prune_history(history: dict, now: float):
    """Выкидывает протухшие снапшоты и пустые записи, чтобы значение в Upstash не росло бесконечно."""
    for collection in list(history):
        models = history[collection]
        for model in list(models):
            entry = models[model]
            pts = [pt for pt in entry.get("p", []) if now - pt[0] <= HISTORY_TTL_HOURS * 3600]
            if not pts and entry.get("b", 0) <= now:
                del models[model]
                continue
            entry["p"] = pts
        if not models:
            del history[collection]
