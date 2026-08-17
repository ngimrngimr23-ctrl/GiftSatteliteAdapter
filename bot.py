import os
import json
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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
        acc.auto_models = saved.get("auto_models", acc.auto_models)
        acc.model_count = saved.get("model_count", acc.model_count)
        acc.pump_pct = saved.get("pump_pct", acc.pump_pct)
        acc.pump_window_h = saved.get("pump_window_h", acc.pump_window_h)
        acc.pump_cooldown_h = saved.get("pump_cooldown_h", acc.pump_cooldown_h)
        accounts[name] = acc
    return accounts


def authorized(update: Update) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True  # список не задан — доступ не ограничен (не рекомендуется в проде)
    return update.effective_chat.id in ALLOWED_CHAT_IDS


def fmt_ago(ts) -> str:
    if not ts:
        return "ещё не запускался"
    import time
    secs = int(time.time() - ts)
    if secs < 60:
        return f"{secs}с назад"
    if secs < 3600:
        return f"{secs // 60}м назад"
    return f"{secs // 3600}ч назад"


def get_account_or_reply(accounts: dict, name: str):
    acc = accounts.get(name)
    return acc


async def _unknown_account_reply(update, accounts):
    await update.message.reply_text(f"Аккаунт не найден. Доступные: {', '.join(accounts)}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "/status — сводка по аккаунтам\n"
        "/errors — сводка ошибок\n"
        "/subs — активные автобай-подписки\n"
        "/setmarkup <%> — наценка над floor для заказов на модели\n"
        "/setmarkupfon <%> — наценка над floor для заказов на фоны (подписки с заданным backdropNames)\n"
        "/automodels on|off — автоподбор моделей в заказ (без аргумента — показать настройки)\n"
        "/setmodelcount <n> — сколько моделей включать в заказ\n"
        "/setpump <%> [окно_ч] [кулдаун_ч] — порог пампа, окно расчёта и срок исключения модели\n"
        "/models — что автоподбор выбрал и что отсеял в последнем цикле\n"
        "/forceupdate — пересчитать цены сейчас\n"
        "/setinterval <мин> — как часто (в минутах) проверяются актуальные цены; без аргумента — показать текущее значение\n"
        "/pause <acc> / /resume <acc> — остановить/возобновить конкретный аккаунт (acc обязателен)\n"
        "\n"
        "Для /status, /errors, /subs, /setmarkup, /setmarkupfon, /automodels, /setmodelcount, /setpump, "
        "/models, /forceupdate: добавь <acc> первым аргументом, чтобы применить команду к одному конкретному "
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
            f"Автоподбор моделей: {'вкл' if acc.auto_models else 'выкл'} "
            f"(до {acc.model_count} шт., памп >{acc.pump_pct:g}% за {acc.pump_window_h:g}ч)",
            f"Последний запуск: {fmt_ago(acc.last_run_ts)}",
            f"Обновлено цен: {acc.last_updated_count}, пропущено: {acc.last_skipped_count}",
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
                f"[{acc.name}] {'включён ✅' if acc.auto_models else 'выключен ❌'} | "
                f"моделей в заказе: до {acc.model_count} | "
                f"памп: >{acc.pump_pct:g}% над медианой за {acc.pump_window_h:g}ч, "
                f"исключение на {acc.pump_cooldown_h:g}ч"
            )
        lines.append("\nВключить: /automodels on (или /automodels <acc> on)")
        await update.message.reply_text("\n".join(lines))
        return

    value = rest[0].lower()
    if value in ("on", "вкл"):
        enabled = True
    elif value in ("off", "выкл"):
        enabled = False
    else:
        await update.message.reply_text("Использование: /automodels [<acc>] on|off")
        return

    for acc in targets:
        acc.auto_models = enabled
    save_persisted(accounts)
    who = targets[0].name if len(targets) == 1 else f"всех аккаунтов ({len(targets)})"
    await update.message.reply_text(
        f"Автоподбор моделей {'включён ✅' if enabled else 'выключен ❌'} для {who}."
        + (
            "\nБот сам перепишет modelNames подписок при следующем пересчёте. "
            "Фильтр пампов заработает, когда накопится история цен (нужно ≥3 цикла)."
            if enabled else "\nmodelNames подписок останутся такими, какие есть сейчас."
        )
    )


async def cmd_setmodelcount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setmodelcount [<acc>] <n> — сколько моделей включать в заказ."""
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
        await update.message.reply_text("Использование: /setmodelcount [<acc>] <n>, напр. /setmodelcount 10")
        return
    try:
        count = int(float(rest[0]))
    except ValueError:
        await update.message.reply_text("Количество должно быть целым числом, напр. 10")
        return
    if count < 1:
        await update.message.reply_text("Количество моделей должно быть не меньше 1")
        return

    for acc in targets:
        acc.model_count = count
    save_persisted(accounts)
    who = targets[0].name if len(targets) == 1 else f"всех аккаунтов ({len(targets)})"
    await update.message.reply_text(f"В заказ будет включаться до {count} моделей — для {who}.")


async def cmd_setpump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setpump [<acc>] <%> [окно_ч] [кулдаун_ч] — настройки детектора пампов."""
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
            "Использование: /setpump [<acc>] <%> [окно_ч] [кулдаун_ч]\n"
            "напр. /setpump 20 24 12 — модель считается пампанувшей, если её floor выше "
            "медианы за 24ч более чем на 20%, и не попадает в заказ следующие 12ч."
        )
        return

    try:
        values = [float(x) for x in rest[:3]]
    except ValueError:
        await update.message.reply_text("Все аргументы должны быть числами, напр. /setpump 20 24 12")
        return
    if values[0] <= 0 or any(v <= 0 for v in values[1:]):
        await update.message.reply_text("Значения должны быть больше нуля")
        return

    for acc in targets:
        acc.pump_pct = values[0]
        if len(values) > 1:
            acc.pump_window_h = values[1]
        if len(values) > 2:
            acc.pump_cooldown_h = values[2]
    save_persisted(accounts)
    acc = targets[0]
    who = acc.name if len(targets) == 1 else f"всех аккаунтов ({len(targets)})"
    await update.message.reply_text(
        f"Детектор пампов для {who}: >{acc.pump_pct:g}% над медианой за {acc.pump_window_h:g}ч, "
        f"исключение из заказа на {acc.pump_cooldown_h:g}ч."
    )


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

    lines = []
    for acc in targets:
        if not acc.last_models:
            lines.append(
                f"[{acc.name}] данных нет — цикл ещё не отработал или нет заказов на модели. /forceupdate"
            )
            continue
        lines.append(f"[{acc.name}] {'автоподбор включён' if acc.auto_models else 'автоподбор ВЫКЛЮЧЕН (только анализ)'}:")
        for sub_name, rep in acc.last_models.items():
            lines.append(
                f"  • {sub_name}: моделей видно {rep['seen']}, "
                f"{'применено' if rep['applied'] else 'подобрано (не применено)'} {len(rep['picked'])}"
            )
            if rep["picked"]:
                lines.append("    ✅ " + ", ".join(rep["picked"]))
            if rep["pumped"]:
                pumped_details = []
                for model in rep["pumped"][:10]:
                    st = rep["stats"].get(model, {})
                    base = st.get("baseline")
                    if base:
                        pumped_details.append(f"{model} ({st['floor']:.2f} vs {base:.2f})")
                    else:
                        pumped_details.append(model)
                lines.append("    🚀 отсеяны как пампанувшие: " + ", ".join(pumped_details))
            if rep["too_expensive"]:
                lines.append(f"    💸 отсеяно как слишком дорогие относительно floor: {rep['too_expensive']}")
            no_history = sum(1 for st in rep["stats"].values() if st.get("baseline") is None)
            if no_history:
                lines.append(f"    ⏳ истории ещё мало для {no_history} моделей — памп по ним не считается")

    text = "\n".join(lines)
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
        await asyncio.to_thread(run_cycle, acc)
        await update.message.reply_text(
            f"[{acc.name}] готово: обновлено {acc.last_updated_count}, "
            f"пропущено {acc.last_skipped_count}, ошибок в буфере: {len(acc.errors)}"
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
    for acc in active:
        await asyncio.to_thread(run_cycle, acc)

    lines = ["✅ Готово:"]
    for acc in active:
        lines.append(
            f"[{acc.name}] обновлено {acc.last_updated_count}, "
            f"пропущено {acc.last_skipped_count}, ошибок: {len(acc.errors)}"
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


def main():
    start_health_server()
    accounts = build_accounts()
    global_settings = load_global_settings()
    cycle_seconds = global_settings.get("cycle_seconds", DEFAULT_CYCLE_SECONDS)

    app = Application.builder().token(TG_BOT_TOKEN).build()
    app.bot_data["accounts"] = accounts
    app.bot_data["cycle_seconds"] = cycle_seconds

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("errors", cmd_errors))
    app.add_handler(CommandHandler("subs", cmd_subs))
    app.add_handler(CommandHandler("setmarkup", cmd_setmarkup))
    app.add_handler(CommandHandler("setmarkupfon", cmd_setmarkupfon))
    app.add_handler(CommandHandler("automodels", cmd_automodels))
    app.add_handler(CommandHandler("setmodelcount", cmd_setmodelcount))
    app.add_handler(CommandHandler("setpump", cmd_setpump))
    app.add_handler(CommandHandler("models", cmd_models))
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
