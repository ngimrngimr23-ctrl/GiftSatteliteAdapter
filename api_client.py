import time
import logging
from urllib.parse import quote

import requests

log = logging.getLogger("api_client")

BASE_URL = "https://api.gift-satellite.example"  # замени на реальный базовый URL сервиса


class ApiError(Exception):
    pass


class GiftApiClient:
    def __init__(self, token: str, base_url: str = BASE_URL, min_interval: float = 0.55):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval  # пауза между запросами, чтобы не упираться в rate limit
        self._last_call = 0.0

    def _headers(self):
        return {"Authorization": f"Token {self.token}"}

    def _throttle(self):
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    def _request(self, method: str, path: str, **kwargs):
        self._throttle()
        url = f"{self.base_url}{path}"
        resp = requests.request(method, url, headers=self._headers(), timeout=15, **kwargs)

        if resp.status_code == 429:
            log.warning("429 rate limit on %s, backing off 2s", path)
            time.sleep(2)
            return self._request(method, path, **kwargs)

        if resp.status_code >= 400:
            raise ApiError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")

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
