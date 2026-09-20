"""
Выгрузка базы скана в GitHub, чтобы её можно было смотреть со стороны.

Уезжает ТОЛЬКО база скана — цены моделей по сделкам и надбавки за фоны.
Настройки аккаунтов (ключ giftadapter:state) не отправляются никогда: там
лежат API-токены аккаунтов, добавленных через /addaccount, и публиковать их
нельзя ни в каком виде. На всякий случай содержимое ещё и проверяется перед
отправкой — см. looks_secret.

Пишем в отдельную ветку, а не в main: коммит в main при включённом
авто-деплое перезапускал бы бота после каждого скана.
"""
import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

log = logging.getLogger("github_sync")

TOKEN = os.environ.get("GITHUB_SYNC_TOKEN")
REPO = os.environ.get("GITHUB_SYNC_REPO")          # вида "owner/repo"
BRANCH = os.environ.get("GITHUB_SYNC_BRANCH", "scan-data")
PATH = os.environ.get("GITHUB_SYNC_PATH", "scan/baseline.csv")
API = "https://api.github.com"

# Строки, похожие на секреты. Если такое попало в выгрузку — значит ошиблись
# источником данных, и отправлять нельзя.
#
# Голое слово тут не годится: на выгрузке цветов правило сорвалось на фоне,
# который так и называется — Secret. Поэтому ловим не слово, а форму: либо
# «ключ = значение», либо узнаваемый вид самого токена.
_SECRET_HINTS = re.compile(
    r"(?:[a-z]+[_-])?(?:token|key|secret|password|passwd|authorization)"
    r"\s*[=:]\s*\S{8,}"
    r"|bot\d{6,}:[A-Za-z0-9_-]{30,}"          # токен телеграм-бота
    r"|gh[pousr]_[A-Za-z0-9]{30,}"             # токен GitHub
    r"|\bAKIA[0-9A-Z]{16}\b",                 # ключ AWS
    re.I)

# Длинная случайная строка — так выглядят токены Upstash и прочие base64.
# Регистр здесь важен, поэтому правило отдельное: под re.I проверка на
# заглавные буквы не значила бы ничего. В именах моделей и фонов таких строк
# не бывает — там пробелы и десяток символов.
_SECRET_BLOB = re.compile(
    r"(?=[A-Za-z0-9_\-]*[0-9])(?=[A-Za-z0-9_\-]*[A-Z])"
    r"(?=[A-Za-z0-9_\-]*[a-z])[A-Za-z0-9_\-]{40,}")


# Не чаще раза в столько секунд. Каждая выгрузка — это коммит, и без паузы
# длинный скан наплодил бы их сотни.
MIN_PUBLISH_INTERVAL = 300.0
_last_publish = 0.0
_last_payload = ""


def enabled() -> bool:
    return bool(TOKEN and REPO)


def publish_throttled(text: str, message: str) -> str | None:
    """
    Выгрузить, если с прошлого раза прошло достаточно времени и данные
    изменились. Возвращает None, когда выгрузка пропущена — звать можно часто.
    """
    global _last_publish, _last_payload
    if not enabled():
        return None
    now = time.monotonic()
    if now - _last_publish < MIN_PUBLISH_INTERVAL:
        return None
    if text == _last_payload:
        return None  # данные не менялись — коммит был бы пустым
    _last_publish = now
    result = publish(text, message)
    if result.startswith("Выгружено"):
        _last_payload = text
    return result


def looks_secret(text: str) -> str | None:
    """Вернуть найденный признак секрета или None, если чисто."""
    for pattern in (_SECRET_HINTS, _SECRET_BLOB):
        found = pattern.search(text or "")
        if found:
            hit = found.group(0)
            return hit if len(hit) <= 24 else hit[:12] + "…" + hit[-4:]
    return None


def _request(method: str, url: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
        return json.loads(body) if body else {}


class NoBranchRights(Exception):
    """
    Токену не дали заводить ветки.

    Отступать при этом в ветку по умолчанию нельзя, хотя соблазн есть: туда
    смотрит Render, и каждая выгрузка стала бы передеплоем — то есть обрывала
    бы идущий дозор или сбор. Да и права на запись файла у такого токена всё
    равно нет: и создание ветки, и запись файла — это одно и то же разрешение
    Contents.
    """


def _ensure_branch(branch: str) -> str:
    """
    Убедиться, что ветка выгрузки есть. Возвращает ветку, в которую писать.

    Если завести ветку нельзя, бросаем NoBranchRights — там же сказано, почему
    отступать в ветку по умолчанию хуже, чем не выгружать вовсе.
    """
    try:
        _request("GET", f"{API}/repos/{REPO}/git/ref/heads/{branch}")
        return branch
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    repo = _request("GET", f"{API}/repos/{REPO}")
    base = repo.get("default_branch") or "main"
    try:
        ref = _request("GET", f"{API}/repos/{REPO}/git/ref/heads/{base}")
        _request("POST", f"{API}/repos/{REPO}/git/refs",
                 {"ref": f"refs/heads/{branch}", "sha": ref["object"]["sha"]})
    except urllib.error.HTTPError as e:
        if e.code not in (403, 404):
            raise
        log.warning("не могу завести ветку %s: HTTP %s", branch, e.code)
        raise NoBranchRights(str(e.code))
    log.info("создал ветку %s для выгрузки базы", branch)
    return branch


def publish(text: str, message: str, path: str | None = None) -> str:
    """
    Положить текст файлом в репозиторий. Возвращает описание результата —
    оно уходит в чат, поэтому без токенов и ссылок с ними.
    """
    if not enabled():
        return ("Выгрузка не настроена: нужны переменные GITHUB_SYNC_TOKEN "
                "и GITHUB_SYNC_REPO (вида owner/repo).")
    hint = looks_secret(text)
    if hint:
        log.error("в выгрузке найден признак секрета (%s) — не отправляю", hint)
        return f"Не отправил: в данных нашлось похожее на секрет ({hint})."

    path = path or PATH
    branch = BRANCH
    try:
        try:
            branch = _ensure_branch(BRANCH)
        except NoBranchRights as e:
            return (f"Не выгрузил: токену нельзя завести ветку {BRANCH} (HTTP {e}).\n"
                    f"Нужно разрешение Contents: Read and write на репозиторий "
                    f"{REPO} — тем же разрешением делается и запись файла, так что "
                    f"без него выгрузка не пройдёт никуда.\n"
                    f"Если ветка {BRANCH} уже есть, проверь, что имя совпадает: "
                    f"оно задаётся переменной GITHUB_SYNC_BRANCH.")
        sha = None
        try:
            existing = _request(
                "GET", f"{API}/repos/{REPO}/contents/{path}?ref={branch}")
            sha = existing.get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        payload = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode(),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        _request("PUT", f"{API}/repos/{REPO}/contents/{path}", payload)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        log.warning("не смог выгрузить базу в GitHub: HTTP %s %s", e.code, detail)
        return f"Не получилось выгрузить: HTTP {e.code} {detail}"
    except Exception as e:
        log.warning("не смог выгрузить базу в GitHub: %s", e)
        return f"Не получилось выгрузить: {e}"

    return f"Выгружено в {REPO}, ветка {branch}, файл {path}"
