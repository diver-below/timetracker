import asyncio

from config import logger
from db import get_due_reminders, mark_reminder_done
from bot_api import send_message


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