#!/usr/bin/env python3
"""
Один вопрос: у какого подарка и с каким фоном монохромны СРАЗУ МНОГО моделей.

Смысл в том, что цена у автобай-подписки одна на весь заказ. Значит выгоднее
всего та пара «подарок + фон», где под один floor попадает максимум моделей:
ставишь один ордер с длинным modelNames и одним backdropNames и ловишь всё
сразу. Скрипт берёт разметку монохромов у giftwiki и строит такой рейтинг.

Источник: GET https://api.giftwiki.tg/gifts/monochromes
Нужен ключ со скоупом collection:read в заголовке X-API-Key.

Запуск:
    export WIKI_KEY="<ключ>"
    python3 tools/monochrome_rank.py                      # всё подряд, постранично
    python3 tools/monochrome_rank.py --gifts "Diamond Ring,Magic Potion"
    python3 tools/monochrome_rank.py --type high,combo    # только удачные сочетания

Про два режима обхода. С фильтром gift_name сервис отдаёт ВСЕ записи по
подарку за один запрос и игнорирует пагинацию — но доки помечают этот фильтр
как «API key/admin only», ключам приложения на него прилетает 403. Без него
список идёт жёстко по 21 записи на страницу. Скрипт сначала пробует быстрый
путь, при 403 сам переключается на постраничный.
"""
import argparse
import collections
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.giftwiki.tg"
MIN_INTERVAL = 0.35
_last = 0.0


def call(path, key, params=None):
    global _last
    wait = MIN_INTERVAL - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.monotonic()

    url = BASE + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(url)
    req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise PermissionError(e.read().decode()[:200])
        sys.exit(f"GET {path} -> HTTP {e.code}: {e.read().decode()[:300]}")
    except Exception as e:
        sys.exit(f"GET {path} -> {e}")


def by_gift(name, key, types):
    params = {"gift_name": name}
    if types:
        params["type"] = ",".join(types)
    return call("/gifts/monochromes", key, params) or []


def by_pages(key, types, max_pages):
    """Постраничный обход: публичный список фиксирован на 21 записи."""
    out, seen_ids = [], set()
    for page in range(1, max_pages + 1):
        params = {"page": page, "limit": 21}
        if types:
            params["type"] = ",".join(types)
        batch = call("/gifts/monochromes", key, params) or []
        if not batch:
            break
        fresh = [r for r in batch if r.get("_id") not in seen_ids]
        if not fresh:          # сервис зациклился на той же странице
            break
        seen_ids.update(r.get("_id") for r in fresh)
        out += fresh
        print(f"    страница {page}: +{len(fresh)} (всего {len(out)})", flush=True)
        if len(batch) < 21:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gifts", default="", help="названия подарков через запятую; пусто = обходить постранично")
    ap.add_argument("--type", default="", help="high,combo,medium,low; пусто = все")
    ap.add_argument("--max-pages", type=int, default=200, help="потолок страниц при обходе без --gifts")
    ap.add_argument("--out", default="monochrome_rank.csv")
    args = ap.parse_args()

    key = os.environ.get("WIKI_KEY")
    if not key:
        sys.exit("нужна переменная WIKI_KEY (ключ со скоупом collection:read)")

    types = [t.strip() for t in args.type.split(",") if t.strip()]
    gifts = [g.strip() for g in args.gifts.split(",") if g.strip()]

    records = []
    if gifts:
        for name in gifts:
            try:
                got = by_gift(name, key, types)
            except PermissionError as e:
                sys.exit(f"фильтр gift_name недоступен этому ключу (403): {e}\n"
                         f"запустите без --gifts, скрипт пойдёт постранично")
            print(f"    {name}: {len(got)} записей", flush=True)
            records += got
    else:
        print("обхожу постранично (по 21 записи):")
        records = by_pages(key, types, args.max_pages)

    if not records:
        sys.exit("ничего не пришло — проверьте ключ и фильтры")

    # группировка: пара подарок+фон -> какие модели с ней монохромны
    pairs = collections.defaultdict(lambda: {"models": set(), "types": collections.Counter(),
                                             "supply": 0})
    for r in records:
        gift = r.get("gift_name") or r.get("gift_id")
        bd, model = r.get("backdrop_name"), r.get("model_name")
        if not (gift and bd and model):
            continue
        cell = pairs[(gift, bd)]
        cell["models"].add(model)
        cell["types"][r.get("type") or "?"] += 1
        cell["supply"] += r.get("count") or 0

    rows = []
    for (gift, bd), cell in pairs.items():
        t = cell["types"]
        rows.append({
            "подарок": gift,
            "фон": bd,
            "моделей": len(cell["models"]),
            "high": t["high"], "combo": t["combo"], "medium": t["medium"], "low": t["low"],
            "экземпляров всего": cell["supply"],
            "модели": ", ".join(sorted(cell["models"])),
        })
    rows.sort(key=lambda r: (-r["моделей"], -r["high"] - r["combo"]))

    with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=";")
        w.writeheader()
        w.writerows(rows)

    print(f"\nзаписей {len(records)}, пар подарок+фон {len(rows)} -> {args.out}\n")
    print(f"{'подарок':24}{'фон':18}{'моделей':>8}{'high':>6}{'combo':>7}{'экз.':>9}")
    for r in rows[:25]:
        print(f"{r['подарок'][:22]:24}{r['фон'][:16]:18}{r['моделей']:>8}"
              f"{r['high']:>6}{r['combo']:>7}{r['экземпляров всего']:>9}")


if __name__ == "__main__":
    main()
