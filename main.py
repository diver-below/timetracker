import asyncio
import signal
import sys
from datetime import datetime, timedelta

from aiohttp import web
from sqlalchemy.ext.asyncio import async_sessionmaker

from config import validate_config, WEBHOOK_URL, LISTEN_PORT, YANDEX_OAUTH_TOKEN, logger
from db import (
    engine, init_db, get_due_reminders, mark_reminder_done, split_midnight_sessions, async_session_factory,
    get_users_for_workday_reminders, has_work_sessions_today, has_open_session, get_current_state,
    is_user_on_vacation
)
from handlers import process_message
from bot_api import parse_webhook_payload, send_message
from reports import send_daily_report_to_manager, get_managers_logins, send_weekly_report_to_manager
from fsm import FSM
import json
import os


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
    fsm = FSM()
    import db as db_module

    while True:
        try:
            due_reminders = await get_due_reminders()

            for reminder_id, text, user_login in due_reminders:
                if text.startswith("BREAK_TIMER:"):
                    keyboard = fsm.get_keyboard_for_state(UserState.ON_BREAK.value)
                    await send_message(user_login, "⏰ Перерыв длится уже 45 минут! Не забудьте вернуться к работе.", keyboard)
                else:
                    # Get user_id for this reminder
                    async with async_session_factory() as session:
                        result = await session.execute(
                            db_module.select(db_module.Reminder.user_id).where(db_module.Reminder.id == reminder_id)
                        )
                        user_id = result.scalar()

                        if user_id:
                            current_state, _ = await get_current_state(user_id)
                            keyboard = fsm.get_keyboard_for_state(current_state) if current_state else None
                            await send_message(user_login, f"⏰ Напоминание: {text}", keyboard)
                        else:
                            await send_message(user_login, f"⏰ Напоминание: {text}")

                await mark_reminder_done(reminder_id)
                logger.info(f"Sent reminder {reminder_id}: {text}")

        except Exception as e:
            logger.error(f"Error in reminder checker: {e}", exc_info=True)

        await asyncio.sleep(60)


async def midnight_session_checker():
    """Split sessions that span across midnight (users are GMT+3). Runs once per day at 21:00-21:05 UTC."""
    logger.info("Midnight session checker started")
    last_run_utc_date = None

    while True:
        try:
            now = datetime.utcnow()

            # Run once per day, at 21:00-21:05 UTC (which is 00:00-00:05 GMT+3)
            if now.hour == 21 and now.minute < 5:
                if last_run_utc_date != now.date():
                    count = await split_midnight_sessions()
                    if count > 0:
                        logger.info(f"Split {count} sessions at midnight (GMT+3)")
                    last_run_utc_date = now.date()

        except Exception as e:
            logger.error(f"Error in midnight session checker: {e}", exc_info=True)

        await asyncio.sleep(60)


async def daily_report_checker():
    """Send daily reports to all managers at 17:00 UTC (8pm GMT+3)."""
    logger.info("Daily report checker started")
    last_run_utc_date = None

    while True:
        try:
            now = datetime.utcnow()

            # Run once per day, at 17:00-17:05 UTC (which is 20:00-20:05 GMT+3)
            if now.hour == 17 and now.minute < 5:
                if last_run_utc_date != now.date():
                    managers = await get_managers_logins()
                    logger.info(f"Sending daily reports to {len(managers)} managers")

                    for user_id, user_login in managers:
                        try:
                            await send_daily_report_to_manager(user_login, user_id)
                        except Exception as e:
                            logger.error(f"Failed to send daily report to {user_login}: {e}", exc_info=True)

                    last_run_utc_date = now.date()

        except Exception as e:
            logger.error(f"Error in daily report checker: {e}", exc_info=True)

        await asyncio.sleep(60)


async def weekly_report_checker():
    """Send weekly reports to all managers at 21:01 UTC Monday (00:01 GMT+3 Monday)."""
    logger.info("Weekly report checker started")
    last_run_utc_week = None

    while True:
        try:
            now = datetime.utcnow()

            # Run once per week, Monday 21:01-21:05 UTC (which is 00:01-00:05 GMT+3 Monday)
            if now.weekday() == 0 and now.hour == 21 and 1 <= now.minute < 5:
                current_week = now.isocalendar()[1]  # ISO week number
                if last_run_utc_week != current_week:
                    managers = await get_managers_logins()
                    logger.info(f"Sending weekly reports to {len(managers)} managers")

                    for user_id, user_login in managers:
                        try:
                            await send_weekly_report_to_manager(user_login, user_id)
                        except Exception as e:
                            logger.error(f"Failed to send weekly report to {user_login}: {e}", exc_info=True)

                    last_run_utc_week = current_week

        except Exception as e:
            logger.error(f"Error in weekly report checker: {e}", exc_info=True)

        await asyncio.sleep(60)


async def workday_reminder_checker():
    """Check and send workday reminders every minute."""
    logger.info("Workday reminder checker started")
    fsm = FSM()
    import db as db_module

    # Track sent reminders per user per day: {user_id: {start_sent: "YYYY-MM-DD", end_sent: "YYYY-MM-DD"}}
    workday_reminders_sent = {}

    while True:
        try:
            # Skip if today is a holiday
            if is_today_holiday():
                await asyncio.sleep(60)
                continue

            users_for_reminders = await get_users_for_workday_reminders()
            today = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d")

            if not users_for_reminders:
                await asyncio.sleep(60)
                continue

            for user_data in users_for_reminders:
                user_id = user_data["user_id"]
                user_login = user_data["user_login"]
                reminder_type = user_data["reminder_type"]

                try:
                    # Skip if user is on vacation
                    if await is_user_on_vacation(user_id):
                        logger.debug(f"User {user_login} is on vacation, skipping reminder")
                        continue

                    # Initialize tracking for this user
                    if user_id not in workday_reminders_sent:
                        workday_reminders_sent[user_id] = {"start_sent": None, "end_sent": None}

                    # Check if already sent today
                    sent_date = workday_reminders_sent[user_id].get(f"{reminder_type}_sent")
                    if sent_date == today:
                        continue

                    if reminder_type == "start":
                        # Check if user has started work today
                        has_worked = await has_work_sessions_today(user_id)
                        if not has_worked:
                            start_time = user_data["scheduled_work_start"]
                            keyboard = fsm.get_keyboard_for_state("idle")
                            await send_message(
                                user_login,
                                f"⏰ Время начать работу! Ваше рабочее время: {start_time.strftime('%H:%M')}",
                                keyboard
                            )
                            workday_reminders_sent[user_id]["start_sent"] = today
                            logger.info(f"Sent start work reminder to {user_login}")

                    elif reminder_type == "end":
                        # Check if user has open session
                        has_open = await has_open_session(user_id)
                        if has_open:
                            end_time = user_data["scheduled_work_end"]
                            keyboard = fsm.get_keyboard_for_state("working")
                            await send_message(
                                user_login,
                                f"⏰ Рабочее время окончено! Не забудьте завершить задачу. Ваше время: до {end_time.strftime('%H:%M')}",
                                keyboard
                            )
                            workday_reminders_sent[user_id]["end_sent"] = today
                            logger.info(f"Sent end work reminder to {user_login}")

                except Exception as e:
                    logger.error(f"Failed to send workday reminder to {user_login}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error in workday reminder checker: {e}", exc_info=True)

        await asyncio.sleep(60)


def is_today_holiday() -> bool:
    """Check if today is a holiday based on holiday JSON file."""
    try:
        now = datetime.utcnow()
        gmt3_now = now + timedelta(hours=3)
        year = gmt3_now.year
        month = gmt3_now.month
        day = gmt3_now.day

        holidays_file = os.path.join(os.path.dirname(__file__), f"{year}.json")
        if not os.path.exists(holidays_file):
            logger.info(f"Holidays file {year}.json not found, treating as work day")
            return False

        with open(holidays_file, "r") as f:
            holidays = json.load(f)

        month_str = str(month)
        if month_str in holidays:
            return day in holidays[month_str]

        return False
    except Exception as e:
        logger.error(f"Error checking holidays: {e}")
        return False


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
    app["midnight_session_task"] = asyncio.create_task(midnight_session_checker())
    app["daily_report_task"] = asyncio.create_task(daily_report_checker())
    app["weekly_report_task"] = asyncio.create_task(weekly_report_checker())
    app["workday_reminder_task"] = asyncio.create_task(workday_reminder_checker())


async def on_shutdown(app: web.Application):
    if "reminder_task" in app:
        app["reminder_task"].cancel()
        try:
            await app["reminder_task"]
        except asyncio.CancelledError:
            pass

    if "midnight_session_task" in app:
        app["midnight_session_task"].cancel()
        try:
            await app["midnight_session_task"]
        except asyncio.CancelledError:
            pass

    if "daily_report_task" in app:
        app["daily_report_task"].cancel()
        try:
            await app["daily_report_task"]
        except asyncio.CancelledError:
            pass

    if "weekly_report_task" in app:
        app["weekly_report_task"].cancel()
        try:
            await app["weekly_report_task"]
        except asyncio.CancelledError:
            pass

    if "workday_reminder_task" in app:
        app["workday_reminder_task"].cancel()
        try:
            await app["workday_reminder_task"]
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