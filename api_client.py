import time
import logging
from urllib.parse import quote

import requests

log = logging.getLogger("api_client")

BASE_URL = "https://api.gift-satellite.example"  # замени на реальный базовый URL сервиса

MAX_429_RETRIES = 5  # сколько раз пережидать rate limit, прежде чем сдаться
RETRY_BACKOFF_SECONDS = 2.0  # база линейного бэкоффа: 2с, 4с, 6с, ...
HISTORY_PAGE_SIZE = 20  # жёсткий потолок pageSize у POST /history/:collection


class ApiError(Exception):
    pass


class GiftApiClient:
    def __init__(self, token: str, base_url: str = BASE_URL, min_interval: float = 0.55):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval  # пауза между запросами, чтобы не упираться в rate limit
        self._last_call = 0.0
        self.request_count = 0  # сбрасывается в начале цикла — видно, во что обошёлся автоподбор

    def _headers(self):
        return {"Authorization": f"Token {self.token}"}

    def _throttle(self):
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    def _request(self, method: str, path: str, **kwargs):
        # Ретраи на 429 сделаны циклом, а не рекурсией: автоподбор моделей шлёт
        # на порядок больше запросов, и затяжной rate limit при рекурсии
        # положил бы процесс переполнением стека.
        for attempt in range(MAX_429_RETRIES + 1):
            self._throttle()
            self.request_count += 1
            url = f"{self.base_url}{path}"
            resp = requests.request(method, url, headers=self._headers(), timeout=15, **kwargs)
            if resp.status_code != 429:
                break
            if attempt == MAX_429_RETRIES:
                raise ApiError(f"{method} {path} -> 429: rate limit не отпустил за {MAX_429_RETRIES} попыток")
            delay = RETRY_BACKOFF_SECONDS * (attempt + 1)
            log.warning("429 rate limit on %s, backing off %.1fs (попытка %d/%d)",
                        path, delay, attempt + 1, MAX_429_RETRIES)
            time.sleep(delay)

        if resp.status_code >= 400:
            # логируем тело запроса, которое вызвало ошибку — помогает найти,
            # какое поле не проходит валидацию на бэкенде
            log.error("Request body that failed (%s %s): %s", method, path, kwargs.get("json"))
            raise ApiError(f"{method} {path} -> {resp.status_code}: {resp.text[:800]}")

        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # --- User / subscriptions ---
    def get_subscriptions(self):
        return self._request("GET", "/user/subscriptions")

    def update_subscription(self, sub_id: str, body: dict):
        return self._request("PUT", f"/user/update-subscription/{sub_id}", json=body)

    # --- Search (floor price lookup) ---
    def search_market(self, market: str, collection: str, models=None, backdrops=None, number=None):
        """
        market: 'portals' | 'tonnel' | 'mrkt' | 'tg' | 'getgems'
        Returns list of listings sorted by price ascending (первый = floor).
        """
        params = {}
        if models:
            params["models"] = ",".join(models)
        if backdrops:
            params["backdrops"] = ",".join(backdrops)
        if number:
            params["number"] = number
        collection_enc = quote(collection, safe="")
        path = f"/search/{market}/{collection_enc}"
        return self._request("GET", path, params=params)

    # --- Gift (справочные данные) ---
    def get_models(self, collection: str):
        """
        GET /gift/models/:collection — полный список моделей коллекции.
        Возвращает [{"name": ..., "rarity": ...}], отсортированный по редкости.
        Нужен потому, что поиск отдаёт только 50 самых дешёвых листингов, и
        дорогие модели (а именно они и интересны при отборе по премии над floor)
        в эту выдачу не попадают.
        """
        collection_enc = quote(collection, safe="")
        return self._request("GET", f"/gift/models/{collection_enc}")

    # --- History (реальные продажи) ---
    def get_history(self, collection: str, models=None, backdrops=None,
                    sort_by: str = "date", page: int = 0, page_size: int = HISTORY_PAGE_SIZE):
        """
        POST /history/:collection — страница истории продаж.
        Возвращает {"content": [...], "page": {...}}, где каждая продажа несёт
        modelName, normalizedPrice и soldAt (ISO 8601).
        """
        body = {"sortBy": sort_by, "page": page, "pageSize": min(page_size, HISTORY_PAGE_SIZE)}
        if models:
            body["models"] = list(models)
        if backdrops:
            body["backdrops"] = list(backdrops)
        collection_enc = quote(collection, safe="")
        return self._request("POST", f"/history/{collection_enc}", json=body)
