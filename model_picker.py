"""
Отбор моделей в заказ: премия над floor коллекции + проверка премии по реальной
истории продаж (POST /history/:collection).

Стратегия. Цена в подписке одна и считается по floor ВСЕЙ коллекции, поэтому
сработавший ордер на модель, которая сама стоит заметно дороже floor, — это
покупка сильно ниже её собственного рынка. Значит:

1. Кандидат — модель, чей текущий floor не ниже floor коллекции с премией
   premium_pct (порог, а не полоса: чем дороже модель, тем выгоднее).
2. Премия должна быть настоящей, а не нарисованной пампом: опорная цена по
   истории продаж модели должна быть не сильно ниже её текущей цены.

Проверка односторонняя: модель, торгующаяся ДЕШЕВЛЕ своей истории, — это
просадка, а не памп, такую берём.
"""
import logging
import statistics
from datetime import datetime, timezone

log = logging.getLogger("model_picker")

MAX_MODEL_NAMES = 100  # жёсткий лимит modelNames в подписке (см. POST /user/subscribe)

# Живой случай: сервис отдал в каталоге модель "Fool's Gold" (с апострофом), но
# его же валидатор на PUT /update-subscription отклонил ВЕСЬ modelNames с 400
# "Неверный формат моделей" — рассинхрон между каталогом и валидатором на их
# стороне. Раз проверка идёт по всему списку разом, одна такая модель обрушивает
# все остальные, поэтому подозрительные имена отсеиваем до отправки.
SUSPECT_CHARS = set("'\"’‘“”★☆%/\\#@!$^&*+=<>{}[]|~`")

# Опорная цена модели берётся не по середине ряда продаж, а по нижней его части.
# Причина: ордер срабатывает на САМОМ ДЕШЁВОМ листинге, значит достаётся нам
# всегда низ распределения. Медиана описывает средний экземпляр и потому
# систематически завышает то, что реально приедет. 20-й процентиль отбрасывает
# примерно 4 самые дешёвые продажи из 20 — этого хватает, чтобы разовый
# панический слив не задавал цену, но оценка остаётся в дешёвой зоне.
REFERENCE_PERCENTILE = 20.0


def has_suspect_chars(name: str) -> bool:
    return any(ch in SUSPECT_CHARS for ch in name)


def percentile(values: list, p: float) -> float:
    """P-й процентиль с линейной интерполяцией. p=50 даёт обычную медиану."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * p / 100
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def share_above(prices: list, level: float) -> float | None:
    """Доля сделок, прошедших дороже level. Именно этим числом предлагается
    заменить одну опорную точку: оно описывает весь ряд сделок сразу и почти
    не шатается от одной мусорной продажи (одна сделка из 16 — это 6%)."""
    if not prices or level is None or level <= 0:
        return None
    return sum(1 for p in prices if p > level) / len(prices) * 100


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


def check_pump(sales: list, current_floor: float, threshold: float, tol_pct: float,
               min_sales: int, fresh_hours: float, now: float,
               exclude_backdrops: set | None = None,
               ref_percentile: float = REFERENCE_PERCENTILE,
               buy_price: float | None = None) -> dict:
    """
    Настоящая ли премия модели. Возвращает
    {"verdict": "ok"|"pump"|"no_data", "ref_price", "inflated", "used", "fresh_skipped"}.

    Из базы исключаются самые свежие продажи (моложе fresh_hours): если памп уже
    успел набить сделок, они бы подтянули опорную цену вверх и спрятали его.
    Даты берём из soldAt, дополнительных запросов это не стоит.

    Ключевой момент: задранная текущая цена сама по себе НЕ повод выбросить
    модель. Платим мы цену ордера, а не цену чужого листинга, поэтому важно
    одно — сколько модель стоит на самом деле. Если опорная цена всё равно
    выше порога, модель законная, даже когда прямо сейчас её выставили втрое
    дороже обычного. Отсеиваем только тот случай, ради которого проверка и
    затевалась: без пампа модель порог не проходит, то есть в список она
    попала бы исключительно из-за задранной цены.
    """
    # Опорная цена устойчива к паре дорогих продаж, но когда редкий фон занимает
    # заметную долю сделок, он её всё же тянет вверх — такие фоны можно исключить.
    excluded = {b.strip().lower() for b in (exclude_backdrops or set()) if b.strip()}
    prices = []
    fresh_skipped = 0
    backdrop_skipped = 0
    for sale in sales:
        price = sale.get("normalizedPrice")
        if price is None:
            continue
        backdrop = (sale.get("backdropName") or "").strip().lower()
        if excluded and backdrop in excluded:
            backdrop_skipped += 1
            continue
        sold_ts = parse_sold_at(sale.get("soldAt"))
        if sold_ts is not None and now - sold_ts < fresh_hours * 3600:
            fresh_skipped += 1
            continue
        prices.append(price)

    if len(prices) < min_sales:
        return {"verdict": "no_data", "ref_price": None, "inflated": False,
                "used": len(prices), "fresh_skipped": fresh_skipped,
                "backdrop_skipped": backdrop_skipped}

    ref_price = percentile(prices, ref_percentile)

    # --- дальше только диагностика, на вердикт она не влияет ---
    # разброс цен внутри модели: широкий обычно означает, что цену сильно
    # двигают фон и символ, а не сама модель
    p20, p80 = percentile(prices, 20), percentile(prices, 80)
    # дрейф: если дешёвые продажи в основном СТАРЫЕ, значит дорожала вся
    # коллекция; если старые и новые стоят одинаково — разброс не от времени
    dated = [(parse_sold_at(s.get("soldAt")), s.get("normalizedPrice"))
             for s in sales if s.get("normalizedPrice") is not None]
    dated = sorted([(t, p) for t, p in dated if t is not None])
    drift_pct = None
    if len(dated) >= 6:
        half = len(dated) // 2
        old_med = statistics.median([p for _, p in dated[:half]])
        new_med = statistics.median([p for _, p in dated[half:]])
        if old_med > 0:
            drift_pct = (new_med / old_med - 1) * 100
    span_days = (dated[-1][0] - dated[0][0]) / 86400 if len(dated) >= 2 else None
    # как часто модель вообще торгуется: по цене может сходиться всё, но сидеть
    # с ней месяцами — тоже убыток, а в отборе это сейчас никак не учтено
    per_month = len(prices) / span_days * 30 if span_days and span_days > 0 else None
    # доля сделок выше цены, которую реально заплатит ордер (и она же с маржой)
    share_buy = share_above(prices, buy_price)
    share_buy_20 = share_above(prices, buy_price * 1.2) if buy_price else None
    # чем кончилась бы проверка, стой ref_percentile на 50 (медиана) — чтобы
    # сравнить два правила на одних и тех же моделях, ничего не переключая
    ref50 = percentile(prices, 50)
    inflated50 = ref50 > 0 and current_floor > ref50 * (1 + tol_pct / 100)
    verdict50 = "pump" if inflated50 and ref50 < threshold else "ok"
    # односторонне: current < ref_price — это просадка, а не памп
    inflated = ref_price > 0 and current_floor > ref_price * (1 + tol_pct / 100)
    # модель выбрасываем, только если без пампа она порога не проходит
    verdict = "pump" if inflated and ref_price < threshold else "ok"
    return {"verdict": verdict, "ref_price": ref_price, "inflated": inflated,
            "used": len(prices), "fresh_skipped": fresh_skipped,
            "backdrop_skipped": backdrop_skipped,
            "p20": p20, "p80": p80, "drift_pct": drift_pct, "span_days": span_days,
            "per_month": per_month, "share_buy": share_buy, "share_buy_20": share_buy_20,
            "ref50": ref50, "verdict50": verdict50, "buy_price": buy_price}


def trim_to_limit(models: list) -> list:
    """modelNames в подписке — максимум 100 (пустой массив означал бы ВСЕ модели)."""
    if len(models) <= MAX_MODEL_NAMES:
        return models
    log.warning("моделей %d, обрезаю до %d (лимит подписки)", len(models), MAX_MODEL_NAMES)
    return models[:MAX_MODEL_NAMES]
