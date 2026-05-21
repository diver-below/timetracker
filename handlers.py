from datetime import datetime, timedelta
from typing import Optional

from config import logger
from db import (
    UserState, get_or_create_user, get_current_state, update_state,
    create_session, end_session, save_task, get_user_tasks,
    create_reminder, get_active_reminders, decrypt_value,
    get_current_encrypted_task, update_user_name
)
from fsm import FSM, NO_KEYBOARD, WORKING_KEYBOARD, IDLE_KEYBOARD, ON_BREAK_KEYBOARD, CANCEL_KEYBOARD
from bot_api import send_message


fsm = FSM()


def parse_reminder_time(text: str) -> Optional[datetime]:
    text = text.strip().lower()

    now = datetime.utcnow()

    if "завтра" in text:
        base = now + timedelta(days=1)
        text = text.replace("завтра", "").strip()
    elif "через" in text:
        base = now
        text = text.replace("через", "").strip()
    else:
        base = now

    minutes = hours = None
    target_time = None

    for word in text.split():
        if word.isdigit():
            num = int(word)
        elif word.endswith("мин") or "min" in word:
            minutes = num if 'num' in locals() else int("".join(filter(str.isdigit, word)))
        elif word.endswith("час") or "h" in word or ":" in word:
            if ":" in word:
                parts = word.split(":")
                hours = int(parts[0])
                minutes = int(parts[1]) if len(parts) > 1 else 0
            else:
                hours = num if 'num' in locals() else int("".join(filter(str.isdigit, word)))

    if target_time is None:
        if hours is not None and minutes is not None:
            if "завтра" in text or (hours < now.hour and "завтра" not in text):
                base = now + timedelta(days=1)
            target_time = datetime(base.year, base.month, base.day, hours, minutes)
        elif minutes is not None and "через" in text:
            target_time = now + timedelta(minutes=minutes)

    if target_time:
        text_clean = text
        for num in str(hours or "") + str(minutes or ""):
            text_clean = text_clean.replace(num, "")
        text_clean = text_clean.replace(":", "").replace("мин", "").replace("час", "").replace("min", "").replace("h", "").strip()
        return target_time, text_clean

    return None


def format_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")


async def handle_start(user_login: str, user_id: int):
    await update_state(user_id, UserState.IDLE.value)
    await send_message(user_login, "Привет! Я бот для учёта рабочего времени.\n\nНажмите «Начать работу», чтобы приступить.", IDLE_KEYBOARD)
    logger.info(f"User {user_login} sent /start, reset to IDLE")


async def handle_begin_work(user_login: str, user_id: int):
    await update_state(user_id, UserState.ENTERING_TASK.value)
    await send_message(user_login, "Введите название задачи:", CANCEL_KEYBOARD)
    logger.info(f"User {user_login} entering task name")


async def handle_task_entry(user_login: str, user_id: int, task_name: str):
    if len(task_name.strip()) == 0:
        await send_message(user_login, "Название задачи не может быть пустым. Попробуйте ещё раз:", CANCEL_KEYBOARD)
        return

    await create_session(user_id, task_name)
    task_id = await save_task(user_id, task_name)
    await update_state(user_id, UserState.WORKING.value, task_id=task_id)

    await send_message(
        user_login,
        f"Задача «{task_name}» начата. Удачной работы!",
        WORKING_KEYBOARD
    )
    logger.info(f"User {user_login} started task: {task_name}")


async def handle_end_work(user_login: str, user_id: int):
    current_encrypted = await get_current_encrypted_task(user_id)
    task_name = decrypt_value(current_encrypted) if current_encrypted else "Текущая задача"

    await end_session(user_id)
    await update_state(user_id, UserState.IDLE.value)

    await send_message(
        user_login,
        f"Задача «{task_name}» завершена. Спасибо за работу!",
        IDLE_KEYBOARD
    )
    logger.info(f"User {user_login} ended work on task: {task_name}")


async def handle_break(user_login: str, user_id: int):
    await create_session(user_id, "Break")
    await update_state(user_id, UserState.ON_BREAK.value)

    await send_message(
        user_login,
        "Перерыв начат. Нажмите «Вернуться», когда будете готовы продолжить работу.",
        ON_BREAK_KEYBOARD
    )
    logger.info(f"User {user_login} went on break")


async def handle_return_from_break(user_login: str, user_id: int):
    await end_session(user_id)
    await update_state(user_id, UserState.WORKING.value)

    await send_message(
        user_login,
        "Добро пожаловать обратно! Продолжаем работу над задачей.",
        WORKING_KEYBOARD
    )
    logger.info(f"User {user_login} returned from break")


async def handle_switch_task(user_login: str, user_id: int):
    current_encrypted = await get_current_encrypted_task(user_id)
    old_task = decrypt_value(current_encrypted) if current_encrypted else "Текущая задача"

    await end_session(user_id)
    await update_state(user_id, UserState.ENTERING_TASK.value)

    await send_message(
        user_login,
        f"Задача «{old_task}» завершена.\n\nВведите название новой задачи:",
        CANCEL_KEYBOARD
    )
    logger.info(f"User {user_login} switching from task: {old_task}")


async def handle_new_reminder_start(user_login: str, user_id: int):
    await update_state(user_id, UserState.ENTERING_REMINDER.value)
    await send_message(
        user_login,
        "Напишите напоминание в формате: «<время> <текст>»\n"
        "Примеры:\n"
        "- через 15мин проверить почту\n"
        "- в 14:30 встреча\n"
        "- завтра 10:00 написать отчёт",
        CANCEL_KEYBOARD
    )
    logger.info(f"User {user_login} entering reminder")


async def handle_reminder_entry(user_login: str, user_id: int, text: str):
    result = parse_reminder_time(text)
    if not result:
        await send_message(
            user_login,
            "Не удалось распознать время. Попробуйте ещё раз в формате:\n"
            "- через 15мин текст\n"
            "- в 14:30 текст\n"
            "- завтра 10:00 текст",
            CANCEL_KEYBOARD
        )
        return

    scheduled_time, reminder_text = result
    await create_reminder(user_id, reminder_text, scheduled_time)
    await update_state(user_id, UserState.IDLE.value)

    await send_message(
        user_login,
        f"Напоминание «{reminder_text}» сработает в {format_time(scheduled_time)}.",
        IDLE_KEYBOARD
    )
    logger.info(f"User {user_login} created reminder: {reminder_text} at {scheduled_time}")


async def handle_list_reminders(user_login: str, user_id: int):
    reminders = await get_active_reminders(user_id)

    if not reminders:
        await send_message(user_login, "У вас нет активных напоминаний.", NO_KEYBOARD)
        return

    lines = ["Ваши напоминания:"]
    for r in reminders:
        lines.append(f"- {r.text} в {format_time(r.scheduled_time)}")

    await send_message(user_login, "\n".join(lines), NO_KEYBOARD)
    logger.info(f"User {user_login} listed reminders")


async def handle_cancel(user_login: str, user_id: int, previous_state: str):
    await update_state(user_id, UserState.IDLE.value)
    await send_message(user_login, "Действие отменено.", IDLE_KEYBOARD)
    logger.info(f"User {user_login} cancelled action from state: {previous_state}")


async def process_message(user_login: str, chat_id: str, text: str):
    user, is_new = await get_or_create_user(user_login)

    if user is None:
        await send_message(user_login, "Ошибка при регистрации. Пожалуйста, попробуйте снова.", NO_KEYBOARD)
        logger.error(f"Failed to create user: {user_login}")
        return

    if is_new:
        await send_message(
            user_login,
            "Добро пожаловать! Это ваш первый запуск.\n\n"
            "Пожалуйста, представьтесь (введите ваше имя):",
            NO_KEYBOARD
        )
        logger.info(f"New user registered: {user_login}")
        return

    if not user.name:
        await update_user_name(user.id, text.strip())
        await send_message(
            user_login,
            f"Рад знакомству, {text.strip()}!\n\n"
            "Нажмите «Начать работу», когда будете готовы приступить к задачам.",
            IDLE_KEYBOARD
        )
        logger.info(f"User {user_login} set name: {text.strip()}")
        return

    current_state, _ = await get_current_state(user.id)

    if text == "/start":
        await handle_start(user_login, user.id)
        return

    if current_state == UserState.ENTERING_TASK.value:
        if text == "Отмена":
            await handle_cancel(user_login, user.id, current_state)
        else:
            await handle_task_entry(user_login, user.id, text)
        return

    if current_state == UserState.ENTERING_REMINDER.value:
        if text == "Отмена":
            await handle_cancel(user_login, user.id, current_state)
        else:
            await handle_reminder_entry(user_login, user.id, text)
        return

    action = text

    if not fsm.is_valid_transition(current_state, action):
        await send_message(
            user_login,
            "Эта команда недоступна в текущем состоянии.",
            fsm.get_keyboard_for_state(current_state)
        )
        return

    if action == "Начать работу":
        await handle_begin_work(user_login, user.id)
    elif action == "Закончить":
        await handle_end_work(user_login, user.id)
    elif action == "Перерыв":
        await handle_break(user_login, user.id)
    elif action == "Вернуться":
        await handle_return_from_break(user_login, user.id)
    elif action == "Сменить задачу":
        await handle_switch_task(user_login, user.id)
    elif action == "/new_rem":
        await handle_new_reminder_start(user_login, user.id)
    elif action == "/list_rem":
        await handle_list_reminders(user_login, user.id)
    else:
        await send_message(
            user_login,
            "Не понимаю команду. Используйте кнопки клавиатуры.",
            fsm.get_keyboard_for_state(current_state)
        )