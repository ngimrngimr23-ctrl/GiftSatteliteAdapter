import os
import json
import asyncio
import logging
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from api_client import GiftApiClient
from state import AccountState, load_persisted, save_persisted
from updater import run_cycle

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
ALLOWED_CHAT_IDS = {int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip()}
CYCLE_SECONDS = int(os.environ.get("CYCLE_SECONDS", 3600))
GIFT_API_BASE_URL = os.environ.get("GIFT_API_BASE_URL")
ACCOUNTS_CFG = json.loads(os.environ["ACCOUNTS_JSON"])  # [{"name": "...", "api_token": "..."}, ...]


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
        "/status — сводка по всем аккаунтам\n"
        "/status <acc> — детали + последние ошибки\n"
        "/errors <acc> — полный список последних ошибок\n"
        "/subs <acc> — активные автобай-подписки и их цена\n"
        "/setmarkup <acc> <%> — сменить наценку над floor\n"
        "/forceupdate <acc> — пересчитать цены сейчас\n"
        "/pause <acc> / /resume <acc> — остановить/возобновить аккаунт"
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
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not context.args:
        await update.message.reply_text("Использование: /errors <acc>")
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
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not context.args:
        await update.message.reply_text("Использование: /subs <acc>")
        return
    acc = get_account_or_reply(accounts, context.args[0])
    if not acc:
        await _unknown_account_reply(update, accounts)
        return
    try:
        subs = await asyncio.to_thread(acc.client.get_subscriptions)
    except Exception as e:
        acc.record_error(f"get_subscriptions (/subs): {e}")
        await update.message.reply_text(f"Ошибка запроса подписок: {e}")
        return
    active = [s for s in subs if s.get("portalsAutobuy") and s.get("portalsAutobuyMaxPrice") is not None]
    if not active:
        await update.message.reply_text(f"[{acc.name}] активных автобай-подписок нет")
        return
    lines = [f"[{acc.name}] активные автобай-подписки:"]
    for s in active:
        lines.append(
            f"• {s.get('subscriptionName')} ({s.get('collectionName')}): maxPrice={s.get('portalsAutobuyMaxPrice')} TON"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_setmarkup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /setmarkup <acc> <%>, напр. /setmarkup acc1 5")
        return
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
    if not authorized(update):
        return
    accounts = context.bot_data["accounts"]
    if not context.args:
        await update.message.reply_text("Использование: /forceupdate <acc>")
        return
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


async def scheduled_cycle(context: ContextTypes.DEFAULT_TYPE):
    accounts = context.job.data["accounts"]
    for acc in accounts.values():
        if acc.paused:
            continue
        await asyncio.to_thread(run_cycle, acc)


def main():
    accounts = build_accounts()
    app = Application.builder().token(TG_BOT_TOKEN).build()
    app.bot_data["accounts"] = accounts

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("errors", cmd_errors))
    app.add_handler(CommandHandler("subs", cmd_subs))
    app.add_handler(CommandHandler("setmarkup", cmd_setmarkup))
    app.add_handler(CommandHandler("forceupdate", cmd_forceupdate))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))

    app.job_queue.run_repeating(
        scheduled_cycle, interval=CYCLE_SECONDS, first=10, data={"accounts": accounts}
    )

    log.info("Бот запущен, аккаунтов: %d", len(accounts))
    app.run_polling()


if __name__ == "__main__":
    main()
