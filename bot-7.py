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
        acc.paused = saved.get("paused", acc.paused)
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
        "/setmarkup <%> — сменить наценку над floor\n"
        "/forceupdate — пересчитать цены сейчас\n"
        "/setinterval <мин> — как часто (в минутах) проверяются актуальные цены; без аргумента — показать текущее значение\n"
        "/pause <acc> / /resume <acc> — остановить/возобновить конкретный аккаунт (acc обязателен)\n"
        "\n"
        "Для /status, /errors, /subs, /setmarkup, /forceupdate: добавь <acc> вторым (для /setmarkup — после наценки) "
        "аргументом, чтобы применить команду к одному конкретному аккаунту, напр. /setmarkup acc1 5 или /status acc1. "
        "Без <acc> они работают сразу по всем аккаунтам."
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
            f"Наценка: +{acc.markup_pct}%",
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


async def cmd_setmarkup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setmarkup <%>        — установить наценку сразу для ВСЕХ аккаунтов
    /setmarkup <acc> <%>  — установить наценку для одного конкретного аккаунта
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
            "/setmarkup <%> — для всех аккаунтов, напр. /setmarkup 5\n"
            "/setmarkup <acc> <%> — для одного аккаунта, напр. /setmarkup acc1 5"
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
            acc.markup_pct = pct
        save_persisted(accounts)
        await update.message.reply_text(f"Наценка +{pct}% установлена для всех аккаунтов ({len(accounts)})")
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
    acc.markup_pct = pct
    save_persisted(accounts)
    await update.message.reply_text(f"[{acc.name}] наценка установлена: +{pct}%")


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
