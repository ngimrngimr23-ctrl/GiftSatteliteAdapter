"""
Отбор моделей в заказ: премия над floor коллекции + проверка премии по реальной
истории продаж (POST /history/:collection).

Стратегия. Цена в подписке одна и считается по floor ВСЕЙ коллекции, поэтому
сработавший ордер на модель, которая сама стоит заметно дороже floor, — это
покупка сильно ниже её собственного рынка. Значит:

1. Кандидат — модель, чей текущий floor не ниже floor коллекции с премией
   premium_pct (порог, а не полоса: чем дороже модель, тем выгоднее).
2. Премия должна быть настоящей, а не нарисованной пампом: медиана цен по
   истории продаж модели должна быть не сильно ниже её текущей цены.

Проверка односторонняя: модель, торгующаяся ДЕШЕВЛЕ своей истории, — это
просадка, а не памп, такую берём.
"""
import logging
import statistics
from datetime import datetime, timezone

log = logging.getLogger("model_picker")

MAX_MODEL_NAMES = 100  # жёсткий лимит modelNames в подписке (см. POST /user/subscribe)


def parse_sold_at(value) -> float | None:
    """soldAt приходит как ISO 8601 с 'Z' — переводим в unix-время."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pick_candidates(model_floors: dict, collection_floor: float, premium_pct: float) -> list:
    """
    Модели с премией не ниже premium_pct над floor коллекции, от дорогих к дешёвым
    (первыми идут самые выгодные — важно при обрезке до MAX_MODEL_NAMES).
    """
    if not collection_floor:
        return []
    threshold = collection_floor * (1 + premium_pct / 100)
    above = [(floor, model) for model, floor in model_floors.items() if floor >= threshold]
    above.sort(reverse=True)
    return [model for _, model in above]


def check_pump(sales: list, current_floor: float, tol_pct: float,
               min_sales: int, fresh_hours: float, now: float) -> dict:
    """
    Настоящая ли премия модели. Возвращает
    {"verdict": "ok"|"pump"|"no_data", "median", "used", "fresh_skipped"}.

    Из базы исключаются самые свежие продажи (моложе fresh_hours): если памп уже
    успел набить сделок, они бы подтянули медиану к пампнутой цене и спрятали
    его. Даты берём из soldAt, дополнительных запросов это не стоит.
    """
    prices = []
    fresh_skipped = 0
    for sale in sales:
        price = sale.get("normalizedPrice")
        if price is None:
            continue
        sold_ts = parse_sold_at(sale.get("soldAt"))
        if sold_ts is not None and now - sold_ts < fresh_hours * 3600:
            fresh_skipped += 1
            continue
        prices.append(price)

    if len(prices) < min_sales:
        return {"verdict": "no_data", "median": None, "used": len(prices), "fresh_skipped": fresh_skipped}

    median = statistics.median(prices)
    # односторонне: current < median — это просадка, а не памп
    verdict = "pump" if median > 0 and current_floor > median * (1 + tol_pct / 100) else "ok"
    return {"verdict": verdict, "median": median, "used": len(prices), "fresh_skipped": fresh_skipped}


def trim_to_limit(models: list) -> list:
    """modelNames в подписке — максимум 100 (пустой массив означал бы ВСЕ модели)."""
    if len(models) <= MAX_MODEL_NAMES:
        return models
    log.warning("моделей %d, обрезаю до %d (лимит подписки)", len(models), MAX_MODEL_NAMES)
    return models[:MAX_MODEL_NAMES]
