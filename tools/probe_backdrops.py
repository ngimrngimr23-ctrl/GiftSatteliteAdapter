#!/usr/bin/env python3
"""
Разведка по фонам и парам модель+фон. Запускается руками, к боту не подключён
и ничего не меняет — только читает API и складывает две таблицы.

Отвечает на три вопроса:
  1. Сколько стоит сам фон: медиана продаж с этим фоном против медианы продаж
     по всей коллекции. Считается по ВСЕМ моделям сразу, поэтому 20 сделок —
     это 20 разных подарков, а не одна вещь, перепроданная 20 раз.
  2. Почём сейчас конкретные пары модель+фон: floor каждой пары читается прямо
     из листингов, в каждом из них есть и modelName, и backdropName.
  3. Врёт ли пагинация истории. В выгрузках бота во всех строках ровно 16
     сделок, хотя запрашивается больше, — здесь печатаются реальные
     totalElements/totalPages из ответа, чтобы стало видно, где обрыв.

Запуск:
    export GIFT_BASE_URL="https://<реальный адрес API>"
    export GIFT_TOKEN="<токен из бота>"
    python3 tools/probe_backdrops.py "Diamond Ring"

    # только интересные фоны и модели
    python3 tools/probe_backdrops.py "Diamond Ring" \
        --backdrops "Candy,Space" --models "Frostband,Obsidian"
"""
import argparse
import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MARKETS = ("portals", "tonnel", "mrkt")
PAGE_SIZE = 20          # жёсткий потолок pageSize у POST /history
MIN_INTERVAL = 0.55     # укладывается в самый строгий лимит (2 req/s)

_last_call = 0.0


def call(method: str, path: str, token: str, base: str, params=None, body=None):
    """Один запрос с троттлингом и понятной ошибкой вместо трейсбека."""
    global _last_call
    wait = MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()

    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Token {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:300]}")
    except Exception as e:
        sys.exit(f"{method} {path} -> {e}")


def fetch_history(collection, token, base, backdrops=None, models=None, pages=5, label=""):
    """Страницы истории продаж. Возвращает (цены, диагностика пагинации)."""
    prices, info = [], {}
    for page in range(pages):
        body = {"sortBy": "date", "page": page, "pageSize": PAGE_SIZE}
        if backdrops:
            body["backdrops"] = list(backdrops)
        if models:
            body["models"] = list(models)
        data = call("POST", f"/history/{urllib.parse.quote(collection, safe='')}",
                    token, base, body=body)
        content = (data or {}).get("content") or []
        meta = (data or {}).get("page") or {}
        if page == 0:
            info = {"totalElements": meta.get("totalElements"),
                    "totalPages": meta.get("totalPages"), "firstPage": len(content)}
        prices += [s["normalizedPrice"] for s in content if s.get("normalizedPrice") is not None]
        info["pagesRead"] = page + 1
        if not content or (meta.get("totalPages") is not None and page + 1 >= meta["totalPages"]):
            break
    info["got"] = len(prices)
    if label:
        print(f"    {label}: получено {len(prices)} сделок "
              f"(в ответе totalElements={info.get('totalElements')}, "
              f"totalPages={info.get('totalPages')}, прочитано страниц {info.get('pagesRead')})")
    return prices, info


def med(values):
    return statistics.median(values) if values else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("collection", help="название коллекции, например 'Diamond Ring'")
    ap.add_argument("--backdrops", default="", help="через запятую; пусто = все фоны коллекции")
    ap.add_argument("--models", default="", help="через запятую; пусто = без фильтра по моделям")
    ap.add_argument("--pages", type=int, default=5, help="страниц истории на запрос (по 20 сделок)")
    ap.add_argument("--markets", default=",".join(MARKETS))
    ap.add_argument("--out", default="backdrops")
    args = ap.parse_args()

    base = os.environ.get("GIFT_BASE_URL")
    token = os.environ.get("GIFT_TOKEN")
    if not base or not token:
        sys.exit("нужны переменные GIFT_BASE_URL и GIFT_TOKEN")

    coll = args.collection
    coll_enc = urllib.parse.quote(coll, safe="")
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    # --- какие вообще есть фоны и насколько они редкие ---
    meta = call("GET", f"/gift/collection/{coll_enc}", token, base)
    rarity = {b["name"]: b.get("rarityPermille") for b in (meta.get("backdrops") or [])}
    backdrops = [b.strip() for b in args.backdrops.split(",") if b.strip()] or sorted(rarity)
    print(f"\n{coll}: фонов в коллекции {len(rarity)}, проверяю {len(backdrops)}")

    # --- база для сравнения: продажи коллекции без фильтра по фону ---
    print("\nбазовая история (без фильтра по фону):")
    base_prices, base_info = fetch_history(coll, token, base, models=models or None,
                                           pages=args.pages, label="вся коллекция")
    base_med = med(base_prices)
    if not base_med:
        sys.exit("по коллекции не пришло ни одной продажи — дальше считать нечего")
    print(f"    медиана по коллекции: {base_med:.2f} TON")

    # --- премия каждого фона ---
    print("\nистория по каждому фону:")
    rows = []
    for name in backdrops:
        prices, info = fetch_history(coll, token, base, backdrops=[name],
                                     models=models or None, pages=args.pages, label=name)
        m = med(prices)
        rows.append({
            "фон": name,
            "редкость ‰": rarity.get(name, ""),
            "сделок": len(prices),
            "медиана": round(m, 2) if m else "",
            "премия к коллекции %": round((m / base_med - 1) * 100, 1) if m else "",
            "p20": round(statistics.quantiles(prices, n=5)[0], 2) if len(prices) >= 5 else "",
            "p80": round(statistics.quantiles(prices, n=5)[3], 2) if len(prices) >= 5 else "",
            "totalElements в ответе": info.get("totalElements", ""),
            "totalPages в ответе": info.get("totalPages", ""),
        })

    rows.sort(key=lambda r: r["премия к коллекции %"] if r["премия к коллекции %"] != "" else -999,
              reverse=True)
    path = f"{args.out}_фоны.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=";")
        w.writeheader()
        w.writerows(rows)
    print(f"\n-> {path}")
    print(f"\n{'фон':22}{'ред ‰':>7}{'сделок':>8}{'медиана':>10}{'премия':>9}")
    for r in rows[:15]:
        prem = f"{r['премия к коллекции %']:+g}%" if r["премия к коллекции %"] != "" else "—"
        print(f"{r['фон'][:20]:22}{str(r['редкость ‰']):>7}{r['сделок']:>8}"
              f"{str(r['медиана']):>10}{prem:>9}")

    # --- floor'ы конкретных пар модель+фон из листингов ---
    # Фон есть в каждом листинге, поэтому пары не нужно перебирать запросами:
    # один запрос на маркет приносит до 50 листингов сразу с моделью и фоном.
    print("\nлистинги (floor пар модель+фон):")
    combos = {}
    for market in [m.strip() for m in args.markets.split(",") if m.strip()]:
        params = {"backdrops": ",".join(backdrops)}
        if models:
            params["models"] = ",".join(models)
        listings = call("GET", f"/search/{market}/{coll_enc}", token, base, params=params)
        print(f"    {market}: {len(listings or [])} листингов")
        for it in listings or []:
            key = (it.get("modelName"), it.get("backdropName"))
            price = it.get("normalizedPrice")
            if None in key or price is None:
                continue
            combos.setdefault(key, {})[market] = min(combos.get(key, {}).get(market, price), price)

    if combos:
        crows = []
        for (model, bd), per_market in combos.items():
            floor = min(per_market.values())
            crows.append({
                "модель": model, "фон": bd,
                "редкость фона ‰": rarity.get(bd, ""),
                "floor пары": round(floor, 2),
                "к медиане коллекции": round(floor / base_med, 2),
                "маркетов с листингом": len(per_market),
            })
        crows.sort(key=lambda r: -r["floor пары"])
        path2 = f"{args.out}_пары.csv"
        with open(path2, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(crows[0]), delimiter=";")
            w.writeheader()
            w.writerows(crows)
        print(f"-> {path2} ({len(crows)} пар)")
    else:
        print("    ни одного листинга под фильтр — пар не собрать")


if __name__ == "__main__":
    main()
