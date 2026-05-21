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

        try:
            await process_message(user_login, chat_id, text)
            return web.json_response({"status": "ok"})
        except Exception as e:
            logger.error(f"Error processing message from {user_login}: {e}", exc_info=True)
            # Send error message to user if possible
            try:
                await send_message(user_login, "Произошла ошибка. Попробуйте еще раз или напишите администратору.")
            except Exception as send_error:
                logger.error(f"Failed to send error message to {user_login}: {send_error}")
            return web.json_response({"status": "error", "message": "Processing failed"}, status=200)  # 200 so Yandex doesn't retry

    except Exception as e:
        logger.error(f"Error in webhook handler: {e}", exc_info=True)
        return web.json_response({"status": "error", "message": str(e)}, status=200)  # 200 to avoid Yandex retries


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "healthy", "timestamp": datetime.utcnow().isoformat()})


async def test_yandex_api_handler(request: web.Request) -> web.Response:
    """Test connectivity to Yandex Bot API"""
    import aiohttp
    import ssl

    try:
        ssl_context = ssl.create_default_context()
        connector = aiohttp.TCPConnector(ssl=ssl_context)

        async with aiohttp.ClientSession(connector=connector) as session:
            # Simple test request to check if we can reach Yandex
            async with session.get("https://botapi.messenger.yandex.net/", timeout=5) as response:
                result = {
                    "status": "ok",
                    "yandex_reachable": True,
                    "response_status": response.status
                }
                return web.json_response(result)
    except Exception as e:
        return web.json_response({
            "status": "error",
            "yandex_reachable": False,
            "error": str(e),
            "error_type": type(e).__name__
        })


async def reminder_checker():
    logger.info("Reminder checker started")
    while True:
        try:
            due_reminders = await get_due_reminders()

            for reminder_id, text, user_login in due_reminders:
                await send_message(user_login, f"⏰ Напоминание: {text}")
                await mark_reminder_done(reminder_id)
                logger.info(f"Sent reminder {reminder_id}: {text}")

        except Exception as e:
            logger.error(f"Error in reminder checker: {e}", exc_info=True)

        await asyncio.sleep(60)


async def poll_pending_updates():
    """Get pending updates that were missed while bot was offline"""
    import aiohttp

    updates_url = "https://botapi.messenger.yandex.net/bot/v1/updates/"
    ack_url_template = "https://botapi.messenger.yandex.net/bot/v1/updates/{}/ack/"

    headers = {
        "Authorization": f"OAuth {YANDEX_OAUTH_TOKEN}"
    }

    try:
        async with aiohttp.ClientSession() as session:
            # Get pending updates
            async with session.get(updates_url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    updates = data.get("updates", [])
                    logger.info(f"Found {len(updates)} pending updates")

                    for update in updates:
                        update_id = update.get("update_id")
                        if not update_id:
                            continue

                        # Process the update
                        try:
                            user_login, chat_id, text = parse_webhook_payload({"updates": [update]})
                            if user_login and chat_id:
                                await process_message(user_login, chat_id, text)
                                logger.info(f"Processed pending update {update_id}")
                        except Exception as e:
                            logger.error(f"Error processing pending update {update_id}: {e}")

                        # Acknowledge the update
                        ack_url = ack_url_template.format(update_id)
                        async with session.get(ack_url, headers=headers, timeout=5) as ack_response:
                            if ack_response.status == 200:
                                logger.info(f"Acknowledged update {update_id}")
                            else:
                                logger.warning(f"Failed to acknowledge update {update_id}: {ack_response.status}")
                else:
                    logger.warning(f"Failed to get pending updates: {response.status}")
    except Exception as e:
        logger.error(f"Error polling pending updates: {e}")


async def register_webhook():
    import aiohttp

    # Yandex Bot API endpoint for setting webhooks
    webhook_api_url = "https://botapi.messenger.yandex.net/bot/v1/self/update/"

    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"OAuth {YANDEX_OAUTH_TOKEN}",
                "Content-Type": "application/json"
            }
            payload = {"webhook_url": WEBHOOK_URL}

            logger.info(f"Registering webhook: {webhook_api_url}, payload: {payload}")
            async with session.post(webhook_api_url, json=payload, headers=headers, timeout=10) as response:
                logger.info(f"Webhook registration response status: {response.status}")
                if response.status == 200:
                    result = await response.text()
                    logger.info(f"Webhook registered successfully: {WEBHOOK_URL}, response: {result}")
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to register webhook: {response.status} - {error_text}")
    except Exception as e:
        logger.error(f"Error registering webhook: {e}")


async def on_startup(app: web.Application):
    validate_config()
    await init_db()
    await poll_pending_updates()  # Get messages sent while bot was offline
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
    app.router.add_get("/test-yandex-api", test_yandex_api_handler)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: sys.exit(0))

    web.run_app(app, host="0.0.0.0", port=LISTEN_PORT)


if __name__ == "__main__":
    main()