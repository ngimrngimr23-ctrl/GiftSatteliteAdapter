import re
import time
import logging
import threading
from urllib.parse import quote

import requests

log = logging.getLogger("api_client")

BASE_URL = "https://api.gift-satellite.example"  # замени на реальный базовый URL сервиса

MAX_429_RETRIES = 5  # сколько раз пережидать rate limit, прежде чем сдаться
RETRY_BACKOFF_SECONDS = 2.0  # база линейного бэкоффа: 2с, 4с, 6с, ...

# Документированный лимит (2 req/s у search и history) на практике срабатывает
# раньше: при паузе 0.55с сервис регулярно отвечает 429, и каждый такой ответ
# стоит 2 секунды простоя. Поэтому после каждого 429 пауза увеличивается и
# остаётся такой до конца жизни процесса — клиент сам находит темп, который
# сервис принимает, вместо того чтобы биться в лимит на каждом запросе.
THROTTLE_STEP = 1.15  # во сколько раз растягиваем паузу после 429
MAX_THROTTLE = 2.0  # выше этого не поднимаем, иначе проход встанет совсем
# Пауза умела только расти. Один шторм 429 (например, когда в API параллельно
# ходили два процесса) разгонял её до потолка, и она такой оставалась до конца
# жизни процесса — скан шёл втрое медленнее уже без всякой причины. Теперь она
# сползает обратно, если лимит давно не срабатывал.
THROTTLE_RECOVER_AFTER = 120.0  # столько секунд без 429, прежде чем сбавлять
THROTTLE_RECOVER_EVERY = 60.0   # и не чаще раза в минуту
HISTORY_PAGE_SIZE = 20  # жёсткий потолок pageSize у POST /history/:collection


def _short_error(resp) -> str:
    """
    Короткое описание ошибки вместо тела ответа целиком.

    Когда у сервиса падает бэкенд, Cloudflare отдаёт HTML-страницу на полсотни
    строк, и она целиком уезжала и в лог, и в буфер ошибок, и в /errors —
    прочитать там что-либо было невозможно. Осмысленное содержимое такой
    страницы — ровно её заголовок.
    """
    body = (resp.text or "").strip()
    if body[:1] == "<" or "text/html" in (resp.headers.get("Content-Type") or ""):
        found = re.search(r"<title>(.*?)</title>", body, re.I | re.S)
        title = found.group(1).strip() if found else f"HTTP {resp.status_code}"
        return f"{title} (страница-заглушка, сервис недоступен)"
    return body[:800]


class ApiError(Exception):
    pass


class GiftApiClient:
    def __init__(self, token: str, base_url: str = BASE_URL, min_interval: float = 0.55):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval  # пауза между запросами, чтобы не упираться в rate limit
        self._base_interval = min_interval  # к ней возвращаемся, когда лимит отпустил
        self._last_call = 0.0
        self._last_429 = 0.0
        self._last_recover = 0.0
        # Клиент один на аккаунт, а ходят в него параллельно: цикл цен, ручной
        # пересмотр моделей и скан рынка. Без замка потоки проходят проверку
        # паузы одновременно и стреляют залпом — ровно отсюда и берутся 429
        # пачками, которые видно в логах.
        self._call_lock = threading.Lock()
        self.request_count = 0  # сбрасывается в начале цикла — видно, во что обошёлся автоподбор
        # Сквозной счётчик за жизнь процесса: request_count обнуляет каждый цикл
        # цен, а скан идёт часами через тот же клиент, и его прогресс по нему
        # показывал не всего, а «с последнего цикла».
        self.total_requests = 0

    def _headers(self):
        return {"Authorization": f"Token {self.token}"}

    def _recover_throttle(self, now: float):
        """Вернуть паузу к базовой, если 429 давно не было. Зовётся под замком."""
        if self.min_interval <= self._base_interval:
            return
        if now - self._last_429 < THROTTLE_RECOVER_AFTER:
            return
        if now - self._last_recover < THROTTLE_RECOVER_EVERY:
            return
        self._last_recover = now
        self.min_interval = max(self._base_interval, self.min_interval / THROTTLE_STEP)
        log.info("лимит отпустил: пауза между запросами теперь %.2fс", self.min_interval)

    def _throttle(self):
        # замок держим и на время сна: лимит общий на аккаунт, значит и очередь
        # должна быть общей, иначе два потока просто поделят паузу пополам
        with self._call_lock:
            self._recover_throttle(time.monotonic())
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
            self.total_requests += 1
            url = f"{self.base_url}{path}"
            resp = requests.request(method, url, headers=self._headers(), timeout=15, **kwargs)
            if resp.status_code != 429:
                break
            if attempt == MAX_429_RETRIES:
                raise ApiError(f"{method} {path} -> 429: rate limit не отпустил за {MAX_429_RETRIES} попыток")
            # раз лимит сработал — сбавляем темп на будущее, иначе следующий
            # запрос упрётся точно так же
            with self._call_lock:
                self._last_429 = time.monotonic()
                if self.min_interval < MAX_THROTTLE:
                    self.min_interval = min(MAX_THROTTLE, self.min_interval * THROTTLE_STEP)
                    log.info("сбавляю темп: пауза между запросами теперь %.2fс", self.min_interval)
            delay = RETRY_BACKOFF_SECONDS * (attempt + 1)
            log.warning("429 rate limit on %s, backing off %.1fs (попытка %d/%d)",
                        path, delay, attempt + 1, MAX_429_RETRIES)
            time.sleep(delay)

        if resp.status_code >= 400:
            # логируем тело запроса, которое вызвало ошибку — помогает найти,
            # какое поле не проходит валидацию на бэкенде
            log.error("Request body that failed (%s %s): %s", method, path, kwargs.get("json"))
            raise ApiError(f"{method} {path} -> {resp.status_code}: {_short_error(resp)}")

        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # --- User / subscriptions ---
    def get_me(self):
        """GET /user/me — профиль владельца токена. Используется для проверки токена при /addaccount."""
        return self._request("GET", "/user/me")

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
    def get_collections(self):
        """
        GET /gift/collections — все коллекции сервиса, отсортированные по имени.
        Возвращает [{"name": ..., "telegramId": ...}]. Нужен сканеру рынка:
        подписки покрывают лишь часть коллекций, а искать выгодные оферы надо
        по всем.
        """
        return self._request("GET", "/gift/collections")

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
