#!/usr/bin/env python3
"""
То же, что команда /monochrome в боте, но из консоли — на случай, когда бот
ещё не задеплоен или хочется прогнать разом весь список.

    export WIKI_KEY="<ключ giftwiki со скоупом collection:read>"
    python3 tools/monochrome_rank.py --type high,combo
    python3 tools/monochrome_rank.py --gifts "Diamond Ring,Magic Potion"

Вся логика живёт в monochrome.py, чтобы бот и скрипт не разъезжались.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import menu  # noqa: E402
import monochrome  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gifts", default="", help="названия подарков через запятую; пусто = постранично")
    ap.add_argument("--type", default="high,combo", help="high,combo,medium,low; пусто = все")
    ap.add_argument("--max-pages", type=int, default=monochrome.MAX_PAGES)
    ap.add_argument("--out", default="monochrome_rank.csv")
    args = ap.parse_args()

    key = os.environ.get("WIKI_KEY") or os.environ.get("WIKI_API_KEY")
    if not key:
        sys.exit("нужна переменная WIKI_KEY (ключ со скоупом collection:read)")

    types = [t.strip() for t in args.type.split(",") if t.strip()]
    gifts = [g.strip() for g in args.gifts.split(",") if g.strip()]
    progress = lambda page, total: print(f"    страница {page}: всего {total}", flush=True)

    try:
        records, client = monochrome.fetch(key, gifts=gifts or None, types=types,
                                           max_pages=args.max_pages, on_page=progress)
    except monochrome.WikiForbidden as e:
        print(f"фильтр по названию подарка недоступен этому ключу (403): {e}\n"
              f"иду постранично", flush=True)
        records, client = monochrome.fetch(key, types=types, max_pages=args.max_pages,
                                           on_page=progress)
    except monochrome.WikiError as e:
        sys.exit(f"giftwiki: {e}")

    rows = monochrome.rank(records)
    if not rows:
        sys.exit("ничего не пришло — проверьте ключ и фильтры")

    with open(args.out, "w", encoding="utf-8-sig") as fh:
        fh.write(menu.monochrome_csv(rows))
    print(f"\n{menu.monochrome_text(rows, limit=25)}")
    print(f"\nЗаписей {len(records)}, запросов {client.request_count} -> {args.out}")


if __name__ == "__main__":
    main()
