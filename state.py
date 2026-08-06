import json
import time
import logging
from dataclasses import dataclass, field
from collections import deque
from typing import Optional

log = logging.getLogger("state")

STATE_FILE = "state.json"
MAX_ERRORS = 30  # сколько последних ошибок хранить на аккаунт


@dataclass
class AccountState:
    name: str
    client: object  # GiftApiClient
    markup_pct: float = 3.0
    paused: bool = False
    last_run_ts: Optional[float] = None
    last_updated_count: int = 0
    last_skipped_count: int = 0
    errors: deque = field(default_factory=lambda: deque(maxlen=MAX_ERRORS))

    def record_error(self, message: str):
        self.errors.append((time.time(), message))
        log.error("[%s] %s", self.name, message)

    def to_persist(self):
        return {"markup_pct": self.markup_pct, "paused": self.paused}


def load_persisted() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("не удалось прочитать %s: %s", STATE_FILE, e)
        return {}


def save_persisted(accounts: dict):
    data = {name: acc.to_persist() for name, acc in accounts.items()}
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.warning("не удалось сохранить %s: %s", STATE_FILE, e)
