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

HISTORY_STATE_FILE = "price_history.json"  # фолбэк для локальной разработки
HISTORY_REDIS_KEY = os.environ.get("REDIS_HISTORY_KEY", "giftadapter:history")

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
    model_count: int = 10  # сколько моделей включать в заказ
    pump_pct: float = 20.0  # превышение floor модели над медианой за окно, считающееся пампом
    pump_window_h: float = 24.0  # окно, по которому считается медиана (часы)
    pump_cooldown_h: float = 12.0  # сколько модель не попадает в заказ после пампа (часы)
    last_run_ts: Optional[float] = None
    last_updated_count: int = 0
    last_skipped_count: int = 0
    last_models: dict = field(default_factory=dict)  # отчёт последнего цикла для /models, не персистится
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
            "model_count": self.model_count,
            "pump_pct": self.pump_pct,
            "pump_window_h": self.pump_window_h,
            "pump_cooldown_h": self.pump_cooldown_h,
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


def load_price_history() -> dict:
    """
    История floor-цен по моделям: {collection: {model: {"p": [[ts, price], ...], "b": ban_until_ts}}}.
    Это рыночные данные, общие для всех аккаунтов, поэтому лежат отдельным ключом.
    """
    if _UPSTASH_ENABLED:
        return _load_from_upstash_key(HISTORY_REDIS_KEY)
    return _load_from_file_path(HISTORY_STATE_FILE)


def save_price_history(data: dict):
    if _UPSTASH_ENABLED:
        _save_to_upstash_key(HISTORY_REDIS_KEY, data)
        return
    _save_to_file_path(HISTORY_STATE_FILE, data)
    
