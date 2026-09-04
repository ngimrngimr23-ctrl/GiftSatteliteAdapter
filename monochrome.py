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
import re
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
    """403 от самого API — ключу недоступны фильтры gift_id/gift_name."""


class WikiBlocked(WikiError):
    """
    Запрос до API не дошёл: его завернул Cloudflare. Отдаётся тоже с кодом 403,
    поэтому без разбора тела это неотличимо от нехватки прав у ключа — а лечится
    совсем иначе, и повтор другим способом обхода бессмысленен.
    """


# Cloudflare пишет свой код в тело страницы: "error code: 1010".
# 1010 — забракована сигнатура клиента (для нас это User-Agent от urllib),
# 1020 — сработало правило доступа, 1015 — превышен лимит запросов.
CF_ERROR = re.compile(r"error code:\s*(\d{4})")
CF_MEANING = {
    "1010": "Cloudflare забраковал сигнатуру клиента",
    "1015": "Cloudflare ограничил частоту запросов",
    "1020": "Cloudflare заблокировал по своему правилу доступа",
}

# Без похожего на браузер набора заголовков Cloudflare отвечает 1010 и запрос
# до API не доходит вовсе.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class WikiClient:
    def __init__(self, api_key: str, base_url: str | None = None):
        # Значение приезжает из переменной окружения, куда легко попадают
        # кавычки, пробелы и перенос строки — сервис на такой ключ отвечает
        # 401, и по сообщению это неотличимо от неверного ключа.
        self.api_key = (api_key or "").strip().strip('"').strip("'")
        # Доки принимают две формы авторизации. Начинаем с X-API-Key, при 401
        # один раз пробуем вторую и дальше держимся той, что сработала.
        self.auth_style = "x-api-key"
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
        return self._send(url, self.auth_style, retry_auth=True)

    def _send(self, url: str, style: str, retry_auth: bool = False):
        req = urllib.request.Request(url)
        if style == "x-api-key":
            req.add_header("X-API-Key", self.api_key)
        else:
            req.add_header("Authorization", f"ApiKey {self.api_key}")
        for header, value in BROWSER_HEADERS.items():
            req.add_header(header, value)
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            found = CF_ERROR.search(body)
            if found:
                code = found.group(1)
                raise WikiBlocked(
                    f"{CF_MEANING.get(code, 'Cloudflare отклонил запрос')} "
                    f"(код {code}, HTTP {e.code})")
            detail = body[:200]
            if e.code == 401 and retry_auth:
                other = "authorization" if style == "x-api-key" else "x-api-key"
                log.info("giftwiki: 401 на %s, пробую %s", style, other)
                result = self._send(url, other)   # ошибка второй формы уйдёт наверх как есть
                self.auth_style = other
                return result
            if e.code == 401:
                raise WikiError(
                    "ключ не принят (401). Проверь, что в WIKI_API_KEY лежит именно "
                    "ключ giftwiki со скоупом collection:read, без кавычек и пробелов, "
                    f"и что он не отозван. Ответ сервиса: {detail}")
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
