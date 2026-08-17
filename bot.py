import os
import json
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import menu
from api_client import GiftApiClient
from state import AccountState, load_persisted, save_persisted, load_global_settings, save_global_settings
from updater import run_cycle

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
GIFT_API_BASE_URL = os.environ.get("GIFT_API_BASE_URL")
ACCOUNTS_CFG = json.loads(os.environ["ACCOUNTS_JSON"])  # [{"name": "...", "api_token": "..."}, ...]
CYCLE_JOB_NAME = "scheduled_cycle"


def build_accounts() -> dict:
    persisted = load_persisted()
    accounts = {}
    for cfg in ACCOUNTS_CFG:
        name = cfg["name"]
        kwargs = {}
        if GIFT_API_BASE_URL:
            kwargs["base_url"] = GIFT_API_BASE_URL
        client = GiftApiClient(cfg["api_token"], **kwargs)
        acc = AccountState(name=name, client=client)
        saved = persisted.get(name, {})
        acc.markup_pct = saved.get("markup_pct", acc.markup_pct)
        acc.markup_pct_fon = saved.get("markup_pct_fon", acc.markup_pct_fon)
        acc.paused = saved.get("paused", acc.paused)
        acc.models_mode = saved.get("models_mode", acc.models_mode)
        acc.premium_pct = saved.get("premium_pct", acc.premium_pct)
        acc.tol_pct = saved.get("tol_pct", acc.tol_pct)
        acc.sales_depth = saved.get("sales_depth", acc.sales_depth)
        acc.fresh_hours = saved.get("fresh_hours", acc.fresh_hours)
        acc.min_sales = saved.get("min_sales", acc.min_sales)
        acc.probe_limit = saved.get("probe_limit", acc.probe_limit)
        acc.probe_markets = saved.get("probe_markets", acc.probe_markets)
        acc.models_interval_h = saved.get("models_interval_h", acc.models_interval_h)
        acc.last_models_ts = saved.get("last_models_ts", acc.last_models_ts)
        acc.original_models = saved.get("original_models", {})
        accounts[name] = acc
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
        "/setpumptol <%> — насколько цена может превышать медиану продаж, прежде чем это памп\n"
        "/setsalesdepth <n> — сколько последних продаж смотреть (20/40/100)\n"
        "/setprobe <лимит> [маркетов] — сколько моделей доуточнять за проход (0 = все) и по скольким маркетам\n"
        "/setmodelsinterval <часы> — как часто пересматривать состав моделей (цены обновляются отдельно и чаще)\n"
        "/refreshmodels — пересмотреть состав моделей прямо сейчас (долго)\n"
        "/models — что автоподбор выбрал и что отсеял в последний пересмотр\n"
        "/restoremodels — вернуть подпискам ручные modelNames, какими они были до автоподбора\n"
        "/forceupdate — пересчитать цены сейчас\n"
        "/setinterval <мин> — как часто (в минутах) проверяются актуальные цены; без аргумента — показать текущее значение\n"
        "/pause <acc> / /resume <acc> — остановить/возобновить конкретный аккаунт (acc обязателен)\n"
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
            f"(премия от +{acc.premium_pct:g}%, памп при +{acc.tol_pct:g}% над историей)",
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
        lines.append(f"[{t}] {msg[:200]}")
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
        await update.message.reply_text(f"Наценка ({label}) +{pct}% установлена для всех аккаунтов ({len(accounts)})")
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
                f"  памп: цена выше медианы продаж более чем на {acc.tol_pct:g}%\n"
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
        lambda v: f"Памп — если цена выше медианы продаж более чем на {v:g}%",
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
    if len(off) == len(targets):
        await update.message.reply_text(
            "Автоподбор выключен у всех выбранных аккаунтов. "
            "Включи режим: /automodels preview (посчитать и показать) или /automodels on (применять)."
        )
        return

    await update.message.reply_text(
        f"Запускаю полный пересмотр моделей для {len(targets)} аккаунт(ов). "
        f"Это надолго — перебираются все модели всех коллекций. Отчёт пришлю по готовности."
    )
    for acc in targets:
        if acc.models_mode == "off":
            continue
        ran = await asyncio.to_thread(run_cycle, acc, True)
        if not ran:
            await update.message.reply_text(f"[{acc.name}] цикл уже идёт — повтори позже.")
            continue
        picked = sum(len(r["picked"]) for r in acc.last_models.values())
        applied = sum(1 for r in acc.last_models.values() if r.get("applied"))
        await update.message.reply_text(
            f"[{acc.name}] готово: подписок разобрано {len(acc.last_models)}, "
            f"моделей подобрано {picked}, подписок обновлено {applied}, "
            f"запросов {acc.last_requests}, ошибок {len(acc.errors)}\n"
            f"Подробности: /models {acc.name}"
        )
    save_persisted(accounts)


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

    text = "\n\n".join(menu.models_report_text(acc) for acc in targets)
    for i in range(0, len(text), 4000):  # лимит телеграма на длину сообщения
        await update.message.reply_text(text[i:i + 4000])


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
        ran = await asyncio.to_thread(run_cycle, acc)
        if not ran:
            await update.message.reply_text("Цикл уже идёт — дождись его окончания и повтори.")
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
        ran_any |= await asyncio.to_thread(run_cycle, acc)

    if not ran_any:
        await update.message.reply_text("Цикл уже идёт — дождись его окончания и повтори.")
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
    for acc in accounts.values():
        if acc.paused:
            continue
        await asyncio.to_thread(run_cycle, acc)


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
    ("setpumptol", "Допуск: насколько цена может превышать медиану продаж"),
    ("setsalesdepth", "Сколько последних продаж смотреть (20/40/100)"),
    ("setprobe", "Сколько моделей доуточнять за проход и по скольким маркетам"),
    ("setmodelsinterval", "Как часто пересматривать состав моделей"),
    ("refreshmodels", "Пересмотреть состав моделей прямо сейчас"),
    ("models", "Что автоподбор выбрал и что отсеял"),
    ("restoremodels", "Вернуть ручные modelNames до автоподбора"),
    ("forceupdate", "Пересчитать цены сейчас"),
    ("setinterval", "Как часто (в минутах) проверяются цены"),
    ("pause", "Остановить конкретный аккаунт"),
    ("resume", "Возобновить конкретный аккаунт"),
    ("help", "Список всех команд"),
]


async def _post_init(app: Application):
    await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in BOT_COMMANDS])


def main():
    start_health_server()
    accounts = build_accounts()
    global_settings = load_global_settings()
    cycle_seconds = global_settings.get("cycle_seconds", DEFAULT_CYCLE_SECONDS)

    app = Application.builder().token(TG_BOT_TOKEN).post_init(_post_init).build()
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
    app.add_handler(CommandHandler("setsalesdepth", cmd_setsalesdepth))
    app.add_handler(CommandHandler("setprobe", cmd_setprobe))
    app.add_handler(CommandHandler("setmodelsinterval", cmd_setmodelsinterval))
    app.add_handler(CommandHandler("refreshmodels", cmd_refreshmodels))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(CommandHandler("restoremodels", cmd_restoremodels))
    app.add_handler(CommandHandler("forceupdate", cmd_forceupdate))
    app.add_handler(CommandHandler("setinterval", cmd_setinterval))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))

    app.job_queue.run_repeating(
        scheduled_cycle,
        interval=cycle_seconds,
        first=10,
        data={"accounts": accounts},
        name=CYCLE_JOB_NAME,
    )

    log.info("Бот запущен, аккаунтов: %d, интервал проверки: %.1f мин.", len(accounts), cycle_seconds / 60)
    app.run_polling()


if __name__ == "__main__":
    main()
