import os
import re
import json
import asyncio
import logging
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from datetime import datetime

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import menu
from api_client import GiftApiClient, HISTORY_PAGE_SIZE
from state import (AccountState, load_persisted, save_persisted, load_global_settings,
                   save_global_settings, storage_status, load_scan_baseline,
                   load_watch_levels, save_watch_levels,
                   load_colors, save_colors,
                   save_scan_baseline)
from updater import run_cycle, fetch_sales_for
import monochrome
import colors
import scanner
import github_sync

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
ALLOWED_CHAT_IDS = {int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip()}
DEFAULT_CYCLE_SECONDS = int(os.environ.get("CYCLE_SECONDS", 3600))  # используется, только если интервал ещё ни разу не меняли через /setinterval
MIN_INTERVAL_MINUTES = 1
# Сколько команда ждёт освобождения цикла, прежде чем сдаться. Пересмотр всех
# моделей идёт минутами на аккаунт, а плановый цикл стартует через 10 секунд
# после запуска процесса — без ожидания почти каждое нажатие после деплоя
# упиралось в «цикл уже идёт».
REFRESH_WAIT_SECONDS = 900
FORCEUPDATE_WAIT_SECONDS = 300
GIFT_API_BASE_URL = os.environ.get("GIFT_API_BASE_URL")
WIKI_API_KEY = os.environ.get("WIKI_API_KEY")  # giftwiki, скоуп collection:read
ACCOUNTS_CFG = json.loads(os.environ["ACCOUNTS_JSON"])  # [{"name": "...", "api_token": "..."}, ...]
CYCLE_JOB_NAME = "scheduled_cycle"


def make_account(name: str, token: str, saved: dict | None = None, dynamic: bool = False) -> AccountState:
    """Собирает аккаунт с клиентом и накатывает на него сохранённые настройки."""
    kwargs = {}
    if GIFT_API_BASE_URL:
        kwargs["base_url"] = GIFT_API_BASE_URL
    acc = AccountState(name=name, client=GiftApiClient(token, **kwargs),
                       api_token=token if dynamic else None, dynamic=dynamic)
    saved = saved or {}
    acc.markup_pct = saved.get("markup_pct", acc.markup_pct)
    acc.markup_pct_fon = saved.get("markup_pct_fon", acc.markup_pct_fon)
    acc.paused = saved.get("paused", acc.paused)
    acc.models_mode = saved.get("models_mode", acc.models_mode)
    acc.premium_pct = saved.get("premium_pct", acc.premium_pct)
    acc.tol_pct = saved.get("tol_pct", acc.tol_pct)
    acc.sales_depth = saved.get("sales_depth", acc.sales_depth)
    acc.fresh_hours = saved.get("fresh_hours", acc.fresh_hours)
    acc.min_sales = saved.get("min_sales", acc.min_sales)
    acc.ref_percentile = saved.get("ref_percentile", acc.ref_percentile)
    acc.probe_limit = saved.get("probe_limit", acc.probe_limit)
    acc.probe_markets = saved.get("probe_markets", acc.probe_markets)
    acc.models_interval_h = saved.get("models_interval_h", acc.models_interval_h)
    acc.exclude_backdrops = saved.get("exclude_backdrops", [])
    acc.last_models_ts = saved.get("last_models_ts", acc.last_models_ts)
    acc.original_models = saved.get("original_models", {})
    return acc


def build_accounts() -> dict:
    persisted = load_persisted()
    accounts = {}
    for cfg in ACCOUNTS_CFG:
        name = cfg["name"]
        accounts[name] = make_account(name, cfg["api_token"], persisted.get(name))
    # аккаунты, добавленные через /addaccount: их нет в ACCOUNTS_JSON, токен лежит рядом с настройками
    for name, saved in persisted.items():
        if name in accounts or not saved.get("api_token"):
            continue
        accounts[name] = make_account(name, saved["api_token"], saved, dynamic=True)
        log.info("аккаунт %s поднят из сохранённых (добавлен командой)", name)
    return accounts


def authorized(update: Update) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True  # список не задан — доступ не ограничен (не рекомендуется в проде)
    return update.effective_chat.id in ALLOWED_CHAT_IDS


fmt_ago = menu.fmt_ago  # одна реализация на команды и на меню


def get_account_or_reply(accounts: dict, name: str):
    acc = accounts.get(name)
    return acc


async def _unknown_account_reply(update, accounts):
    await update.message.reply_text(f"Аккаунт не найден. Доступные: {', '.join(accounts)}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "/menu — интерактивное меню (всё то же самое, но кнопками)\n"
        "\n"
        "/status — сводка по аккаунтам\n"
        "/errors — сводка ошибок\n"
        "/subs — активные автобай-подписки\n"
        "/setmarkup <%> — наценка над floor для заказов на модели\n"
        "/setmarkupfon <%> — наценка над floor для заказов на фоны (подписки с заданным backdropNames)\n"
        "/automodels on|off|preview — автоподбор моделей: применять / не считать вовсе / "
        "считать и показывать в /models, ничего не меняя\n"
        "/setpremium <%> — насколько выше floor коллекции должна стоить модель, чтобы попасть в заказ\n"
        "/setpumptol <%> — пометка «цена задрана» в отчёте; на отбор не влияет\n"
        "/setpercentile <n> — какую долю самых дешёвых продаж не брать в расчёт (1-50, сейчас 20)\n"
        "/setsalesdepth <n> — сколько последних продаж смотреть (20/40/100)\n"
        "/setprobe <лимит> [маркетов] — сколько моделей доуточнять за проход (0 = все) и по скольким маркетам\n"
        "/setmodelsinterval <часы> — как часто пересматривать состав моделей (цены обновляются отдельно и чаще)\n"
        "/refreshmodels — пересмотреть состав моделей прямо сейчас (долго)\n"
        "/excludebackdrops <фон, фон> — не учитывать продажи этих фонов при расчёте цены\n"
        "/filters — понятным языком объяснить, как сейчас настроен отбор\n"
        "/models — что автоподбор выбрал и что отсеял в последний пересмотр\n"
        "/sales <коллекция>, <модель> — сами сделки, по которым бот оценил модель\n"
        "/restoremodels — вернуть подпискам ручные modelNames, какими они были до автоподбора\n"
        "/monochrome [подарок] — пары подарок+фон: где больше всего моделей влезает в один заказ\n"
        "/scan [выгода%] [мин_цена] [макс_цена] [fast] [missing] — найти листинги ниже реальной "
        "цены модели; fast — быстрый проход по дешёвому краю, missing — только коллекции, "
        "которых ещё нет в базе\n"
        "/scanstop — прервать идущий скан\n"
        "/watch [просадка%] [мин_цена] [макс_цена] [all] — дозор: гоняет по кругу одни листинги и ловит модели, чей флор только что ушёл вниз против своего же уровня. Круг — минуты, историю продаж не качает\n"
        "/watchstop — остановить дозор\n"
        "/colorsprobe <slug> — проверка на одной вещи: скачать её картинку и вынуть цвет фона и цвет модели\n"
        "/colors [снимков] [thin] [back] — собрать цвета всех моделей и фонов. К API — по три запроса на коллекцию, картинки идут с телеграма. thin — добрать модели, снятые с одного фона; back — пересобрать только фоны\n"
        "/colorsstop — остановить сбор\n"
        "/colorsbase — что накопила база цветов, файлом\n"
        "/match [допуск%] [мин_цена] [макс_цена] [совпадение%] [ΔE] — лоты, где фон подходит модели по цвету, а цена как у обычной. совпадение% — сколько площади модели должно совпасть с фоном\n"
        "/matchstop — остановить поиск\n"
        "/matchtable [совпадение%] [ΔE] — сама подборка: какой фон какой модели подходит и на сколько процентов площади. Запросов не делает\n"
        "/scanbase — что накопила база скана: коллекции, цены моделей, надбавки за фоны\n"
        "/scanpublish — выложить базу скана в GitHub (токены аккаунтов не отправляются)\n"
        "/forceupdate — пересчитать цены сейчас\n"
        "/setinterval <мин> — как часто (в минутах) проверяются актуальные цены; без аргумента — показать текущее значение\n"
        "/pause <acc> / /resume <acc> — остановить/возобновить конкретный аккаунт (acc обязателен)\n"
        "/addaccount <имя> <токен> — добавить аккаунт на ходу, без перезапуска\n"
        "/delaccount <имя> — убрать аккаунт, добавленный командой\n"
        "\n"
        "\n"
        "Почти все команды принимают <acc> первым аргументом, чтобы применить их к одному конкретному "
        "аккаунту, напр. /setmarkup acc1 5 или /status acc1. Без <acc> они работают сразу по всем аккаунтам."
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]

    if context.args:
        acc = get_account_or_reply(accounts, context.args[0])
        if not acc:
            await _unknown_account_reply(update, accounts)
            return
        lines = [
            f"📋 {acc.name}",
            f"Статус: {'⏸ на паузе' if acc.paused else '▶️ активен'}",
            f"Наценка (модели): +{acc.markup_pct}%",
            f"Наценка (фоны): +{acc.markup_pct_fon}%",
            f"Автоподбор моделей: {acc.models_mode} "
            f"(премия от +{acc.premium_pct:g}% к floor, проверка по реальным сделкам)",
            f"Последний запуск: {fmt_ago(acc.last_run_ts)}",
            f"Обновлено цен: {acc.last_updated_count}, пропущено: {acc.last_skipped_count}",
            f"Запросов к API за цикл: {acc.last_requests}",
            f"Ошибок в буфере: {len(acc.errors)}",
        ]
        if acc.errors:
            lines.append("Последние ошибки:")
            for ts, msg in list(acc.errors)[-3:]:
                t = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                lines.append(f"  [{t}] {msg[:150]}")
        await update.message.reply_text("\n".join(lines))
        return

    lines = ["📊 Все аккаунты:"]
    for acc in accounts.values():
        status_icon = "⏸" if acc.paused else "▶️"
        err_icon = f"⚠️{len(acc.errors)}" if acc.errors else "✅"
        lines.append(
            f"{status_icon} {acc.name}: обновлено {acc.last_updated_count} | "
            f"{fmt_ago(acc.last_run_ts)} | {err_icon}"
        )
    lines.append(f"\nХранилище настроек: {storage_status()}")
    await update.message.reply_text("\n".join(lines))


async def cmd_errors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /errors        — краткая сводка ошибок по ВСЕМ аккаунтам
    /errors <acc>  — полный список последних ошибок одного аккаунта
    """
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return

    if not context.args:
        lines = ["⚠️ Ошибки по всем аккаунтам:"]
        any_errors = False
        for acc in accounts.values():
            if not acc.errors:
                lines.append(f"[{acc.name}] ошибок нет ✅")
                continue
            any_errors = True
            ts, msg = acc.errors[-1]
            t = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M:%S")
            lines.append(f"[{acc.name}] {len(acc.errors)} ошиб. в буфере, последняя [{t}]: {msg[:150]}")
        if not any_errors:
            await update.message.reply_text("Ошибок нет ни у одного аккаунта ✅")
            return
        lines.append("\nПодробности: /errors <acc>")
        await update.message.reply_text("\n".join(lines))
        return

    acc = get_account_or_reply(accounts, context.args[0])
    if not acc:
        await _unknown_account_reply(update, accounts)
        return
    if not acc.errors:
        await update.message.reply_text(f"[{acc.name}] ошибок нет ✅")
        return
    lines = [f"⚠️ Ошибки [{acc.name}] (последние {len(acc.errors)}):"]
    for ts, msg in acc.errors:
        t = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M:%S")
        lines.append(f"[{t}] {msg[:500]}")
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):  # лимит телеграма на длину сообщения
        await update.message.reply_text(text[i:i + 4000])


async def cmd_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /subs        — активные автобай-подписки по ВСЕМ аккаунтам
    /subs <acc>  — активные автобай-подписки одного аккаунта
    """
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return

    targets = list(accounts.values()) if not context.args else None
    if targets is None:
        acc = get_account_or_reply(accounts, context.args[0])
        if not acc:
            await _unknown_account_reply(update, accounts)
            return
        targets = [acc]

    lines = []
    for acc in targets:
        try:
            subs = await asyncio.to_thread(acc.client.get_subscriptions)
        except Exception as e:
            acc.record_error(f"get_subscriptions (/subs): {e}")
            lines.append(f"[{acc.name}] ошибка запроса подписок: {e}")
            continue
        active = [s for s in subs if s.get("portalsAutobuy") and s.get("portalsAutobuyMaxPrice") is not None]
        if not active:
            lines.append(f"[{acc.name}] активных автобай-подписок нет")
            continue
        lines.append(f"[{acc.name}] активные автобай-подписки:")
        for s in active:
            lines.append(
                f"  • {s.get('subscriptionName')} ({s.get('collectionName')}): maxPrice={s.get('portalsAutobuyMaxPrice')} TON"
            )

    text = "\n".join(lines)
    for i in range(0, len(text), 4000):  # лимит телеграма на длину сообщения
        await update.message.reply_text(text[i:i + 4000])


async def _cmd_setmarkup_generic(update: Update, context: ContextTypes.DEFAULT_TYPE, attr_name: str, cmd_name: str, label: str):
    """
    <cmd_name> <%>        — установить наценку (attr_name) сразу для ВСЕХ аккаунтов
    <cmd_name> <acc> <%>  — установить наценку для одного конкретного аккаунта
    """
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return

    if not context.args:
        await update.message.reply_text(
            "Использование:\n"
            f"{cmd_name} <%> — {label}, для всех аккаунтов, напр. {cmd_name} 5\n"
            f"{cmd_name} <acc> <%> — {label}, для одного аккаунта, напр. {cmd_name} acc1 5"
        )
        return

    if len(context.args) == 1:
        # один аргумент = наценка, применяется ко всем аккаунтам
        try:
            pct = float(context.args[0])
        except ValueError:
            await update.message.reply_text("Наценка должна быть числом, напр. 3 или 4.5")
            return
        for acc in accounts.values():
            setattr(acc, attr_name, pct)
        save_persisted(accounts)
        await update.message.reply_text(
            f"Наценка ({label}) +{pct}% установлена для всех аккаунтов ({len(accounts)}): "
            + ", ".join(accounts)
        )
        return

    # два и более аргумента = <acc> <%>, применяется к одному аккаунту
    acc = get_account_or_reply(accounts, context.args[0])
    if not acc:
        await _unknown_account_reply(update, accounts)
        return
    try:
        pct = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Наценка должна быть числом, напр. 3 или 4.5")
        return
    setattr(acc, attr_name, pct)
    save_persisted(accounts)
    await update.message.reply_text(f"[{acc.name}] наценка ({label}) установлена: +{pct}%")


async def cmd_setmarkup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setmarkup — наценка над floor для заказов на модели (подписки без backdropNames)."""
    await _cmd_setmarkup_generic(update, context, "markup_pct", "/setmarkup", "модели")


async def cmd_setmarkupfon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setmarkupfon — наценка над floor для заказов на фоны (подписки с заданным backdropNames)."""
    await _cmd_setmarkup_generic(update, context, "markup_pct_fon", "/setmarkupfon", "фоны")


def _split_acc_args(accounts: dict, args: list):
    """
    Команды принимают либо '<acc> <аргументы...>', либо просто '<аргументы...>' (для всех аккаунтов).
    Возвращает (targets, rest, unknown_acc): unknown_acc=True, если первый аргумент похож на имя
    аккаунта (не число и не on/off), но такого аккаунта нет.
    """
    if args and args[0] in accounts:
        return [accounts[args[0]]], args[1:], False
    if args and not _looks_like_value(args[0]):
        return [], args, True
    return list(accounts.values()), args, False


def _looks_like_value(arg: str) -> bool:
    if arg.lower() in ("on", "off", "вкл", "выкл"):
        return True
    try:
        float(arg)
        return True
    except ValueError:
        return False


async def cmd_automodels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /automodels             — показать настройки автоподбора моделей
    /automodels on|off      — включить/выключить для всех аккаунтов
    /automodels <acc> on    — включить/выключить для одного аккаунта
    """
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return

    targets, rest, unknown = _split_acc_args(accounts, context.args)
    if unknown:
        await _unknown_account_reply(update, accounts)
        return

    if not rest:
        lines = ["🤖 Автоподбор моделей:"]
        for acc in targets:
            lines.append(
                f"[{acc.name}] режим: {acc.models_mode}\n"
                f"  премия: модель должна стоить от +{acc.premium_pct:g}% к floor коллекции\n"
                f"  отсев: по сделкам модель дешевле порога\n"
                f"  (допуск {acc.tol_pct:g}% на вердикт не влияет, только в отчёте)\n"
                f"  история: {acc.sales_depth} последних продаж, свежие {acc.fresh_hours:g}ч в базу не идут, "
                f"минимум {acc.min_sales} сделок\n"
                f"  добор цен: {'все модели' if not acc.probe_limit else f'до {acc.probe_limit} моделей'} "
                f"по {acc.probe_markets} маркет(ам)\n"
                f"  пересмотр состава: раз в {acc.models_interval_h:g}ч, "
                f"последний — {fmt_ago(acc.last_models_ts)}"
            )
        lines.append("\nВключить: /automodels on (или /automodels <acc> on)")
        await update.message.reply_text("\n".join(lines))
        return

    value = rest[0].lower()
    aliases = {"on": "on", "вкл": "on", "off": "off", "выкл": "off",
               "preview": "preview", "превью": "preview", "тест": "preview"}
    mode = aliases.get(value)
    if mode is None:
        await update.message.reply_text("Использование: /automodels [<acc>] on|off|preview")
        return

    for acc in targets:
        acc.models_mode = mode
    save_persisted(accounts)
    who = targets[0].name if len(targets) == 1 else f"всех аккаунтов ({len(targets)})"
    explain = {
        "off": "Отбор не считается вовсе — ни одного лишнего запроса, поведение как до автоподбора.",
        "preview": "Бот считает отбор и показывает его в /models, но modelNames НЕ трогает. "
                   "Цикл станет заметно дольше — это цена запросов к истории.",
        "on": "Бот будет переписывать modelNames подписок при каждом пересчёте. "
              "Прежние ручные списки сохранятся — вернуть их можно через /restoremodels.",
    }[mode]
    await update.message.reply_text(f"[{who}] режим автоподбора: {mode}\n{explain}")


async def _cmd_setnumber(update, context, field: str, cmd: str, example: str, describe):
    """Общий разбор для команд вида '<cmd> [<acc>] <число>'."""
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return

    targets, rest, unknown = _split_acc_args(accounts, context.args)
    if unknown:
        await _unknown_account_reply(update, accounts)
        return
    if not rest:
        await update.message.reply_text(f"Использование: {cmd} [<acc>] <число>, напр. {example}")
        return
    try:
        value = float(rest[0])
    except ValueError:
        await update.message.reply_text(f"Значение должно быть числом, напр. {example}")
        return
    if value <= 0:
        await update.message.reply_text("Значение должно быть больше нуля")
        return

    for acc in targets:
        setattr(acc, field, int(value) if isinstance(getattr(acc, field), int) else value)
    save_persisted(accounts)
    who = targets[0].name if len(targets) == 1 else f"всех аккаунтов ({len(targets)})"
    await update.message.reply_text(f"{describe(getattr(targets[0], field))} — для {who}.")


async def cmd_setpremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setpremium [<acc>] <%> — порог: насколько выше floor коллекции должна стоить модель."""
    await _cmd_setnumber(
        update, context, "premium_pct", "/setpremium", "/setpremium 50",
        lambda v: f"В заказ идут модели, стоящие от +{v:g}% к floor коллекции и выше",
    )


async def cmd_setpumptol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setpumptol [<acc>] <%> — допустимое превышение текущей цены над медианой продаж."""
    await _cmd_setnumber(
        update, context, "tol_pct", "/setpumptol", "/setpumptol 15",
        lambda v: f"Памп — если цена выше обычной цены модели более чем на {v:g}%",
    )


async def cmd_setpercentile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setpercentile [<acc>] <n> — по какой части ряда продаж считать обычную цену."""
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return

    targets, rest, unknown = _split_acc_args(accounts, context.args)
    if unknown:
        await _unknown_account_reply(update, accounts)
        return
    if not rest:
        await update.message.reply_text(
            "Использование: /setpercentile [<acc>] <n>, где n от 1 до 50\n\n"
            "Сколько процентов самых дешёвых продаж считать случайными сливами "
            "и не брать в расчёт обычной цены модели.\n"
            "20 — сейчас (из 20 сделок отбрасываются 4 самые дешёвые)\n"
            "50 — медиана, опора на середину ряда: оценка выше, но завышает\n"
            "10 — почти самый дешёвый край: строже, но пара случайных сливов "
            "начнёт ронять оценку\n\n"
            f"Сейчас: " + ", ".join(f"{a.name} {a.ref_percentile:g}" for a in targets)
        )
        return
    try:
        value = float(rest[0])
    except ValueError:
        await update.message.reply_text("Значение должно быть числом, напр. /setpercentile 20")
        return
    if not 1 <= value <= 50:
        await update.message.reply_text(
            "Значение должно быть от 1 до 50.\n"
            "Выше 50 смысла нет — это уже дороже середины ряда, а тебе по ордеру "
            "приезжает дешёвый край."
        )
        return

    for acc in targets:
        acc.ref_percentile = value
    save_persisted(accounts)
    who = targets[0].name if len(targets) == 1 else f"всех аккаунтов ({len(targets)})"
    skipped = round(20 * value / 100)
    await update.message.reply_text(
        f"Обычная цена модели считается по {value:g}-му процентилю — для {who}.\n"
        f"Из 20 продаж отбрасываются примерно {skipped} самых дешёвых"
        + (" (это медиана — опора на середину ряда)" if value == 50 else "")
        + "\nПрименится при следующем /refreshmodels"
    )


async def cmd_setsalesdepth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setsalesdepth [<acc>] <n> — сколько последних продаж смотреть."""
    await _cmd_setnumber(
        update, context, "sales_depth", "/setsalesdepth", "/setsalesdepth 100",
        lambda v: f"Смотрим последние {v} продаж модели ({-(-v // 20)} страниц истории)",
    )


async def cmd_setprobe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setprobe [<acc>] <лимит> [маркетов] — сколько моделей доуточнять и по скольким маркетам."""
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return

    targets, rest, unknown = _split_acc_args(accounts, context.args)
    if unknown:
        await _unknown_account_reply(update, accounts)
        return
    if not rest:
        await update.message.reply_text(
            "Использование: /setprobe [<acc>] <лимит> [маркетов]\n"
            "напр. /setprobe 0 3 — узнавать цену у ВСЕХ моделей коллекции (0 = без ограничения), "
            "по трём маркетам.\n"
            "Влияет только на редкий пересмотр состава моделей, не на обновление цен."
        )
        return
    try:
        limit = int(float(rest[0]))
        markets = int(float(rest[1])) if len(rest) > 1 else None
    except ValueError:
        await update.message.reply_text("Аргументы должны быть числами, напр. /setprobe 30 1")
        return
    if limit < 0:
        await update.message.reply_text("Лимит не может быть отрицательным (0 = без ограничения)")
        return
    if markets is not None and not 1 <= markets <= 3:
        await update.message.reply_text("Маркетов может быть от 1 до 3")
        return

    for acc in targets:
        acc.probe_limit = limit
        if markets is not None:
            acc.probe_markets = markets
    save_persisted(accounts)
    acc = targets[0]
    who = acc.name if len(targets) == 1 else f"всех аккаунтов ({len(targets)})"
    await update.message.reply_text(
        f"Добор цен для {who}: {'все модели' if not acc.probe_limit else f'до {acc.probe_limit} моделей'} "
        f"по {acc.probe_markets} маркет(ам)."
    )


ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


async def cmd_addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addaccount <имя> <токен> — добавить аккаунт на ходу, без перезапуска бота."""
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]

    if len(context.args) != 2:
        await update.message.reply_text(
            "Использование: /addaccount <имя> <токен>\n"
            "напр. /addaccount acc5 abcdef123456\n\n"
            "Имя — латиница, цифры, дефис или подчёркивание, до 32 символов.\n"
            "⚠️ Сообщение с токеном я удалю сразу после добавления, но токен всё равно "
            "проходит через серверы Telegram. Надёжнее держать аккаунты в ACCOUNTS_JSON."
        )
        return

    name, token = context.args[0], context.args[1]
    if not ACCOUNT_NAME_RE.match(name):
        await update.message.reply_text(
            "Имя должно быть из латиницы, цифр, дефиса или подчёркивания, до 32 символов "
            "(оно используется как аргумент в других командах, поэтому без пробелов)."
        )
        return
    if name in accounts:
        await update.message.reply_text(f"Аккаунт «{name}» уже есть. Удалить: /delaccount {name}")
        return

    acc = make_account(name, token, dynamic=True)
    try:
        me = await asyncio.to_thread(acc.client.get_me)
    except Exception as e:
        await update.message.reply_text(f"Токен не подошёл — аккаунт не добавлен.\n{str(e)[:300]}")
        return

    # токен принят: убираем сообщение с ним из чата
    deleted = True
    try:
        await update.message.delete()
    except Exception:
        deleted = False

    accounts[name] = acc  # тот же объект словаря держат джоба и меню, поэтому меняем на месте
    save_persisted(accounts)

    me = me or {}
    banned = " ⛔️ аккаунт забанен" if me.get("isBan") else ""
    await update.effective_chat.send_message(
        f"✅ Аккаунт «{name}» добавлен{banned}\n"
        f"Telegram: @{me.get('username', '—')} | баланс: {me.get('tonBalance', '—')} TON | "
        f"лимит подписок: {me.get('subscriptionLimit', '—')} | "
        f"автобай: {'вкл' if me.get('isAutobuyEnabled') else 'выкл'}\n\n"
        f"Настройки по умолчанию, автоподбор моделей выключен. Открыть: /menu\n"
        + ("" if deleted else "⚠️ Не смог удалить твоё сообщение с токеном — удали вручную.")
    )


async def cmd_delaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/delaccount <имя> — убрать аккаунт, добавленный командой."""
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not context.args:
        dynamic = [a.name for a in accounts.values() if a.dynamic]
        await update.message.reply_text(
            "Использование: /delaccount <имя>\n"
            + (f"Можно удалить: {', '.join(dynamic)}" if dynamic
               else "Удалять нечего — все аккаунты заданы в ACCOUNTS_JSON.")
        )
        return

    name = context.args[0]
    acc = accounts.get(name)
    if not acc:
        await _unknown_account_reply(update, accounts)
        return
    if not acc.dynamic:
        await update.message.reply_text(
            f"«{name}» задан в ACCOUNTS_JSON — убрать его можно только оттуда, "
            f"командой не получится. Чтобы бот его не трогал: /pause {name}"
        )
        return

    del accounts[name]
    save_persisted(accounts)
    await update.message.reply_text(
        f"Аккаунт «{name}» удалён вместе с сохранённым токеном.\n"
        f"Подписки самого аккаунта не тронуты — бот просто перестал им заниматься."
    )


async def cmd_excludebackdrops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/excludebackdrops [<acc>] <фон, фон> — не учитывать продажи этих фонов в медиане."""
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return

    # Названия фонов состоят из слов ("Onyx Black"), поэтому общий разбор
    # '<acc> <аргументы>' здесь ломался: первое слово принималось за имя
    # аккаунта и команда падала с "Аккаунт не найден". Аккаунт — только точное
    # совпадение; без него настройка применяется ко всем аккаунтам.
    args = list(context.args)
    if args and args[0] in accounts:
        targets, rest = [accounts[args[0]]], args[1:]
    else:
        targets, rest = list(accounts.values()), args

    if not rest:
        lines = ["Фоны, чьи продажи не идут в расчёт цены:"]
        for acc in targets:
            lines.append(f"[{acc.name}] " + (", ".join(acc.exclude_backdrops) or "— пусто, учитываются все"))
        lines.append("\nЗадать: /excludebackdrops Onyx Black, Deep Purple")
        lines.append("Очистить: /excludebackdrops -")
        lines.append("\nБез имени аккаунта настройка применяется сразу ко всем.")
        lines.append("Учтите: это влияет ТОЛЬКО на проверку моделей по истории продаж. "
                     "На расчёт цены не влияет, и на заказы с фонами тоже — они "
                     "через подбор моделей не проходят.")
        await update.message.reply_text("\n".join(lines))
        return

    raw = " ".join(rest)
    names = [] if raw.strip() in ("-", "нет", "clear") else [n.strip() for n in raw.split(",") if n.strip()]
    for acc in targets:
        acc.exclude_backdrops = names
    save_persisted(accounts)
    who = targets[0].name if len(targets) == 1 else f"всех аккаунтов ({len(targets)})"
    await update.message.reply_text(
        (f"Для {who} продажи этих фонов больше не учитываются: {', '.join(names)}"
         if names else f"Для {who} снова учитываются продажи всех фонов.")
        + "\nПрименится при следующем пересмотре: /refreshmodels"
    )


async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/filters [<acc>] — человеческим языком объяснить, как сейчас настроен отбор."""
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return

    targets, _, unknown = _split_acc_args(accounts, context.args)
    if unknown:
        await _unknown_account_reply(update, accounts)
        return

    # одно сообщение на все аккаунты: правила у них обычно одинаковые
    await update.message.reply_text(menu.filters_text(targets)[:4000])


async def cmd_setmodelsinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setmodelsinterval [<acc>] <часы> — как часто пересматривать состав моделей."""
    await _cmd_setnumber(
        update, context, "models_interval_h", "/setmodelsinterval", "/setmodelsinterval 48",
        lambda v: f"Состав моделей пересматривается раз в {v:g}ч ({v / 24:.1f} сут). "
                  f"Цены при этом обновляются каждым циклом, как и раньше",
    )


async def cmd_refreshmodels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/refreshmodels [<acc>] — пересмотреть состав моделей прямо сейчас, не дожидаясь расписания."""
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return

    targets, _, unknown = _split_acc_args(accounts, context.args)
    if unknown:
        await _unknown_account_reply(update, accounts)
        return

    off = [acc.name for acc in targets if acc.models_mode == "off"]
    work = [acc for acc in targets if acc.models_mode != "off"]
    if not work:
        await update.message.reply_text(
            "Автоподбор выключен у всех выбранных аккаунтов. "
            "Включи режим: /automodels preview (посчитать и показать) или /automodels on (применять)."
        )
        return

    # про пропущенные говорим сразу: молчаливый пропуск выглядит так, будто бот
    # оборвался на середине списка
    await update.message.reply_text(
        f"Запускаю полный пересмотр моделей: {', '.join(a.name for a in work)}.\n"
        + (f"⚠️ Пропускаю (автоподбор выключен): {', '.join(off)}\n"
           f"Включить: /automodels <acc> preview — или on, чтобы сразу применял.\n" if off else "")
        + "Это надолго — перебираются все модели всех коллекций. Отчёт пришлю по готовности."
    )
    done = []
    for acc in work:
        before = acc.last_models_ts
        ran = await asyncio.to_thread(run_cycle, acc, True, REFRESH_WAIT_SECONDS)
        if not ran:
            await update.message.reply_text(
                f"[{acc.name}] предыдущий цикл не закончился за "
                f"{REFRESH_WAIT_SECONDS // 60} мин — пропускаю. Повтори позже.")
            continue
        if acc.last_models_ts == before:
            # цикл оборвался до фазы моделей (обычно упал get_subscriptions).
            # Старый last_models при этом цел, и отчёт по нему выглядел бы как
            # свежий результат — поэтому вместо отчёта говорим, что не вышло.
            last = acc.errors[-1][1] if acc.errors else "причина неизвестна"
            await update.message.reply_text(
                f"[{acc.name}] пересмотр не состоялся — сервис не ответил.\n"
                f"{last[:300]}\n\nЗаказы не тронуты. Повтори, когда сервис поднимется."
            )
            continue
        done.append(acc)
        text = menu.refresh_summary_text(acc)
        for i in range(0, len(text), 4000):  # лимит телеграма на длину сообщения
            await update.message.reply_text(text[i:i + 4000])
    save_persisted(accounts)
    # полный разбор отбора сразу следом, чтобы не нажимать /models руками
    if done:
        await _send_models_report(context.bot, update.effective_chat.id, done)


async def cmd_restoremodels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/restoremodels [<acc>] — вернуть подпискам ручные modelNames, какими они были до автоподбора."""
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return

    targets, _, unknown = _split_acc_args(accounts, context.args)
    if unknown:
        await _unknown_account_reply(update, accounts)
        return

    lines = []
    for acc in targets:
        if not acc.original_models:
            lines.append(f"[{acc.name}] нечего восстанавливать — бот ещё не переписывал modelNames")
            continue
        if acc.models_mode == "on":
            lines.append(
                f"[{acc.name}] сначала выключи автоподбор: /automodels {acc.name} off — "
                f"иначе следующий цикл снова перепишет модели"
            )
            continue
        restored, failed = await asyncio.to_thread(_restore_models, acc)
        lines.append(f"[{acc.name}] восстановлено подписок: {restored}"
                     + (f", ошибок: {failed}" if failed else ""))
        if restored:
            save_persisted(accounts)
    await update.message.reply_text("\n".join(lines))


def _restore_models(acc):
    """Возвращает подпискам сохранённые ручные modelNames. Синхронно — из to_thread."""
    from updater import SUBSCRIPTION_BODY_FIELDS
    restored = failed = 0
    try:
        subs = acc.client.get_subscriptions()
    except Exception as e:
        acc.record_error(f"get_subscriptions (/restoremodels): {e}")
        return 0, 1

    for sub in subs:
        original = acc.original_models.get(sub["_id"])
        if original is None:
            continue
        body = {f: sub.get(f) for f in SUBSCRIPTION_BODY_FIELDS}
        body["modelNames"] = original
        if not body.get("numberPattern"):
            body.pop("numberPattern", None)
        try:
            acc.client.update_subscription(sub["_id"], body)
            acc.original_models.pop(sub["_id"], None)
            restored += 1
        except Exception as e:
            acc.record_error(f"[{sub.get('subscriptionName')}] restore modelNames: {e}")
            failed += 1
    return restored, failed


async def _send_models_report(bot, chat_id: int, targets: list):
    """
    Сводка по отбору плюс файл — ровно то, что отдаёт /models. Вынесено, чтобы
    отчёт после пересмотра приходил сам, а не только по ручной команде, и был
    при этом тем же самым, а не отдельной урезанной версией.
    """
    text = menu.models_summary_text(targets)
    for i in range(0, len(text), 4000):  # лимит телеграма на длину сообщения
        await bot.send_message(chat_id=chat_id, text=text[i:i + 4000])
    if not any(acc.last_models for acc in targets):
        return
    # \ufeff в начале — иначе Excel открывает кириллицу кракозябрами
    data = BytesIO(("\ufeff" + menu.models_report_csv(targets)).encode("utf-8"))
    scope = targets[0].name if len(targets) == 1 else "all"
    data.name = f"models_{scope}_{datetime.now():%Y-%m-%d_%H%M}.csv"
    await bot.send_document(chat_id=chat_id, document=data, filename=data.name)


async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/models [<acc>] — что автоподбор выбрал и что отсеял в последнем цикле."""
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return

    targets, _, unknown = _split_acc_args(accounts, context.args)
    if unknown:
        await _unknown_account_reply(update, accounts)
        return

    # одна сводка и один файл на все аккаунты, а не по паре сообщений на каждый
    await _send_models_report(context.bot, update.effective_chat.id, targets)


async def cmd_monochrome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /monochrome [<подарок>[, <подарок>...]] — у какого подарка и с каким фоном
    монохромны сразу много моделей.

    Смысл: цена у подписки одна на весь заказ, поэтому выгоднее та пара, под
    которую попадает максимум моделей — один ордер ловит их все под общий floor.
    """
    if not authorized(update):
        return
    if not WIKI_API_KEY:
        await update.message.reply_text(
            "Не задан WIKI_API_KEY — это ключ giftwiki со скоупом collection:read.\n"
            "Добавь его в переменные окружения на Render и перезапусти сервис."
        )
        return

    # аргументы: список подарков через запятую, плюс необязательное :type
    raw = " ".join(context.args)
    types = ["high", "combo"]
    if ":" in raw:
        raw, _, tail = raw.partition(":")
        types = [t.strip() for t in tail.replace(",", " ").split() if t.strip()] or types
    gifts = [g.strip() for g in raw.split(",") if g.strip()]

    scope = ", ".join(gifts) if gifts else "все подарки (постранично, это дольше)"
    await update.message.reply_text(f"Собираю монохромы: {scope}. Сочетания: {', '.join(types)}")

    def work():
        try:
            return monochrome.fetch(WIKI_API_KEY, gifts=gifts or None, types=types), None
        except monochrome.WikiBlocked as e:
            # запрос завернул Cloudflare, до API он не дошёл — другой способ
            # обхода тут не поможет, повторять смысла нет
            return None, f"blocked:{e}"
        except monochrome.WikiForbidden as e:
            # ключу приложения фильтр по названию подарка недоступен — идём постранично
            if gifts:
                try:
                    return monochrome.fetch(WIKI_API_KEY, types=types), "paged"
                except monochrome.WikiError as e2:
                    return None, str(e2)
            return None, str(e)
        except monochrome.WikiError as e:
            return None, str(e)

    result, note = await asyncio.to_thread(work)
    if result is None:
        if note and note.startswith("blocked:"):
            await update.message.reply_text(
                f"Запрос не дошёл до giftwiki: {note[len('blocked:'):]}\n\n"
                "Это защита перед их API, а не проблема ключа. Если повторяется — "
                "значит, они не пускают запросы с сервера Render."
            )
            return
        await update.message.reply_text(f"giftwiki не ответил: {note}")
        return
    records, client = result
    if note == "paged":
        await update.message.reply_text(
            "Ключу недоступен поиск по названию подарка — собрал весь список постранично."
        )

    rows = monochrome.rank(records)
    if gifts and note == "paged":
        wanted = {g.lower() for g in gifts}
        rows = [r for r in rows if r["gift"].lower() in wanted] or rows

    await update.message.reply_text(
        menu.monochrome_text(rows) + f"\n\nЗаписей {len(records)}, запросов {client.request_count}"
    )
    if not rows:
        return
    data = BytesIO(("\ufeff" + menu.monochrome_csv(rows)).encode("utf-8"))
    data.name = f"monochrome_{datetime.now():%Y-%m-%d_%H%M}.csv"
    await update.message.reply_document(document=data, filename=data.name)


async def cmd_sales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sales <коллекция>, <модель> — показать сами сделки, по которым бот оценил
    модель: дата, цена, фон, маркет и пометка, почему сделка не пошла в расчёт.

    В /models лежат только итоговые проценты, поэтому расхождение оценки с
    рынком по нему не разобрать — эта команда показывает исходные данные.
    """
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return

    # Здесь общий разбор '<acc> <аргументы>' не годится: названия коллекций
    # состоят из слов ("Love Candle"), и первое слово принималось за имя
    # аккаунта. Аккаунтом считаем только точное совпадение с известным именем.
    args = list(context.args)
    if args and args[0] in accounts:
        acc, args = accounts[args[0]], args[1:]
    else:
        # история одна на сервис, токен любого аккаунта её отдаст
        acc = next((a for a in accounts.values() if not a.paused), None) or \
            next(iter(accounts.values()))
    raw = " ".join(args)
    if "," not in raw:
        await update.message.reply_text(
            "Использование: /sales [<acc>] <коллекция>, <модель>\n"
            "Например: /sales Love Candle, Gray Smoke\n\n"
            "Запятая обязательна — в названиях есть пробелы. "
            "Имя аккаунта в начале необязательно: история одна на сервис."
        )
        return
    collection, _, model = raw.partition(",")
    collection, model = collection.strip(), model.strip()

    await update.message.reply_text(f"[{acc.name}] тяну сделки {collection} / {model}...")

    def work():
        # мимо кеша: смысл команды в том, чтобы увидеть, что сервис отдаёт прямо сейчас
        pages = max(1, -(-acc.sales_depth // HISTORY_PAGE_SIZE))
        sales, meta = [], {}
        for page in range(pages):
            data = acc.client.get_history(collection, models=[model], sort_by="date", page=page)
            content = (data or {}).get("content") or []
            if page == 0:
                meta = (data or {}).get("page") or {}
            sales += content
            total_pages = ((data or {}).get("page") or {}).get("totalPages")
            if not content or (total_pages is not None and page + 1 >= total_pages):
                break
        return sales, meta

    try:
        sales, meta = await asyncio.to_thread(work)
    except Exception as e:
        await update.message.reply_text(f"Не получилось: {e}")
        return
    if not sales:
        await update.message.reply_text(
            "Сервис не вернул ни одной продажи. Проверь названия — они должны "
            "совпадать с теми, что в /models, вплоть до регистра."
        )
        return

    text = menu.sales_dump_text(collection, model, sales, meta,
                                set(acc.exclude_backdrops), acc.fresh_hours,
                                time.time(), acc.ref_percentile)
    if len(text) < 3500:
        await update.message.reply_text(text)
    else:
        data = BytesIO(("\ufeff" + text).encode("utf-8"))
        data.name = f"sales_{model.replace(' ', '_')}_{datetime.now():%Y-%m-%d_%H%M}.txt"
        await update.message.reply_document(document=data, filename=data.name)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /scan [<acc>] [выгода%] [мин_цена] [макс_цена] — найти листинги, выставленные
    заметно ниже цены, по которой модель реально уходит.

    Проход по всем коллекциям идёт часами, поэтому находки уходят в чат
    порциями по мере готовности, а собранные цены сохраняются в Upstash —
    следующий прогон берёт их оттуда и укладывается в минуты.
    """
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return
    if context.bot_data.get("scan_running"):
        await update.message.reply_text(
            "Скан уже идёт. Остановить: /scanstop")
        return

    args = list(context.args)
    if args and args[0] in accounts:
        acc, args = accounts[args[0]], args[1:]
    else:
        acc = next((a for a in accounts.values() if not a.paused), None) or \
            next(iter(accounts.values()))

    def number(index, default):
        try:
            return float(args[index].replace(",", "."))
        except (IndexError, ValueError):
            return default

    # слова-переключатели можно ставить в любом месте после чисел
    words = {a.lower() for a in args}
    args = [a for a in args if a.lower() not in ("fast", "missing")]
    params = scanner.ScanParams(
        min_benefit_pct=number(0, 20.0),
        price_min=number(1, 0.0),
        price_max=number(2, 0.0),
        ref_percentile=acc.ref_percentile,
        fresh_hours=acc.fresh_hours,
        sales_depth=acc.sales_depth,
        # fast — не опрашивать модели поштучно: остаются только 50 самых
        # дешёвых лотов на коллекцию, зато весь рынок за минуты
        probe_all="fast" not in words,
        only_missing="missing" in words,
    )
    if params.price_max and params.price_max < params.price_min:
        await update.message.reply_text("Максимальная цена меньше минимальной.")
        return

    chat_id = update.effective_chat.id
    loop = asyncio.get_running_loop()
    context.bot_data["scan_running"] = True
    context.bot_data["scan_stop"] = False

    def say(text: str):
        # сканер живёт в отдельном потоке, отправка — в петле бота
        asyncio.run_coroutine_threadsafe(
            context.bot.send_message(chat_id=chat_id, text=text[:4000]), loop)

    await update.message.reply_text(
        f"🔎 Запускаю скан рынка.\n"
        f"Выгода от {params.min_benefit_pct:g}%"
        + (f", цена офера {params.price_min:g}–{params.price_max:g} TON"
           if params.price_max else f", цена офера от {params.price_min:g} TON") + "\n"
        f"Маркетов {len(params.markets)}, аккаунт {acc.name}."
        + ("\nБыстрый проход: только дешёвый край, модели поштучно не опрашиваю."
           if not params.probe_all else "")
        + ("\nТолько недостающие коллекции." if params.only_missing else "") + "\n\n"
        "Первый проход долгий — собирается база цен по всем моделям. Находки буду "
        "слать по ходу. Остановить: /scanstop"
    )

    saved = await asyncio.to_thread(load_scan_baseline)

    def persist(collected: dict):
        # к собранному подмешиваем то, что уже лежало: прогон мог идти по части
        # рынка, и коллекции, которых он не касался, терять нельзя
        merged = dict((saved or {}).get("collections") or {})
        merged.update(collected)
        full = {"ts": time.time(), "collections": merged}
        save_scan_baseline(full)
        # заодно выкладываем в GitHub, но не чаще раза в пять минут: каждая
        # выгрузка — коммит, и на длинном проходе их набежали бы сотни
        note = github_sync.publish_throttled(
            menu.scan_baseline_csv(full), f"скан: {len(merged)} коллекций")
        if note and not note.startswith("Выгружено"):
            log.warning("выгрузка базы: %s", note)

    def work():
        acc.client.request_count = 0
        return scanner.scan_market(
            acc.client, acc, params, baseline=saved,
            fetch_sales=lambda collection, model: fetch_sales_for(
                acc.client, collection, model, params.sales_depth, acc),
            on_progress=say,
            on_finds=lambda collection, finds: say(menu.scan_finds_text(collection, finds)),
            should_stop=lambda: context.bot_data.get("scan_stop"),
            on_baseline=persist,
        )

    try:
        result = await asyncio.to_thread(work)
    except Exception as e:
        log.exception("скан упал")
        await update.message.reply_text(f"Скан оборвался: {e}")
        return
    finally:
        context.bot_data["scan_running"] = False

    if result.get("error"):
        await update.message.reply_text(f"Не удалось получить список коллекций: {result['error']}")
        return

    if github_sync.enabled():
        baseline = await asyncio.to_thread(load_scan_baseline)
        note = await asyncio.to_thread(
            github_sync.publish, menu.scan_baseline_csv(baseline),
            f"скан: {len((baseline or {}).get('collections') or {})} коллекций")
        await update.message.reply_text(note)

    await update.message.reply_text(menu.scan_summary_text(result, params))
    finds = result.get("finds") or []
    if finds:
        data = BytesIO(("\ufeff" + menu.scan_report_csv(finds)).encode("utf-8"))
        data.name = f"scan_{datetime.now():%Y-%m-%d_%H%M}.csv"
        await update.message.reply_document(document=data, filename=data.name)


async def cmd_scanbase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/scanbase — что накопила база скана: коллекции, цены моделей, надбавки за фоны."""
    if not authorized(update):
        return
    baseline = await asyncio.to_thread(load_scan_baseline)
    await update.message.reply_text(menu.scan_baseline_text(baseline))
    if not (baseline or {}).get("collections"):
        return
    data = BytesIO(("\ufeff" + menu.scan_baseline_csv(baseline)).encode("utf-8"))
    data.name = f"scanbase_{datetime.now():%Y-%m-%d_%H%M}.csv"
    await update.message.reply_document(document=data, filename=data.name)


async def cmd_scanpublish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/scanpublish — выложить базу скана в GitHub, чтобы её можно было посмотреть со стороны."""
    if not authorized(update):
        return
    if not github_sync.enabled():
        await update.message.reply_text(
            "Выгрузка не настроена. Нужны переменные окружения:\n"
            "GITHUB_SYNC_TOKEN — токен с правом записи в репозиторий\n"
            "GITHUB_SYNC_REPO — owner/repo\n\n"
            "Уезжает только база скана (цены и надбавки за фоны). "
            "Настройки аккаунтов с токенами не отправляются никогда."
        )
        return
    baseline = await asyncio.to_thread(load_scan_baseline)
    if not (baseline or {}).get("collections"):
        await update.message.reply_text("База скана пуста — выгружать нечего.")
        return
    await update.message.reply_text("Выгружаю базу...")
    note = await asyncio.to_thread(
        github_sync.publish, menu.scan_baseline_csv(baseline),
        f"скан: {len(baseline['collections'])} коллекций")
    await update.message.reply_text(note)


async def cmd_scanstop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/scanstop — прервать идущий скан после текущей коллекции."""
    if not authorized(update):
        return
    if not context.bot_data.get("scan_running"):
        await update.message.reply_text("Скан сейчас не идёт.")
        return
    context.bot_data["scan_stop"] = True
    await update.message.reply_text(
        "Остановлю после текущей коллекции. Найденное и собранная база сохранятся.")


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /watch [<acc>] [просадка%] [мин_цена] [макс_цена] [all] — дозор по листингам.

    Скан ищет оферы дешевле цены по сделкам, и история продаж съедает в нём
    ~14 000 запросов на рынок против 393 на листинги: полный проход выходит
    часами, а значит на одну коллекцию скан смотрит раз в много часов. Дешёвый
    лот столько не живёт.

    Дозор историю не трогает вовсе: он гоняет по кругу одни листинги и сравнивает
    текущий флор модели с её же флором час назад. Круг — минуты, и просадку
    видно, пока она свежая.
    """
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return
    if context.bot_data.get("watch_running"):
        await update.message.reply_text("Дозор уже идёт. Остановить: /watchstop")
        return

    args = list(context.args)
    if args and args[0] in accounts:
        acc, args = accounts[args[0]], args[1:]
    else:
        acc = next((a for a in accounts.values() if not a.paused), None) or \
            next(iter(accounts.values()))

    words = {a.lower() for a in args}
    args = [a for a in args if a.lower() not in ("all",)]

    def number(index, default):
        try:
            return float(args[index].replace(",", "."))
        except (IndexError, ValueError):
            return default

    params = scanner.WatchParams(
        drop_pct=number(0, 20.0),
        price_min=number(1, 0.0),
        price_max=number(2, 0.0),
        # по умолчанию три основных маркета: круг тем быстрее, чем меньше
        # запросов, а tg и getgems дают считанные листинги
        markets=scanner.ALL_MARKETS if "all" in words else scanner.WATCH_MARKETS,
    )
    if params.price_max and params.price_max < params.price_min:
        await update.message.reply_text("Максимальная цена меньше минимальной.")
        return

    chat_id = update.effective_chat.id
    loop = asyncio.get_running_loop()
    context.bot_data["watch_running"] = True
    context.bot_data["watch_stop"] = False

    def say(text: str):
        asyncio.run_coroutine_threadsafe(
            context.bot.send_message(chat_id=chat_id, text=text[:4000]), loop)

    baseline = await asyncio.to_thread(load_scan_baseline)
    seed = await asyncio.to_thread(load_watch_levels)
    known = len((baseline or {}).get("collections") or {})
    if not known:
        context.bot_data["watch_running"] = False
        await update.message.reply_text(
            "База скана пуста — не из чего брать цены по сделкам и не по чему "
            "отсекать дорогие коллекции. Сначала /scan, хотя бы частично.")
        return

    def work():
        return scanner.watch_market(
            acc.client, acc, params, baseline=baseline,
            on_finds=lambda collection, finds: say(
                menu.watch_finds_text(collection, finds)),
            on_progress=say,
            on_pass=lambda stats: say(menu.watch_pass_text(stats)),
            should_stop=lambda: context.bot_data.get("watch_stop"),
            # замеры флоров переживают деплой: иначе дозор после каждого
            # перезапуска три круга молчит, набирая уровень заново
            on_levels=save_watch_levels,
            levels_seed=seed,
        )

    try:
        result = await asyncio.to_thread(work)
    except Exception as e:
        log.exception("дозор упал")
        await update.message.reply_text(f"Дозор оборвался: {e}")
        return
    finally:
        context.bot_data["watch_running"] = False

    if result.get("error"):
        await update.message.reply_text(f"Дозор не стартовал: {result['error']}")
        return
    await update.message.reply_text(
        f"👁 Дозор остановлен. Кругов {result['passes']}, "
        f"просадок найдено {result['finds']}.")


async def cmd_colors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /colors [<acc>] [снимков] — собрать цвета моделей и фонов по всему рынку.

    К gift-satellite идут только запросы за листингами — по три на коллекцию.
    Сами картинки качаются с телеграма и лимита API не касаются.

    Собранное сохраняется по ходу: проход идёт час, и обрыв не должен его
    обнулять. Повторный запуск не перекачивает то, что уже разобрано.
    """
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return
    if context.bot_data.get("colors_running"):
        await update.message.reply_text("Сбор цветов уже идёт. Остановить: /colorsstop")
        return

    args = list(context.args)
    if args and args[0] in accounts:
        acc, args = accounts[args[0]], args[1:]
    else:
        acc = next((a for a in accounts.values() if not a.paused), None) or \
            next(iter(accounts.values()))
    words = {a.lower() for a in args}
    args = [a for a in args if a.lower() not in ("thin", "back")]
    redo_thin = "thin" in words
    only_backdrops = "back" in words
    try:
        per_model = max(1, min(4, int(args[0])))
    except (IndexError, ValueError):
        per_model = colors.SAMPLES_PER_MODEL

    known = await asyncio.to_thread(load_colors)
    baseline = await asyncio.to_thread(load_scan_baseline)
    names = sorted((baseline or {}).get("collections") or {})
    if not names:
        try:
            names = sorted(c["name"] for c in (await asyncio.to_thread(acc.client.get_collections))
                           if isinstance(c, dict) and c.get("name"))
        except Exception as e:
            await update.message.reply_text(f"Не удалось получить список коллекций: {e}")
            return

    chat_id = update.effective_chat.id
    loop = asyncio.get_running_loop()
    context.bot_data["colors_running"] = True
    context.bot_data["colors_stop"] = False

    def say(text: str):
        asyncio.run_coroutine_threadsafe(
            context.bot.send_message(chat_id=chat_id, text=text[:4000]), loop)

    if only_backdrops:
        await update.message.reply_text(
            f"🎨 Пересобираю только фоны.\n"
            f"Коллекций: {len(names)}, замеров на фон: {colors.BACKDROP_TARGET}.\n"
            f"Фонов восемь десятков против почти пяти тысяч моделей, поэтому это "
            f"сотня картинок, а не восемь тысяч.\n"
            "Остановить: /colorsstop")
    else:
        await update.message.reply_text(
        f"🎨 Собираю цвета.\n"
        f"Коллекций: {len(names)}, снимков на модель: {per_model}.\n"
        f"Запросов к API — по три на коллекцию, картинки идут с телеграма.\n"
        f"Разные фоны на снимках нужны, чтобы отсеять узор: цвета модели "
        f"повторяются, цвета узора меняются вместе с фоном.\n"
        + ("Добор: беру только модели, снятые меньше чем с нужного числа фонов, "
           "и только лоты на фонах, которых у них ещё не было.\n" if redo_thin else "")
        + "Остановить: /colorsstop")

    def offers_for(collection):
        out = []
        for market in scanner.WATCH_MARKETS:
            try:
                out += scanner._offers_from_listings(
                    acc.client.search_market(market, collection), market)
            except Exception as e:
                acc.record_error(f"colors search {market}/{collection}: {e}")
        return out

    def work():
        if only_backdrops:
            return colors.collect_backdrops(
                offers_for, names, known=known, on_progress=say, on_save=save_colors,
                should_stop=lambda: context.bot_data.get("colors_stop"))
        return colors.collect(
            offers_for, names, per_model=per_model, known=known,
            on_progress=say, on_save=save_colors, redo_thin=redo_thin,
            should_stop=lambda: context.bot_data.get("colors_stop"))

    try:
        result = await asyncio.to_thread(work)
    except Exception as e:
        log.exception("сбор цветов упал")
        await update.message.reply_text(f"Сбор оборвался: {e}")
        return
    finally:
        context.bot_data["colors_running"] = False

    stats = result.get("stats") or {}
    if only_backdrops:
        head = (f"🎨 Готово.\nФонов в базе: {stats.get('backdrops', 0)}, "
                f"картинок скачано: {stats.get('images', 0)}, "
                f"ошибок: {stats.get('errors', 0)}\n\n")
    else:
        head = (f"🎨 Готово.\n"
                f"Новых моделей: {stats.get('models', 0)}, картинок скачано: "
                f"{stats.get('images', 0)}\n"
                f"Пропущено готовых: {stats.get('skipped', 0)}, "
                f"ошибок: {stats.get('errors', 0)}\n\n")
    await update.message.reply_text(head + menu.colors_text(result))
    await _send_colors_file(update, result)


async def cmd_colorsstop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/colorsstop — остановить сбор цветов."""
    if not authorized(update):
        return
    if not context.bot_data.get("colors_running"):
        await update.message.reply_text("Сбор цветов сейчас не идёт.")
        return
    context.bot_data["colors_stop"] = True
    await update.message.reply_text("Остановлю. Собранное сохранится.")


async def cmd_colorsbase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/colorsbase — что накопила база цветов, файлом."""
    if not authorized(update):
        return
    base = await asyncio.to_thread(load_colors)
    await update.message.reply_text(menu.colors_text(base))
    await _send_colors_file(update, base)


async def _send_colors_file(update: Update, base: dict):
    """Базу цветов файлом и, если настроено, в GitHub — чтобы её видно было со стороны."""
    if not (base or {}).get("models"):
        return
    text = menu.colors_csv(base)
    data = BytesIO(("\ufeff" + text).encode("utf-8"))
    data.name = f"colors_{datetime.now():%Y-%m-%d_%H%M}.csv"
    await update.message.reply_document(document=data, filename=data.name)
    if github_sync.enabled():
        note = await asyncio.to_thread(
            github_sync.publish, text,
            f"цвета: {sum(len(v) for v in base['models'].values() if isinstance(v, dict))} моделей",
            "scan/colors.csv")
        await update.message.reply_text(note)


async def cmd_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /match [<acc>] [допуск%] [мин_цена] [макс_цена] — лоты, где фон подходит
    модели по цвету, а цена как у обычной.

    За сочетание обычно платят. Но цену ставит человек, и ставит он её по
    модели, на фон часто не глядя — тогда вещь с подходящим фоном стоит
    столько же, сколько такая же с любым другим.
    """
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return
    if context.bot_data.get("match_running"):
        await update.message.reply_text("Поиск уже идёт. Остановить: /matchstop")
        return

    args = list(context.args)
    if args and args[0] in accounts:
        acc, args = accounts[args[0]], args[1:]
    else:
        acc = next((a for a in accounts.values() if not a.paused), None) or \
            next(iter(accounts.values()))

    def number(index, default):
        try:
            return float(args[index].replace(",", "."))
        except (IndexError, ValueError):
            return default

    params = scanner.MatchParams(
        tolerance_pct=number(0, 10.0),
        price_min=number(1, 0.0),
        price_max=number(2, 0.0),
        min_coverage=number(3, colors.MATCH_MIN_COVERAGE * 100) / 100,
        tol=number(4, colors.MATCH_TOL),
    )
    await update.message.reply_text("Читаю базу цветов…")
    base = await asyncio.to_thread(load_colors)
    if not (base or {}).get("backdrops"):
        await update.message.reply_text(
            "База цветов пуста — подбирать фоны не по чему. Сначала /colors")
        return
    baseline = await asyncio.to_thread(load_scan_baseline)

    chat_id = update.effective_chat.id
    loop = asyncio.get_running_loop()
    context.bot_data["match_running"] = True
    context.bot_data["match_stop"] = False

    def say(text: str):
        asyncio.run_coroutine_threadsafe(
            context.bot.send_message(chat_id=chat_id, text=text[:4000]), loop)

    def work():
        return scanner.scan_matches(
            acc.client, acc, params, base, baseline=baseline,
            on_finds=lambda collection, finds: say(
                menu.match_finds_text(collection, finds)),
            on_progress=say,
            should_stop=lambda: context.bot_data.get("match_stop"))

    try:
        result = await asyncio.to_thread(work)
    except Exception as e:
        log.exception("поиск сочетаний упал")
        await update.message.reply_text(f"Поиск оборвался: {e}")
        return
    finally:
        context.bot_data["match_running"] = False

    if result.get("error"):
        await update.message.reply_text(f"Не получилось: {result['error']}")
        return
    finds = result.get("finds") or []
    await update.message.reply_text(
        f"🎯 Готово.\n"
        f"Коллекций: {result['collections']}, моделей с подобранным фоном "
        f"проверено: {result['checked']}\n"
        f"Найдено лотов без наценки за фон: {len(finds)}"
        + (f"\nНе с чем было сравнить: {result['no_base']}" if result.get("no_base") else "")
        + (f"\nПропущено, потому что все лоты стоят на флоре: {result['flat']}"
           if result.get("flat") else ""))
    if finds:
        data = BytesIO(("\ufeff" + menu.match_report_csv(finds)).encode("utf-8"))
        data.name = f"match_{datetime.now():%Y-%m-%d_%H%M}.csv"
        await update.message.reply_document(document=data, filename=data.name)


async def cmd_matchstop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/matchstop — остановить поиск сочетаний."""
    if not authorized(update):
        return
    if not context.bot_data.get("match_running"):
        await update.message.reply_text("Поиск сейчас не идёт.")
        return
    context.bot_data["match_stop"] = True
    await update.message.reply_text("Остановлю после текущей коллекции.")


async def cmd_matchtable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/matchtable — сама подборка: какой фон какой модели подходит. Запросов не делает."""
    if not authorized(update):
        return
    args = list(context.args)

    def number(index, default):
        try:
            return float(args[index].replace(",", "."))
        except (IndexError, ValueError):
            return default

    min_coverage = number(0, colors.MATCH_MIN_COVERAGE * 100) / 100
    tol = number(1, colors.MATCH_TOL)
    # Отвечаем до расчёта, а не после: пар «цвет модели × фон» два с половиной
    # миллиона, и раньше команда молчала до самого результата.
    await update.message.reply_text(
        f"Подбираю: не меньше {min_coverage * 100:.0f}% площади при ΔE {tol:g}…")
    base = await asyncio.to_thread(load_colors)
    table = await asyncio.to_thread(colors.matching_table, base,
                                    colors.MATCH_TOP, tol, min_coverage)
    if not table:
        await update.message.reply_text(
            "Ни одна модель не набрала такого совпадения. Попробуй мягче: "
            "/matchtable 40 20")
        return
    total = sum(len(v) for v in (base.get("models") or {}).values() if isinstance(v, dict))
    await update.message.reply_text(
        f"🎨 Совпадение не меньше {min_coverage * 100:.0f}% площади при ΔE {tol:g}, "
        f"и главный цвет модели тоже должен попасть в этот порог.\n"
        f"Подошло {len(table)} моделей из {total}.\n"
        f"Фонов в базе: {len(colors.backdrop_colors(base))}.\n"
        f"Мягче — /matchtable 40 20, строже — /matchtable 70 10.")
    data = BytesIO(("\ufeff" + menu.match_table_csv(table)).encode("utf-8"))
    data.name = f"matchtable_{datetime.now():%Y-%m-%d_%H%M}.csv"
    await update.message.reply_document(document=data, filename=data.name)


async def cmd_colorsprobe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /colorsprobe <slug> — проверка на одной вещи: качается ли картинка и
    отделяется ли модель от фона.

    Слаг берётся из любого лота, напр. PlushPepe-274. Запросов к gift-satellite
    не делает вовсе — картинка качается с телеграма.
    """
    if not authorized(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Нужен слаг вещи, напр.: /colorsprobe PlushPepe-274\n"
            "Слаг виден в ссылке на любой лот.")
        return
    slug = context.args[0].strip().strip("/").split("/")[-1]
    await update.message.reply_text(f"Качаю {slug}…")
    try:
        got = await asyncio.to_thread(colors.probe, slug)
    except colors.ColorError as e:
        await update.message.reply_text(f"Не вышло: {e}")
        return
    except Exception as e:
        log.exception("colorsprobe упал")
        await update.message.reply_text(f"Сорвалось: {e}")
        return

    backdrop = got["backdrop_rgb"]
    palette = "\n".join(
        f"   {p['share'] * 100:4.0f}%  RGB {p['rgb']} — {colors.color_name(p['rgb'])}"
        for p in got["palette"])
    await update.message.reply_text(
        f"✅ {slug}\n"
        f"{got['how']}, {got['bytes'] // 1024} КБ\n"
        f"{got['url']}\n\n"
        f"фон  RGB {backdrop} — {colors.color_name(backdrop)}\n\n"
        f"цвета модели:\n{palette}\n\n"
        f"главный цвет против фона: ΔE {colors.delta_e(backdrop, got['model_rgb']):.0f}\n"
        f"модель заняла {got['coverage'] * 100:.0f}% центра\n\n"
        "Сверь с картинкой ниже: если цвета названы верно — разбор работает.")
    picture = BytesIO(got["image"])
    picture.name = f"{slug}.jpg"
    await update.message.reply_document(document=picture, filename=picture.name)


async def cmd_watchstop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/watchstop — остановить дозор после текущей коллекции."""
    if not authorized(update):
        return
    if not context.bot_data.get("watch_running"):
        await update.message.reply_text("Дозор сейчас не идёт.")
        return
    context.bot_data["watch_stop"] = True
    await update.message.reply_text("Остановлю после текущей коллекции.")


async def cmd_forceupdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /forceupdate         — пересчитать цены сразу для ВСЕХ аккаунтов (кроме тех, что на паузе)
    /forceupdate <acc>   — пересчитать цены для одного конкретного аккаунта
    """
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not accounts:
        await update.message.reply_text("Нет ни одного аккаунта")
        return

    if context.args:
        # указан acc — работаем только с ним, даже если он на паузе
        acc = get_account_or_reply(accounts, context.args[0])
        if not acc:
            await _unknown_account_reply(update, accounts)
            return
        await update.message.reply_text(f"[{acc.name}] запускаю пересчёт...")
        ran = await asyncio.to_thread(run_cycle, acc, False, FORCEUPDATE_WAIT_SECONDS)
        if not ran:
            await update.message.reply_text(
                f"Цикл не освободился за {FORCEUPDATE_WAIT_SECONDS // 60} мин — повтори позже.")
            return
        await update.message.reply_text(
            f"[{acc.name}] готово: обновлено {acc.last_updated_count}, "
            f"пропущено {acc.last_skipped_count}, запросов {acc.last_requests}, "
            f"ошибок в буфере: {len(acc.errors)}"
        )
        return

    # без аргументов — все аккаунты (кроме тех, что на паузе)
    active = [acc for acc in accounts.values() if not acc.paused]
    skipped_paused = len(accounts) - len(active)
    if not active:
        await update.message.reply_text("Все аккаунты на паузе, обновлять нечего")
        return

    await update.message.reply_text(
        f"Запускаю пересчёт для {len(active)} аккаунт(ов)"
        + (f" (пропущено на паузе: {skipped_paused})" if skipped_paused else "") + "..."
    )
    ran_any = False
    for acc in active:
        ran_any |= await asyncio.to_thread(run_cycle, acc, False, FORCEUPDATE_WAIT_SECONDS)

    if not ran_any:
        await update.message.reply_text(
            f"Цикл не освободился за {FORCEUPDATE_WAIT_SECONDS // 60} мин — повтори позже.")
        return

    lines = ["✅ Готово:"]
    for acc in active:
        lines.append(
            f"[{acc.name}] обновлено {acc.last_updated_count}, "
            f"пропущено {acc.last_skipped_count}, запросов {acc.last_requests}, "
            f"ошибок: {len(acc.errors)}"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not context.args:
        await update.message.reply_text("Использование: /pause <acc>")
        return
    acc = get_account_or_reply(accounts, context.args[0])
    if not acc:
        await _unknown_account_reply(update, accounts)
        return
    acc.paused = True
    save_persisted(accounts)
    await update.message.reply_text(f"[{acc.name}] на паузе ⏸")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not context.args:
        await update.message.reply_text("Использование: /resume <acc>")
        return
    acc = get_account_or_reply(accounts, context.args[0])
    if not acc:
        await _unknown_account_reply(update, accounts)
        return
    acc.paused = False
    save_persisted(accounts)
    await update.message.reply_text(f"[{acc.name}] возобновлён ▶️")


async def cmd_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setinterval        — показать текущий интервал проверки цен (в минутах)
    /setinterval <мин>  — сменить интервал проверки цен для всех аккаунтов
    """
    if not authorized(update):
        return

    current_seconds = context.bot_data.get("cycle_seconds", DEFAULT_CYCLE_SECONDS)
    if not context.args:
        await update.message.reply_text(
            f"Текущий интервал проверки цен: {current_seconds / 60:g} мин.\n"
            f"Чтобы изменить: /setinterval <мин>, напр. /setinterval 30"
        )
        return

    try:
        minutes = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Интервал должен быть числом в минутах, напр. 30 или 15.5")
        return
    if minutes < MIN_INTERVAL_MINUTES:
        await update.message.reply_text(f"Минимальный интервал — {MIN_INTERVAL_MINUTES} мин.")
        return

    new_seconds = minutes * 60
    accounts = context.bot_data["accounts"]

    # снимаем старую джобу и ставим новую с обновлённым интервалом
    for job in context.job_queue.get_jobs_by_name(CYCLE_JOB_NAME):
        job.schedule_removal()
    context.job_queue.run_repeating(
        scheduled_cycle,
        interval=new_seconds,
        first=new_seconds,
        data={"accounts": accounts},
        name=CYCLE_JOB_NAME,
    )

    context.bot_data["cycle_seconds"] = new_seconds
    save_global_settings({"cycle_seconds": new_seconds})

    await update.message.reply_text(f"Интервал проверки цен изменён: теперь каждые {minutes:g} мин.")


async def scheduled_cycle(context: ContextTypes.DEFAULT_TYPE):
    accounts = context.job.data["accounts"]
    # Пересмотр моделей идёт раз в models_interval_h и попадает не в каждый
    # цикл. Ловим его по last_models_ts: если отметка сдвинулась, состав
    # пересматривали — и отчёт нужно прислать, не дожидаясь ручного /models.
    refreshed = []
    for acc in accounts.values():
        if acc.paused:
            continue
        before = acc.last_models_ts
        await asyncio.to_thread(run_cycle, acc)
        if acc.last_models_ts != before:
            refreshed.append(acc)

    if not refreshed:
        return
    save_persisted(accounts)
    if not ALLOWED_CHAT_IDS:
        # слать некуда: чат не задан, и рассылать кому попало нельзя
        log.info("состав моделей пересмотрен (%s), но ALLOWED_CHAT_IDS пуст — "
                 "отчёт не отправляю, он доступен по /models",
                 ", ".join(a.name for a in refreshed))
        return
    for chat_id in ALLOWED_CHAT_IDS:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="🔄 Плановый пересмотр состава моделей: "
                     + ", ".join(a.name for a in refreshed))
            for acc in refreshed:
                text = menu.refresh_summary_text(acc)
                for i in range(0, len(text), 4000):
                    await context.bot.send_message(chat_id=chat_id, text=text[i:i + 4000])
            await _send_models_report(context.bot, chat_id, refreshed)
        except Exception as e:
            # упавшая отправка не должна ронять джобу и срывать следующий цикл
            log.warning("не смог отправить отчёт в чат %s: %s", chat_id, e)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        # UptimeRobot и подобные мониторы часто шлют HEAD, а не GET —
        # без этого метода BaseHTTPRequestHandler сам отвечает 501 Not Implemented
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass  # не засоряем логи каждым пингом от uptimerobot/render


def start_health_server():
    """Render (Web Service) и UptimeRobot дергают этот порт, чтобы сервис не засыпал."""
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Health-check сервер слушает порт %d", port)


# Показывается Telegram-ом во всплывающем меню по кнопке "/" в чате с ботом.
# Порядок в списке — тот же, в котором команды идут в /help.
BOT_COMMANDS = [
    ("menu", "Интерактивное меню на кнопках"),
    ("status", "Сводка по аккаунтам"),
    ("errors", "Сводка ошибок"),
    ("subs", "Активные автобай-подписки"),
    ("setmarkup", "Наценка над floor для заказов на модели"),
    ("setmarkupfon", "Наценка над floor для заказов на фоны"),
    ("automodels", "Автоподбор моделей: on|off|preview"),
    ("setpremium", "Порог премии модели над floor коллекции"),
    ("setpumptol", "Пометка «цена задрана» в отчёте, на отбор не влияет"),
    ("setpercentile", "Какую долю дешёвых продаж не брать в расчёт"),
    ("setsalesdepth", "Сколько последних продаж смотреть (20/40/100)"),
    ("setprobe", "Сколько моделей доуточнять за проход и по скольким маркетам"),
    ("setmodelsinterval", "Как часто пересматривать состав моделей"),
    ("refreshmodels", "Пересмотреть состав моделей прямо сейчас"),
    ("excludebackdrops", "Не учитывать продажи этих фонов при расчёте цены"),
    ("filters", "Понятным языком: как сейчас настроен отбор"),
    ("models", "Что автоподбор выбрал и что отсеял"),
    ("sales", "Сами сделки по модели: /sales <коллекция>, <модель>"),
    ("restoremodels", "Вернуть ручные modelNames до автоподбора"),
    ("monochrome", "Пары подарок+фон: где больше всего моделей в один floor"),
    ("scan", "Скан рынка: листинги ниже реальной цены модели"),
    ("scanstop", "Прервать идущий скан"),
    ("watch", "Дозор: свежие просадки флора"),
    ("watchstop", "Остановить дозор"),
    ("colorsprobe", "Проверить разбор цветов на одной вещи"),
    ("colors", "Собрать цвета моделей и фонов"),
    ("colorsstop", "Остановить сбор цветов"),
    ("colorsbase", "Что накопила база цветов"),
    ("match", "Подходящий фон без наценки"),
    ("matchstop", "Остановить поиск сочетаний"),
    ("matchtable", "Какой фон какой модели подходит"),
    ("scanbase", "Что накопила база скана"),
    ("scanpublish", "Выложить базу скана в GitHub"),
    ("forceupdate", "Пересчитать цены сейчас"),
    ("setinterval", "Как часто (в минутах) проверяются цены"),
    ("pause", "Остановить конкретный аккаунт"),
    ("resume", "Возобновить конкретный аккаунт"),
    ("addaccount", "Добавить аккаунт: /addaccount <имя> <токен>"),
    ("delaccount", "Удалить добавленный командой аккаунт"),
    ("help", "Список всех команд"),
]


async def _post_shutdown(app: Application):
    """
    Гасим скан при остановке процесса. Иначе он переживает деплой: Render
    поднимает новый инстанс раньше, чем гаснет старый, старый теряет связь с
    телеграмом (и с /scanstop), но продолжает ходить в API — в логах это
    выглядело как два прохода по разным коллекциям одновременно.
    """
    if not scanner.SHUTDOWN.is_set():
        scanner.SHUTDOWN.set()
        log.info("останавливаю скан: процесс гасится")


async def _post_init(app: Application):
    await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in BOT_COMMANDS])


def main():
    start_health_server()
    accounts = build_accounts()
    global_settings = load_global_settings()
    cycle_seconds = global_settings.get("cycle_seconds", DEFAULT_CYCLE_SECONDS)

    app = (Application.builder().token(TG_BOT_TOKEN)
           .post_init(_post_init).post_shutdown(_post_shutdown).build())
    app.bot_data["accounts"] = accounts
    app.bot_data["cycle_seconds"] = cycle_seconds
    # меню не импортирует bot.py (иначе вышел бы circular import), нужное отдаём через bot_data
    app.bot_data["authorized"] = authorized
    app.bot_data["restore_models"] = _restore_models

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("menu", menu.cmd_menu))
    app.add_handler(CallbackQueryHandler(menu.on_callback, pattern=r"^mn\|"))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("errors", cmd_errors))
    app.add_handler(CommandHandler("subs", cmd_subs))
    app.add_handler(CommandHandler("setmarkup", cmd_setmarkup))
    app.add_handler(CommandHandler("setmarkupfon", cmd_setmarkupfon))
    app.add_handler(CommandHandler("automodels", cmd_automodels))
    app.add_handler(CommandHandler("setpremium", cmd_setpremium))
    app.add_handler(CommandHandler("setpumptol", cmd_setpumptol))
    app.add_handler(CommandHandler("setpercentile", cmd_setpercentile))
    app.add_handler(CommandHandler("monochrome", cmd_monochrome, block=False))
    app.add_handler(CommandHandler("sales", cmd_sales, block=False))
    # block=False: иначе PTB разбирает обновления строго по одному, и пока
    # идёт долгая команда, остальные сообщения просто лежат в очереди — живой
    # случай: /scanstop молчал, потому что ждал окончания /scan
    app.add_handler(CommandHandler("scan", cmd_scan, block=False))
    app.add_handler(CommandHandler("scanstop", cmd_scanstop, block=False))
    app.add_handler(CommandHandler("watch", cmd_watch, block=False))
    app.add_handler(CommandHandler("watchstop", cmd_watchstop, block=False))
    app.add_handler(CommandHandler("colorsprobe", cmd_colorsprobe, block=False))
    app.add_handler(CommandHandler("colors", cmd_colors, block=False))
    app.add_handler(CommandHandler("colorsstop", cmd_colorsstop, block=False))
    app.add_handler(CommandHandler("colorsbase", cmd_colorsbase, block=False))
    app.add_handler(CommandHandler("match", cmd_match, block=False))
    app.add_handler(CommandHandler("matchstop", cmd_matchstop, block=False))
    app.add_handler(CommandHandler("matchtable", cmd_matchtable, block=False))
    app.add_handler(CommandHandler("scanbase", cmd_scanbase, block=False))
    app.add_handler(CommandHandler("scanpublish", cmd_scanpublish, block=False))
    app.add_handler(CommandHandler("setsalesdepth", cmd_setsalesdepth))
    app.add_handler(CommandHandler("setprobe", cmd_setprobe))
    app.add_handler(CommandHandler("setmodelsinterval", cmd_setmodelsinterval))
    app.add_handler(CommandHandler("refreshmodels", cmd_refreshmodels, block=False))
    app.add_handler(CommandHandler("excludebackdrops", cmd_excludebackdrops))
    app.add_handler(CommandHandler("filters", cmd_filters))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(CommandHandler("restoremodels", cmd_restoremodels))
    app.add_handler(CommandHandler("forceupdate", cmd_forceupdate, block=False))
    app.add_handler(CommandHandler("setinterval", cmd_setinterval))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("addaccount", cmd_addaccount))
    app.add_handler(CommandHandler("delaccount", cmd_delaccount))

    # Первый цикл — через полный интервал, а не сразу после старта. Иначе каждый
    # деплой немедленно запускал перебор, и команда, поданная следом, упиралась
    # в занятую блокировку.
    app.job_queue.run_repeating(
        scheduled_cycle,
        interval=cycle_seconds,
        first=cycle_seconds,
        data={"accounts": accounts},
        name=CYCLE_JOB_NAME,
    )

    log.info("Бот запущен, аккаунтов: %d, интервал проверки: %.1f мин. "
             "Первый цикл через %.1f мин.",
             len(accounts), cycle_seconds / 60, cycle_seconds / 60)
    app.run_polling()


if __name__ == "__main__":
    main()
