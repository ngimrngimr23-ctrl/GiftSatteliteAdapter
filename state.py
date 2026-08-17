import os
import json
import time
import logging
import requests
from dataclasses import dataclass, field
from collections import deque
from typing import Optional

log = logging.getLogger("state")

STATE_FILE = "state.json"  # фолбэк для локальной разработки (на Render диск эфемерный!)
MAX_ERRORS = 30  # сколько последних ошибок хранить на аккаунт

GLOBAL_STATE_FILE = "global_state.json"  # фолбэк для локальной разработки
GLOBAL_REDIS_KEY = os.environ.get("REDIS_GLOBAL_KEY", "giftadapter:global")


UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
REDIS_STATE_KEY = os.environ.get("REDIS_STATE_KEY", "giftadapter:state")
_UPSTASH_ENABLED = bool(UPSTASH_URL and UPSTASH_TOKEN)


@dataclass
class AccountState:
    name: str
    client: object  # GiftApiClient
    markup_pct: float = 3.0  # наценка для заказов на модели (подписки без backdropNames)
    markup_pct_fon: float = 3.0  # наценка для заказов на фоны (подписки с заданным backdropNames)
    paused: bool = False
    # --- автоподбор моделей в заказ ---
    auto_models: bool = False  # выключен по умолчанию: включение меняет modelNames живых подписок
    premium_pct: float = 50.0  # на сколько % выше floor коллекции должна стоить модель (порог)
    tol_pct: float = 15.0  # насколько текущая цена может превышать медиану продаж, прежде чем это памп
    sales_depth: int = 100  # сколько последних продаж смотреть (кратно 20 — размеру страницы истории)
    fresh_hours: float = 24.0  # свежие продажи не идут в базу сравнения
    min_sales: int = 5  # меньше продаж в базе — проверить нечем, модель не берём
    probe_limit: int = 30  # максимум моделей, чью цену доуточняем запросами за цикл
    probe_markets: int = 1  # по скольким маркетам уточнять цену модели (1 быстрее, 3 точнее)
    last_run_ts: Optional[float] = None
    last_updated_count: int = 0
    last_skipped_count: int = 0
    last_requests: int = 0  # запросов к API за последний цикл — видно реальную стоимость
    last_models: dict = field(default_factory=dict)  # отчёт последнего цикла для /models, не персистится
    original_models: dict = field(default_factory=dict)  # {sub_id: [модели]} до первой перезаписи ботом
    errors: deque = field(default_factory=lambda: deque(maxlen=MAX_ERRORS))

    def record_error(self, message: str):
        self.errors.append((time.time(), message))
        log.error("[%s] %s", self.name, message)

    def to_persist(self):
        return {
            "markup_pct": self.markup_pct,
            "markup_pct_fon": self.markup_pct_fon,
            "paused": self.paused,
            "auto_models": self.auto_models,
            "premium_pct": self.premium_pct,
            "tol_pct": self.tol_pct,
            "sales_depth": self.sales_depth,
            "fresh_hours": self.fresh_hours,
            "min_sales": self.min_sales,
            "probe_limit": self.probe_limit,
            "probe_markets": self.probe_markets,
            "original_models": self.original_models,
        }


def _upstash_headers():
    return {"Authorization": f"Bearer {UPSTASH_TOKEN}"}


def _load_from_upstash() -> dict:
    try:
        r = requests.get(
            f"{UPSTASH_URL}/get/{REDIS_STATE_KEY}",
            headers=_upstash_headers(),
            timeout=10,
        )
        r.raise_for_status()
        result = r.json().get("result")
        if not result:
            return {}
        return json.loads(result)
    except Exception as e:
        log.warning("не удалось прочитать состояние из Upstash: %s", e)
        return {}


def _save_to_upstash(data: dict):
    try:
        r = requests.post(
            f"{UPSTASH_URL}/set/{REDIS_STATE_KEY}",
            headers=_upstash_headers(),
            data=json.dumps(data),
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("не удалось сохранить состояние в Upstash: %s", e)


def _load_from_file() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("не удалось прочитать %s: %s", STATE_FILE, e)
        return {}


def _save_to_file(data: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.warning("не удалось сохранить %s: %s", STATE_FILE, e)


def load_persisted() -> dict:
    if _UPSTASH_ENABLED:
        return _load_from_upstash()
    log.warning(
        "UPSTASH_REDIS_REST_URL/TOKEN не заданы — состояние хранится в локальном файле "
        "и НЕ переживёт передеплой на Render"
    )
    return _load_from_file()


def save_persisted(accounts: dict):
    data = {name: acc.to_persist() for name, acc in accounts.items()}
    if _UPSTASH_ENABLED:
        _save_to_upstash(data)
        return
    _save_to_file(data)


def _load_from_upstash_key(key: str) -> dict:
    try:
        r = requests.get(
            f"{UPSTASH_URL}/get/{key}",
            headers=_upstash_headers(),
            timeout=10,
        )
        r.raise_for_status()
        result = r.json().get("result")
        if not result:
            return {}
        return json.loads(result)
    except Exception as e:
        log.warning("не удалось прочитать %s из Upstash: %s", key, e)
        return {}


def _save_to_upstash_key(key: str, data: dict):
    try:
        r = requests.post(
            f"{UPSTASH_URL}/set/{key}",
            headers=_upstash_headers(),
            data=json.dumps(data),
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("не удалось сохранить %s в Upstash: %s", key, e)


def _load_from_file_path(path: str) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("не удалось прочитать %s: %s", path, e)
        return {}


def _save_to_file_path(path: str, data: dict):
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.warning("не удалось сохранить %s: %s", path, e)


def load_global_settings() -> dict:
    """Настройки, общие для всего бота (не привязаны к конкретному аккаунту), напр. интервал проверки цен."""
    if _UPSTASH_ENABLED:
        return _load_from_upstash_key(GLOBAL_REDIS_KEY)
    return _load_from_file_path(GLOBAL_STATE_FILE)


def save_global_settings(data: dict):
    if _UPSTASH_ENABLED:
        _save_to_upstash_key(GLOBAL_REDIS_KEY, data)
        return
    _save_to_file_path(GLOBAL_STATE_FILE, data)


    
