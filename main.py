import asyncio
import signal
import sys
from datetime import datetime, timedelta

from aiohttp import web
from sqlalchemy.ext.asyncio import async_sessionmaker

from config import validate_config, WEBHOOK_URL, LISTEN_PORT, YANDEX_OAUTH_TOKEN, logger
from db import engine, init_db, get_due_reminders, mark_reminder_done, async_session_factory
from handlers import process_message
from bot_api import parse_webhook_payload, send_message


async def webhook_handler(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        logger.info(f"Received webhook: {data}")

        user_login, chat_id, text = parse_webhook_payload(data)

        if not user_login or not chat_id:
            logger.warning("Missing user_login or chat_id in webhook")
            return web.json_response({"status": "error", "message": "Invalid payload"}, status=400)

        await process_message(user_login, chat_id, text)

        return web.json_response({"status": "ok"})
    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "healthy", "timestamp": datetime.utcnow().isoformat()})


async def reminder_checker():
    logger.info("Reminder checker started")
    while True:
        try:
            due_reminders = await get_due_reminders()

            for reminder_id, text, chat_id in due_reminders:
                await send_message(chat_id, f"⏰ Напоминание: {text}")
                await mark_reminder_done(reminder_id)
                logger.info(f"Sent reminder {reminder_id}: {text}")

        except Exception as e:
            logger.error(f"Error in reminder checker: {e}", exc_info=True)

        await asyncio.sleep(60)


async def register_webhook():
    import aiohttp

    # Yandex Bot API endpoint for setting webhooks
    webhook_api_url = "https://botapi.messenger.yandex.net/api/v1/setWebhook"

    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"OAuth {YANDEX_OAUTH_TOKEN}",
                "Content-Type": "application/json"
            }
            payload = {"webhook_url": WEBHOOK_URL}

            async with session.post(webhook_api_url, json=payload, headers=headers, timeout=10) as response:
                if response.status == 200:
                    logger.info(f"Webhook registered successfully: {WEBHOOK_URL}")
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to register webhook: {response.status} - {error_text}")
    except Exception as e:
        logger.error(f"Error registering webhook: {e}")


async def on_startup(app: web.Application):
    validate_config()
    await init_db()
    await register_webhook()
    logger.info("Bot started")
    app["reminder_task"] = asyncio.create_task(reminder_checker())


async def on_shutdown(app: web.Application):
    if "reminder_task" in app:
        app["reminder_task"].cancel()
        try:
            await app["reminder_task"]
        except asyncio.CancelledError:
            pass

    await engine.dispose()
    logger.info("Bot stopped")


def main():
    app = web.Application()
    app.router.add_post("/webhook", webhook_handler)
    app.router.add_get("/health", health_handler)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: sys.exit(0))

    web.run_app(app, host="0.0.0.0", port=LISTEN_PORT)


if __name__ == "__main__":
    main()