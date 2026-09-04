"""
Рейтинг пар «подарок + фон» по числу монохромных моделей.

Зачем. Цена у автобай-подписки одна на весь заказ, поэтому выгоднее всего та
пара, под которую попадает максимум моделей: один ордер с длинным modelNames и
одним backdropNames ловит их все под общий floor.

Источник — giftwiki: GET /gifts/monochromes, ключ со скоупом collection:read
в заголовке X-API-Key. Сам API маленький, цен и истории продаж там нет, только
разметка сочетаний по type: high / medium / low / combo.
"""
import collections
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("monochrome")

BASE_URL = "https://api.giftwiki.tg"
PAGE_SIZE = 21          # публичный список фиксирован на 21 записи
MIN_INTERVAL = 0.35
MAX_PAGES = 200


class WikiError(Exception):
    pass


class WikiForbidden(WikiError):
    """403 — ключу приложения недоступны фильтры gift_id/gift_name."""


class WikiClient:
    def __init__(self, api_key: str, base_url: str | None = None):
        self.api_key = api_key
        # берём модульный BASE_URL в момент вызова, а не при импорте:
        # так адрес можно подменить в тестах, не трогая сигнатуру
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self._last = 0.0
        self.request_count = 0

    def _get(self, path: str, params: dict | None = None):
        wait = MIN_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.request_count += 1

        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url)
        req.add_header("X-API-Key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:200]
            if e.code == 403:
                raise WikiForbidden(detail)
            raise WikiError(f"HTTP {e.code}: {detail}")
        except Exception as e:
            raise WikiError(str(e))

    def monochromes_by_gift(self, gift_name: str, types=None) -> list:
        """Все записи по подарку разом: этот фильтр игнорирует пагинацию."""
        params = {"gift_name": gift_name}
        if types:
            params["type"] = ",".join(types)
        return self._get("/gifts/monochromes", params) or []

    def monochromes_paged(self, types=None, max_pages: int = MAX_PAGES,
                          on_page=None) -> list:
        """Публичный обход по 21 записи, со страховкой от зацикливания по _id."""
        out, seen = [], set()
        for page in range(1, max_pages + 1):
            params = {"page": page, "limit": PAGE_SIZE}
            if types:
                params["type"] = ",".join(types)
            batch = self._get("/gifts/monochromes", params) or []
            if not batch:
                break
            fresh = [r for r in batch if r.get("_id") not in seen]
            if not fresh:
                break
            seen.update(r.get("_id") for r in fresh)
            out += fresh
            if on_page:
                on_page(page, len(out))
            if len(batch) < PAGE_SIZE:
                break
        return out


def rank(records: list) -> list:
    """
    Группировка по паре подарок+фон: сколько РАЗНЫХ моделей монохромны с этим
    фоном. Сортировка — по числу моделей, при равенстве выше та пара, где
    больше удачных сочетаний (high и combo).
    """
    pairs = collections.defaultdict(
        lambda: {"models": set(), "types": collections.Counter(), "supply": 0})
    for r in records:
        gift = r.get("gift_name") or r.get("gift_id")
        backdrop, model = r.get("backdrop_name"), r.get("model_name")
        if not (gift and backdrop and model):
            continue
        cell = pairs[(gift, backdrop)]
        cell["models"].add(model)
        cell["types"][r.get("type") or "?"] += 1
        cell["supply"] += r.get("count") or 0

    rows = []
    for (gift, backdrop), cell in pairs.items():
        t = cell["types"]
        rows.append({
            "gift": gift,
            "backdrop": backdrop,
            "models": sorted(cell["models"]),
            "high": t["high"], "combo": t["combo"],
            "medium": t["medium"], "low": t["low"],
            "supply": cell["supply"],
        })
    rows.sort(key=lambda r: (-len(r["models"]), -(r["high"] + r["combo"])))
    return rows


def fetch(api_key: str, gifts=None, types=None, max_pages: int = MAX_PAGES,
          on_page=None) -> tuple[list, WikiClient]:
    """
    Записи монохромов. С непустым gifts идёт быстрым путём (все записи по
    подарку за запрос); если ключу этот фильтр недоступен, отдаём WikiForbidden
    наверх, чтобы вызывающий сам решил, падать или переключаться на обход.
    """
    client = WikiClient(api_key)
    if gifts:
        records = []
        for name in gifts:
            records += client.monochromes_by_gift(name, types)
        return records, client
    return client.monochromes_paged(types, max_pages, on_page), client
