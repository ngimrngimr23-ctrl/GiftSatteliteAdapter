"""
Интерактивное меню на инлайн-кнопках.

Дублирует то, что доступно командами, но без запоминания синтаксиса. Команды
никуда не деваются — меню это просто вторая дверь к тем же настройкам.

Модуль намеренно не импортирует bot.py: это bot.py импортирует menu, а всё
нужное (аккаунты, функция проверки доступа) лежит в context.bot_data.
"""
import asyncio
import logging
import statistics
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from state import save_persisted
from updater import run_cycle

log = logging.getLogger("menu")

CB = "mn"  # префикс callback_data, чтобы не пересекаться с чужими кнопками

# Шаг изменения и границы для кнопок «плюс/минус».
LIMITS = {
    "premium_pct": (0.0, 1000.0),
    "tol_pct": (1.0, 200.0),
    "markup_pct": (0.0, 100.0),
    "markup_pct_fon": (0.0, 100.0),
}


def _cb(*parts) -> str:
    """callback_data ограничен 64 байтами, поэтому аккаунты адресуем номером, а не именем."""
    return "|".join([CB, *(str(p) for p in parts)])


def _accounts_list(context) -> list:
    return list(context.bot_data["accounts"].values())


def _acc_by_index(context, idx):
    accounts = _accounts_list(context)
    idx = int(idx)
    return accounts[idx] if 0 <= idx < len(accounts) else None


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русские склонения: 1 модель, 2 модели, 5 моделей."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def fmt_ago(ts) -> str:
    if not ts:
        return "ещё не запускался"
    import time
    secs = int(time.time() - ts)
    if secs < 60:
        return f"{secs}с назад"
    if secs < 3600:
        return f"{secs // 60}м назад"
    if secs < 86400:
        return f"{secs // 3600}ч назад"
    return f"{secs // 86400}д назад"


# --- Тексты, общие для меню и для команд ---

def models_report_text(acc) -> str:
    """Отчёт по последнему пересмотру моделей. Используется и в /models, и в меню."""
    if not acc.last_models:
        return (f"[{acc.name}] отчёта пока нет — пересмотр моделей ещё не запускался.\n"
                f"Режим сейчас: {acc.models_mode}")

    lines = [f"[{acc.name}] режим {acc.models_mode}"
             + (" — показано то, что бот выбрал бы, подписки не тронуты"
                if acc.models_mode == "preview" else "") + ":"]
    for sub_name, rep in acc.last_models.items():
        lines.append(
            f"\n• {sub_name}\n"
            f"  порог {rep['threshold']:.2f} TON, моделей видно {rep['seen']}, "
            f"кандидатов {rep['candidates']}, "
            f"{'применено' if rep['applied'] else 'подобрано'} {len(rep['picked'])}"
        )
        for model in rep["picked"][:15]:
            d = rep["details"][model]
            if d.get("inflated"):
                # цена задрана, но обычная цена всё равно выше порога — модель законная
                lines.append(f"  ✅ {model}: сейчас {d['floor']:.2f} (задрана), "
                             f"обычно уходит за {d['ref_price']:.2f} — всё равно выше порога")
            else:
                lines.append(f"  ✅ {model}: {d['floor']:.2f} TON, обычно уходит за "
                             f"{d['ref_price']:.2f} (по {d['used']} сделкам)")
        if len(rep["picked"]) > 15:
            lines.append(f"  … и ещё {len(rep['picked']) - 15}")
        for model in rep["pumped"][:10]:
            d = rep["details"][model]
            lines.append(f"  🚀 {model}: сейчас {d['floor']:.2f}, но обычно уходит за {d['ref_price']:.2f} — "
                         f"ниже порога {rep['threshold']:.2f}, в список попала бы только из-за пампа")
        if rep["no_data"]:
            lines.append(f"  ⏳ мало сделок для проверки ({len(rep['no_data'])}): "
                         + ", ".join(rep["no_data"][:8]))
        if rep.get("bad_format"):
            lines.append(f"  ⛔ сервис отклоняет имя (символы вроде ' в названии), "
                         f"пропущены ({len(rep['bad_format'])}): " + ", ".join(rep["bad_format"][:8]))
    return "\n".join(lines)


def models_report_csv(accounts: list) -> str:
    """
    Полная выгрузка последнего пересмотра ОДНИМ файлом на все аккаунты: строка
    на каждую увиденную модель, включая те, что не дошли до порога.
    """
    rows = ["аккаунт;заказ;модель;редкость %;статус;цена сейчас;премия к floor %;"
            "обычная цена;дешёвый край p20;середина p50;дорогой край p80;разброс p80/p20;"
            "цена закупки;доля сделок выше закупки %;доля сделок выше закупки +20% %;"
            "сделок в месяц;вердикт при p50;"
            "сделок учтено;сделок всего;история за дней;дрейф цены %;порог"]

    def num(value, digits=2):
        if value is None:
            return ""
        return f"{value:.{digits}f}".replace(".", ",")  # запятая — чтобы Excel понял как число

    for acc in accounts:
        for sub_name, rep in acc.last_models.items():
            for model, d in sorted(rep["details"].items(),
                                   key=lambda kv: kv[1].get("floor") or 0, reverse=True):
                p20, p80 = d.get("p20"), d.get("p80")
                spread = (p80 / p20) if p20 and p80 else None
                # rarity сервис отдаёт в тысячных: 5 — это 0.5%, а не 5%
                rarity = d.get("rarity")
                verdict50 = {"ok": "взята", "pump": "отсев"}.get(d.get("verdict50"), "")
                rows.append(";".join([
                    acc.name,
                    sub_name.replace(";", ","),
                    model.replace(";", ","),
                    num(rarity / 10, 1) if rarity is not None else "",
                    d.get("status", ""),
                    num(d.get("floor")),
                    num(d.get("premium_pct"), 1),
                    num(d.get("ref_price")),
                    num(p20), num(d.get("ref50")), num(p80), num(spread, 2),
                    num(d.get("buy_price")),
                    num(d.get("share_buy"), 0),
                    num(d.get("share_buy_20"), 0),
                    num(d.get("per_month"), 1),
                    verdict50,
                    str(d.get("used", "")),
                    str(d.get("sales_total", "")),
                    num(d.get("span_days"), 1),
                    num(d.get("drift_pct"), 1),
                    num(rep.get("threshold")),
                ]))
    return "\n".join(rows)


def models_summary_text(accounts: list) -> str:
    """Одна сводка к файлу по всем аккаунтам: статусы, редкость, дрейф."""
    with_data = [a for a in accounts if a.last_models]
    if not with_data:
        return ("Отчёта пока нет — пересмотр моделей ещё не запускался ни по одному аккаунту.\n"
                "Запустить: /refreshmodels")

    by_status, rarity_of, drifts = {}, {}, []
    orders = 0
    # сравнение двух правил на одних и тех же моделях: сколько берёт нынешний
    # процентиль, сколько взяла бы медиана, и как ложится доля сделок
    now_ok = p50_ok = checked = 0
    share_ok, share_bad, slow = [], [], 0
    for acc in with_data:
        orders += len(acc.last_models)
        for rep in acc.last_models.values():
            for d in rep["details"].values():
                status = d.get("status", "?")
                by_status[status] = by_status.get(status, 0) + 1
                if d.get("rarity") is not None:
                    rarity_of.setdefault(status, []).append(d["rarity"])
                if d.get("drift_pct") is not None:
                    drifts.append(d["drift_pct"])
                if d.get("verdict50"):
                    checked += 1
                    if d.get("verdict") == "ok":
                        now_ok += 1
                    if d["verdict50"] == "ok":
                        p50_ok += 1
                    # берём долю с маржой: без неё почти всё показывает 90-100%
                    # (выше голой цены закупки продаётся практически что угодно)
                    if d.get("share_buy_20") is not None:
                        (share_ok if d.get("verdict") == "ok" else share_bad).append(d["share_buy_20"])
                    if (d.get("per_month") or 99) < 2:
                        slow += 1

    lines = [f"📄 Разбор отбора: аккаунтов {len(with_data)}, заказов {orders}, "
             f"моделей {sum(by_status.values())}", ""]
    for status, count in sorted(by_status.items(), key=lambda kv: -kv[1]):
        rar = rarity_of.get(status)
        # медианная редкость по группе: видно, отсеиваются редкие модели или рядовые
        tail = (f" · медианная редкость {statistics.median(rar) / 10:g}%" if rar else "")
        lines.append(f"• {status}: {count}{tail}")

    if drifts:
        lines += ["", f"Дрейф цен по истории: медиана {statistics.median(drifts):+.0f}%",
                  "(сильный плюс — коллекция дорожала, и старые продажи занижают оценку;"
                  " около нуля — разброс идёт от фонов, а не от времени)"]

    if checked:
        lines += ["", f"Проверено по истории: {checked} моделей",
                  f"• нынешнее правило берёт: {now_ok}",
                  f"• медиана (p50) взяла бы: {p50_ok}"]
        if share_ok:
            lines.append(f"• доля сделок дороже закупки хотя бы на 20% — у взятых медиана "
                         f"{statistics.median(share_ok):.0f}%")
        if share_bad:
            lines.append(f"• то же у отсеянных: {statistics.median(share_bad):.0f}%")
        lines.append("(если у отсеянных эта доля высокая — их выкидывают зря)")
        if slow:
            lines.append(f"• торгуются реже 2 раз в месяц: {slow} — по цене могут "
                         f"проходить, но лежать будут месяцами")

    skipped = [a.name for a in accounts if not a.last_models]
    if skipped:
        lines.append(f"\nБез данных (пересмотр не запускался): {', '.join(skipped)}")
    return "\n".join(lines)


def monochrome_text(rows: list, limit: int = 20) -> str:
    """Верх рейтинга пар подарок+фон — тем же текстом, что уходит в подпись к файлу."""
    if not rows:
        return "Монохромов не пришло — проверь ключ и фильтры."
    lines = [f"🎨 Пары подарок+фон: сколько моделей влезет в один заказ",
             f"(всего пар {len(rows)}, показываю {min(limit, len(rows))})", ""]
    for r in rows[:limit]:
        good = r["high"] + r["combo"]
        lines.append(f"• {r['gift']} + {r['backdrop']} — моделей {len(r['models'])}"
                     f" (удачных сочетаний {good}, экземпляров {r['supply']})")
    best = rows[0]
    lines += ["", f"Лучшая пара: {best['gift']} + {best['backdrop']}, "
                  f"{len(best['models'])} моделей в один floor.",
              "Список моделей для modelNames — в файле, последняя колонка."]
    return "\n".join(lines)


def monochrome_csv(rows: list) -> str:
    """Тот же рейтинг файлом: модели перечислены, чтобы копировать в подписку."""
    out = ["подарок;фон;моделей;удачных сочетаний;high;combo;medium;low;"
           "экземпляров всего;модели"]
    for r in rows:
        out.append(";".join([
            r["gift"].replace(";", ","),
            r["backdrop"].replace(";", ","),
            str(len(r["models"])),
            str(r["high"] + r["combo"]),
            str(r["high"]), str(r["combo"]), str(r["medium"]), str(r["low"]),
            str(r["supply"]),
            ", ".join(r["models"]).replace(";", ","),
        ]))
    return "\n".join(out)


def refresh_summary_text(acc) -> str:
    """
    Итог пересмотра моделей: по каждому заказу видно, что именно изменилось —
    сколько моделей добавлено и сколько убрано, а не только общее число.
    """
    if not acc.last_models:
        return f"[{acc.name}] заказов на модели не нашлось — менять нечего."

    added = sum(len(r.get("added", [])) for r in acc.last_models.values())
    removed = sum(len(r.get("removed", [])) for r in acc.last_models.values())
    total = sum(len(r["picked"]) for r in acc.last_models.values())
    empty = sum(1 for r in acc.last_models.values() if not r["picked"])

    lines = [f"[{acc.name}] пересмотр закончен"
             + ("" if acc.models_mode == "on" else " (preview — заказы не тронуты)")]
    lines.append(f"➕ добавлено {added} · ➖ удалено {removed} · всего в заказах {total}")
    if empty:
        lines.append(f"⚠️ у {empty} {plural(empty, 'заказа', 'заказов', 'заказов')} "
                     f"отбор пустой, состав не тронут")
    lines.append(f"Запросов {acc.last_requests}, ошибок {len(acc.errors)}")
    return "\n".join(lines)


MODE_SHORT = {"off": "❌ выкл", "preview": "👁 показывает", "on": "✅ применяет"}


def _rules_lines(acc) -> list:
    """Сами правила отбора — то, что обычно одинаково у всех аккаунтов."""
    return [
        f"• Беру модели дороже floor коллекции на +{acc.premium_pct:g}%",
        f"• Обычную цену модели беру по {acc.sales_depth} последним продажам, по дешёвой их "
        f"части: самые дешёвые {acc.ref_percentile:g}% отбрасываю как случайные сливы, свежие "
        f"{acc.fresh_hours:g}ч не в счёт, нужно от {acc.min_sales} сделок",
        f"• Отсеиваю как памп, только если без него модель не прошла бы порог",
        f"• Цена заказа: floor {acc.markup_pct:+g}% (фоны {acc.markup_pct_fon:+g}%)",
        f"• Пересматриваю состав раз в {acc.models_interval_h:g}ч, цены — каждый цикл",
    ] + ([f"• В расчёт не идут продажи фонов: {', '.join(acc.exclude_backdrops)}"]
         if acc.exclude_backdrops else [])


def filters_text(accounts: list) -> str:
    """
    Коротко: как настроен отбор. Правила у аккаунтов обычно одинаковые, поэтому
    печатаем их один раз, а по аккаунтам показываем только режим и отличия.
    """
    lines = ["🔎 Как настроен отбор", ""]

    # группируем по настройкам: одинаковые аккаунты не должны печататься по разу
    def sig(a):
        return (a.premium_pct, a.tol_pct, a.sales_depth, a.fresh_hours, a.min_sales,
                a.markup_pct, a.markup_pct_fon, a.models_interval_h)

    groups = {}
    for acc in accounts:
        groups.setdefault(sig(acc), []).append(acc)

    if len(groups) == 1:
        lines += _rules_lines(accounts[0])
    else:
        # настройки разные — печатаем блок на каждую группу, а не на каждый аккаунт
        for group in groups.values():
            lines.append(", ".join(a.name for a in group) + ":")
            lines += _rules_lines(group[0])
            lines.append("")

    lines.append("")
    lines.append("Режим: " + " · ".join(f"{a.name} {MODE_SHORT[a.models_mode]}" for a in accounts))

    picked = sum(len(r["picked"]) for a in accounts for r in a.last_models.values())
    if picked:
        lines.append(f"Отобрано в прошлый раз: {picked} "
                     f"{plural(picked, 'модель', 'модели', 'моделей')}. Подробно: /models")
    return "\n".join(lines)


def _account_text(acc) -> str:
    return "\n".join([
        f"📋 {acc.name}",
        f"Статус: {'⏸ на паузе' if acc.paused else '▶️ активен'}",
        f"Наценки: модели +{acc.markup_pct:g}%, фоны +{acc.markup_pct_fon:g}%",
        f"Автоподбор моделей: {acc.models_mode}",
        f"Последний пересчёт цен: {fmt_ago(acc.last_run_ts)}",
        f"Последний пересмотр моделей: {fmt_ago(acc.last_models_ts)}",
        f"Обновлено: {acc.last_updated_count}, пропущено: {acc.last_skipped_count}, "
        f"запросов: {acc.last_requests}",
        f"Ошибок в буфере: {len(acc.errors)}",
    ])


def _automodels_text(acc) -> str:
    return "\n".join([
        f"🤖 Автоподбор моделей — {acc.name}",
        f"Текущий режим: {acc.models_mode}",
        "",
        "❌ off — не считать вовсе, как будто автоподбора нет",
        "👁 preview — посчитать и показать в отчёте, подписки не трогать",
        "✅ on — считать и применять к подпискам",
        "",
        f"Отбор: модели дороже floor коллекции на +{acc.premium_pct:g}%,",
        f"памп — если цена выше обычной более чем на {acc.tol_pct:g}%.",
        f"Пересмотр раз в {acc.models_interval_h:g}ч, последний — {fmt_ago(acc.last_models_ts)}.",
    ])


def _params_text(acc) -> str:
    return "\n".join([
        f"⚙️ Параметры отбора — {acc.name}",
        "",
        f"Премия над floor: +{acc.premium_pct:g}%  (какая выгода тебя устраивает)",
        f"Допуск пампа: {acc.tol_pct:g}%  (насколько цена может быть выше истории)",
        f"Глубина истории: {acc.sales_depth} продаж",
        f"Свежие сделки не в счёт: {acc.fresh_hours:g}ч",
        f"Отбрасываю самых дешёвых продаж: {acc.ref_percentile:g}%",
        f"Минимум сделок: {acc.min_sales}",
        f"Добор цен: {'все модели' if not acc.probe_limit else f'до {acc.probe_limit}'} "
        f"по {acc.probe_markets} маркет(ам)",
        f"Пересмотр состава: раз в {acc.models_interval_h:g}ч",
    ])


# --- Клавиатуры ---

def _main_kb(context) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("📊 Статус", callback_data=_cb("status")),
        InlineKeyboardButton("⚠️ Ошибки", callback_data=_cb("errors")),
    ]]
    accounts = _accounts_list(context)
    for i in range(0, len(accounts), 2):
        rows.append([
            InlineKeyboardButton(f"⚙️ {acc.name}", callback_data=_cb("acc", i + j))
            for j, acc in enumerate(accounts[i:i + 2])
        ])
    rows.append([InlineKeyboardButton("🔄 Пересчитать цены (все)", callback_data=_cb("forceall"))])
    return InlineKeyboardMarkup(rows)


def _account_kb(idx, acc) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Возобновить" if acc.paused else "⏸ Пауза",
                                 callback_data=_cb("pause", idx)),
            InlineKeyboardButton("🔄 Цены сейчас", callback_data=_cb("force", idx)),
        ],
        [
            InlineKeyboardButton("🤖 Автоподбор", callback_data=_cb("auto", idx)),
            InlineKeyboardButton("📈 Отчёт моделей", callback_data=_cb("report", idx)),
        ],
        [InlineKeyboardButton("🔎 Что сейчас настроено", callback_data=_cb("filters", idx))],
        [
            InlineKeyboardButton("💰 Наценки", callback_data=_cb("markup", idx)),
            InlineKeyboardButton("⚙️ Параметры отбора", callback_data=_cb("params", idx)),
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data=_cb("main"))],
    ])


def _automodels_kb(idx, acc) -> InlineKeyboardMarkup:
    mark = lambda mode, label: ("• " + label) if acc.models_mode == mode else label
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(mark("off", "❌ off"), callback_data=_cb("mode", idx, "off")),
            InlineKeyboardButton(mark("preview", "👁 preview"), callback_data=_cb("mode", idx, "preview")),
            InlineKeyboardButton(mark("on", "✅ on"), callback_data=_cb("mode", idx, "on")),
        ],
        [InlineKeyboardButton("🔁 Пересмотреть модели сейчас", callback_data=_cb("refresh", idx))],
        [InlineKeyboardButton("↩️ Вернуть ручные модели", callback_data=_cb("restore", idx))],
        [InlineKeyboardButton("◀️ Назад", callback_data=_cb("acc", idx))],
    ])


def _params_kb(idx, acc) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("−10", callback_data=_cb("set", idx, "premium_pct", -10)),
            InlineKeyboardButton(f"Премия +{acc.premium_pct:g}%", callback_data=_cb("noop")),
            InlineKeyboardButton("+10", callback_data=_cb("set", idx, "premium_pct", 10)),
        ],
        [
            InlineKeyboardButton("−5", callback_data=_cb("set", idx, "tol_pct", -5)),
            InlineKeyboardButton(f"Допуск {acc.tol_pct:g}%", callback_data=_cb("noop")),
            InlineKeyboardButton("+5", callback_data=_cb("set", idx, "tol_pct", 5)),
        ],
        [
            InlineKeyboardButton(("• " if acc.sales_depth == d else "") + f"{d} продаж",
                                 callback_data=_cb("depth", idx, d))
            for d in (20, 40, 100)
        ],
        [
            InlineKeyboardButton(("• " if acc.probe_markets == m else "") + f"{m} маркет",
                                 callback_data=_cb("markets", idx, m))
            for m in (1, 2, 3)
        ],
        [
            InlineKeyboardButton(("• " if acc.models_interval_h == h else "") + f"{h}ч",
                                 callback_data=_cb("interval", idx, h))
            for h in (24, 48, 72)
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data=_cb("acc", idx))],
    ])


def _markup_kb(idx, acc) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("−1", callback_data=_cb("set", idx, "markup_pct", -1)),
            InlineKeyboardButton(f"Модели +{acc.markup_pct:g}%", callback_data=_cb("noop")),
            InlineKeyboardButton("+1", callback_data=_cb("set", idx, "markup_pct", 1)),
        ],
        [
            InlineKeyboardButton("−1", callback_data=_cb("set", idx, "markup_pct_fon", -1)),
            InlineKeyboardButton(f"Фоны +{acc.markup_pct_fon:g}%", callback_data=_cb("noop")),
            InlineKeyboardButton("+1", callback_data=_cb("set", idx, "markup_pct_fon", 1)),
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data=_cb("acc", idx))],
    ])


def _back_kb(idx=None) -> InlineKeyboardMarkup:
    target = _cb("acc", idx) if idx is not None else _cb("main")
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=target)]])


# --- Обработчики ---

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/menu — открыть интерактивное меню."""
    if not context.bot_data["authorized"](update):
        return
    accounts = _accounts_list(context)
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return
    await update.message.reply_text(
        f"🤖 GiftSatellite\nАккаунтов: {len(accounts)}\n\nВыбери, что нужно:",
        reply_markup=_main_kb(context),
    )


async def _show(query, text: str, kb):
    """Телеграм ругается, если текст и клавиатура не изменились — это не ошибка, глушим."""
    try:
        await query.edit_message_text(text, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not context.bot_data["authorized"](update):
        await query.answer("Нет доступа", show_alert=True)
        return
    await query.answer()  # гасим «часики» на кнопке сразу

    parts = query.data.split("|")[1:]
    action = parts[0]
    accounts = context.bot_data["accounts"]

    if action == "noop":
        return

    if action == "main":
        await _show(query, f"🤖 GiftSatellite\nАккаунтов: {len(accounts)}\n\nВыбери, что нужно:",
                    _main_kb(context))
        return

    if action == "status":
        lines = ["📊 Все аккаунты:"]
        for acc in accounts.values():
            lines.append(
                f"{'⏸' if acc.paused else '▶️'} {acc.name}: автоподбор {acc.models_mode} | "
                f"обновлено {acc.last_updated_count} | {fmt_ago(acc.last_run_ts)} | "
                f"{f'⚠️{len(acc.errors)}' if acc.errors else '✅'}"
            )
        await _show(query, "\n".join(lines), _back_kb())
        return

    if action == "errors":
        lines = ["⚠️ Ошибки:"]
        for acc in accounts.values():
            if not acc.errors:
                lines.append(f"[{acc.name}] ошибок нет ✅")
                continue
            ts, msg = acc.errors[-1]
            t = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M:%S")
            lines.append(f"[{acc.name}] {len(acc.errors)} шт., последняя [{t}]: {msg[:200]}")
        await _show(query, "\n".join(lines)[:4000], _back_kb())
        return

    if action == "forceall":
        active = [a for a in accounts.values() if not a.paused]
        if not active:
            await _show(query, "Все аккаунты на паузе, обновлять нечего", _back_kb())
            return
        await _show(query, f"🔄 Пересчитываю цены для {len(active)} аккаунт(ов)...", None)
        ran_any = False
        for acc in active:
            ran_any |= await asyncio.to_thread(run_cycle, acc)
        if not ran_any:
            await _show(query, "Цикл уже идёт — дождись окончания и повтори.", _back_kb())
            return
        lines = ["✅ Готово:"]
        for acc in active:
            lines.append(f"[{acc.name}] обновлено {acc.last_updated_count}, "
                         f"пропущено {acc.last_skipped_count}, запросов {acc.last_requests}")
        save_persisted(accounts)
        await _show(query, "\n".join(lines), _back_kb())
        return

    # дальше всё привязано к конкретному аккаунту
    idx = parts[1]
    acc = _acc_by_index(context, idx)
    if acc is None:
        await _show(query, "Аккаунт не найден — открой меню заново: /menu", None)
        return

    if action == "acc":
        await _show(query, _account_text(acc), _account_kb(idx, acc))
        return

    if action == "pause":
        acc.paused = not acc.paused
        save_persisted(accounts)
        await _show(query, _account_text(acc), _account_kb(idx, acc))
        return

    if action == "force":
        await _show(query, f"🔄 [{acc.name}] пересчитываю цены...", None)
        ran = await asyncio.to_thread(run_cycle, acc)
        text = (f"[{acc.name}] готово: обновлено {acc.last_updated_count}, "
                f"пропущено {acc.last_skipped_count}, запросов {acc.last_requests}, "
                f"ошибок {len(acc.errors)}") if ran else "Цикл уже идёт — повтори позже."
        save_persisted(accounts)
        await _show(query, text, _back_kb(idx))
        return

    if action == "auto":
        await _show(query, _automodels_text(acc), _automodels_kb(idx, acc))
        return

    if action == "mode":
        acc.models_mode = parts[2]
        save_persisted(accounts)
        await _show(query, _automodels_text(acc), _automodels_kb(idx, acc))
        return

    if action == "refresh":
        if acc.models_mode == "off":
            await _show(query, f"[{acc.name}] автоподбор выключен. Включи preview или on.",
                        _automodels_kb(idx, acc))
            return
        await _show(query, f"🔁 [{acc.name}] полный пересмотр моделей. Это надолго — "
                           f"перебираются все модели всех коллекций. Дождись отчёта.", None)
        ran = await asyncio.to_thread(run_cycle, acc, True)
        if not ran:
            await _show(query, "Цикл уже идёт — повтори позже.", _back_kb(idx))
            return
        save_persisted(accounts)
        await _show(query, refresh_summary_text(acc)[:4000], _automodels_kb(idx, acc))
        return

    if action == "restore":
        if acc.models_mode == "on":
            await _show(query, f"[{acc.name}] сначала переключи режим в off — иначе следующий "
                               f"пересмотр снова перепишет модели.", _automodels_kb(idx, acc))
            return
        if not acc.original_models:
            await _show(query, f"[{acc.name}] нечего возвращать — бот ещё не переписывал модели.",
                        _automodels_kb(idx, acc))
            return
        restored, failed = await asyncio.to_thread(context.bot_data["restore_models"], acc)
        save_persisted(accounts)
        await _show(query, f"[{acc.name}] восстановлено подписок: {restored}"
                           + (f", ошибок: {failed}" if failed else ""), _automodels_kb(idx, acc))
        return

    if action == "report":
        await _show(query, models_report_text(acc)[:4000], _back_kb(idx))
        return

    if action == "filters":
        await _show(query, filters_text([acc])[:4000], _back_kb(idx))
        return

    if action == "params":
        await _show(query, _params_text(acc), _params_kb(idx, acc))
        return

    if action == "markup":
        await _show(query, f"💰 Наценки над floor — {acc.name}\n\n"
                           f"Модели: +{acc.markup_pct:g}%\nФоны: +{acc.markup_pct_fon:g}%",
                    _markup_kb(idx, acc))
        return

    if action == "set":
        field, delta = parts[2], float(parts[3])
        low, high = LIMITS[field]
        setattr(acc, field, min(high, max(low, getattr(acc, field) + delta)))
        save_persisted(accounts)
        if field.startswith("markup"):
            await _show(query, f"💰 Наценки над floor — {acc.name}\n\n"
                               f"Модели: +{acc.markup_pct:g}%\nФоны: +{acc.markup_pct_fon:g}%",
                        _markup_kb(idx, acc))
        else:
            await _show(query, _params_text(acc), _params_kb(idx, acc))
        return

    if action in ("depth", "markets", "interval"):
        field = {"depth": "sales_depth", "markets": "probe_markets", "interval": "models_interval_h"}[action]
        value = float(parts[2])
        setattr(acc, field, value if field == "models_interval_h" else int(value))
        save_persisted(accounts)
        await _show(query, _params_text(acc), _params_kb(idx, acc))
        return

    log.warning("неизвестное действие меню: %s", query.data)
