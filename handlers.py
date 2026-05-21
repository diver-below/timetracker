from datetime import datetime, timedelta
from typing import Optional
import traceback
import re

from config import logger
from db import (
    UserState, get_or_create_user, get_current_state, update_state,
    create_session, end_session, end_break_session, end_task_session, save_task, get_user_tasks,
    create_reminder, get_active_reminders, delete_all_user_reminders, decrypt_value,
    get_current_encrypted_task, update_user_name, get_task_name_by_id,
    get_user_roles, has_role, add_role, get_user_by_yandex_login, get_user_by_id
)
from fsm import FSM, NO_KEYBOARD, WORKING_KEYBOARD, IDLE_KEYBOARD, ON_BREAK_KEYBOARD, CANCEL_KEYBOARD
from bot_api import send_message

# Custom exceptions
class ValidationError(Exception):
    """Raised when user input is invalid."""
    pass

class DatabaseError(Exception):
    """Raised when database operation fails."""
    pass


fsm = FSM()

# In-memory tracking for users entering reminders/tasks
entering_reminder_users = set()
entering_task_users = set()


def parse_reminder_time(text: str) -> Optional[datetime]:
    text = text.strip().lower()

    now = datetime.utcnow()
    # User is in GMT+3, so get current local time
    now_local = now + timedelta(hours=3)

    # Save the check before modifying text
    has_cherez = "через" in text
    has_zavtra = "завтра" in text

    if has_zavtra:
        base = now + timedelta(days=1)
        text = text.replace("завтра", "").strip()
    elif has_cherez:
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
            if has_zavtra or (hours < now_local.hour and not has_zavtra):
                base = now + timedelta(days=1)
            # User time is GMT+3, convert to UTC by subtracting 3 hours
            target_time = datetime(base.year, base.month, base.day, hours, minutes) - timedelta(hours=3)
        elif minutes is not None and has_cherez:
            target_time = now + timedelta(minutes=minutes)

    if target_time:
        text_clean = text
        # Remove time pattern (HH:MM or HHччмин)
        text_clean = re.sub(r'\d{1,2}:\d{2}', '', text_clean)
        text_clean = re.sub(r'\d+\s*час', '', text_clean)
        text_clean = re.sub(r'\d+\s*мин', '', text_clean)
        # Remove keywords and extra spaces
        text_clean = re.sub(r'(через|мин|час|min|h|:)', '', text_clean, flags=re.IGNORECASE)
        text_clean = ' '.join(text_clean.split())
        return target_time, text_clean

    return None


def format_time(dt: datetime) -> str:
    # Convert UTC to GMT+3 for display
    local_dt = dt + timedelta(hours=3)
    return local_dt.strftime("%H:%M")


async def handle_start(user_login: str, user_id: int):
    entering_reminder_users.discard(user_login)
    entering_task_users.discard(user_login)
    await update_state(user_id, UserState.IDLE.value)
    await send_message(user_login, "Привет! Я бот для учёта рабочего времени.\n\nНажмите «Начать работу», чтобы приступить.", IDLE_KEYBOARD)
    logger.info(f"User {user_login} sent /start, reset to IDLE")


async def handle_begin_work(user_login: str, user_id: int):
    entering_task_users.add(user_login)
    await send_message(user_login, "Введите название задачи:", CANCEL_KEYBOARD)
    logger.info(f"User {user_login} entering task name")


async def handle_task_entry(user_login: str, user_id: int, task_name: str):
    if len(task_name.strip()) == 0:
        await send_message(user_login, "Название задачи не может быть пустым. Попробуйте ещё раз:", CANCEL_KEYBOARD)
        return

    entering_task_users.discard(user_login)
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
    # Get current task_id to preserve it
    _, current_task_id = await get_current_state(user_id)
    # Close the current task session before starting break
    await end_task_session(user_id)
    # Create break session
    await create_session(user_id, "Break")
    # Preserve task_id in CurrentStatus
    await update_state(user_id, UserState.ON_BREAK.value, task_id=current_task_id)

    await send_message(
        user_login,
        "Перерыв начат. Нажмите «Вернуться», когда будете готовы продолжить работу.",
        ON_BREAK_KEYBOARD
    )
    logger.info(f"User {user_login} went on break")


async def handle_return_from_break(user_login: str, user_id: int):
    # Get current task id from CurrentStatus
    _, current_task_id = await get_current_state(user_id)

    # Close only the break session
    await end_break_session(user_id)

    if current_task_id:
        # Get task name from user_tasks table
        task_name = await get_task_name_by_id(current_task_id)
        if task_name:
            # Create new session for the task
            await create_session(user_id, task_name)
            await update_state(user_id, UserState.WORKING.value, task_id=current_task_id)

            await send_message(
                user_login,
                f"Добро пожаловать обратно! Продолжаем работу над задачей «{task_name}».",
                WORKING_KEYBOARD
            )
            logger.info(f"User {user_login} returned from break, resumed task: {task_name}")
        else:
            # Task id exists but task not found - ask to enter a new task
            entering_task_users.add(user_login)
            await send_message(user_login, "Введите название задачи:", CANCEL_KEYBOARD)
            logger.info(f"User {user_login} returned from break but task {current_task_id} not found")
    else:
        # No task id - ask to enter a task name
        entering_task_users.add(user_login)
        await send_message(user_login, "Введите название задачи:", CANCEL_KEYBOARD)
        logger.info(f"User {user_login} returned from break with no current task")


async def handle_switch_task(user_login: str, user_id: int):
    current_encrypted = await get_current_encrypted_task(user_id)
    old_task = decrypt_value(current_encrypted) if current_encrypted else None

    await end_session(user_id)
    entering_task_users.add(user_login)

    if old_task:
        await send_message(
            user_login,
            f"Задача «{old_task}» завершена.\n\nВведите название новой задачи:",
            CANCEL_KEYBOARD
        )
    else:
        await send_message(
            user_login,
            "Введите название новой задачи:",
            CANCEL_KEYBOARD
        )
    logger.info(f"User {user_login} switching from task: {old_task}")


async def handle_new_reminder_start(user_login: str, user_id: int):
    entering_reminder_users.add(user_login)
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
    entering_reminder_users.discard(user_login)
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
        entering_reminder_users.add(user_login)  # Keep in reminder mode
        return

    scheduled_time, reminder_text = result
    await create_reminder(user_id, reminder_text, scheduled_time)

    # Get current state to show correct keyboard
    current_state, _ = await get_current_state(user_id)
    keyboard = fsm.get_keyboard_for_state(current_state)

    await send_message(
        user_login,
        f"Напоминание «{reminder_text}» сработает в {format_time(scheduled_time)}.",
        keyboard
    )
    logger.info(f"User {user_login} created reminder: {reminder_text} at {scheduled_time}")


async def handle_list_reminders(user_login: str, user_id: int):
    reminders = await get_active_reminders(user_id)

    if not reminders:
        await send_message(user_login, "У вас нет активных напоминаний.", CANCEL_KEYBOARD)
        return

    lines = ["Ваши напоминания:"]
    for r in reminders:
        lines.append(f"- {r.text} в {format_time(r.scheduled_time)}")

    await send_message(user_login, "\n".join(lines), CANCEL_KEYBOARD)
    logger.info(f"User {user_login} listed reminders")


async def handle_delete_reminders(user_login: str, user_id: int):
    count = await delete_all_user_reminders(user_id)
    current_state, _ = await get_current_state(user_id)
    keyboard = fsm.get_keyboard_for_state(current_state)

    if count == 0:
        message = "У вас нет активных напоминаний для удаления."
    elif count == 1:
        message = "1 напоминание удалено."
    else:
        message = f"{count} напоминаний удалено."

    await send_message(user_login, message, keyboard)
    logger.info(f"User {user_login} deleted {count} reminders")


async def handle_my_id(user_login: str, user_id: int):
    current_state, _ = await get_current_state(user_id)
    keyboard = fsm.get_keyboard_for_state(current_state)
    await send_message(user_login, f"Ваш ID: {user_id}", keyboard)
    logger.info(f"User {user_login} requested their ID: {user_id}")


async def handle_give_role(user_login: str, user_id: int, args: str):
    """Admin only: /give_role <user_id> <role>"""
    # Check if user has admin role
    is_admin = await has_role(user_id, "admin")
    if not is_admin:
        await send_message(user_login, "У вас нет прав для этой команды.")
        logger.warning(f"Non-admin user {user_login} tried to use /give_role")
        return

    # Parse arguments
    parts = args.strip().split()
    if len(parts) != 2:
        await send_message(user_login, "Формат: /give_role <user_id> <role>\nДоступные роли: admin, manager")
        return

    # Validate user_id is a number
    try:
        target_user_id = int(parts[0])
    except ValueError:
        await send_message(user_login, "Неверный формат user_id. Должно быть число.")
        return

    # Validate role
    role = parts[1].lower()
    if role not in ("admin", "manager"):
        await send_message(user_login, "Неверная роль. Доступные: admin, manager")
        return

    # Validate target user exists
    from db import get_user_by_id
    target_user = await get_user_by_id(target_user_id)
    if not target_user:
        await send_message(user_login, f"Пользователь с ID {target_user_id} не найден.")
        logger.info(f"Admin {user_login} tried to give role to non-existent user {target_user_id}")
        return

    # Add role to target user
    success = await add_role(target_user_id, role)
    if success:
        await send_message(user_login, f"Роль '{role}' добавлена пользователю {target_user_id} ({target_user.name}).")
        logger.info(f"Admin {user_login} added role '{role}' to user {target_user_id}")
    else:
        await send_message(user_login, f"Не удалось добавить роль. У пользователя {target_user_id} уже есть роль '{role}'.")
        logger.info(f"Admin {user_login} failed to add role '{role}' to user {target_user_id} - already has role")


async def handle_cancel(user_login: str, user_id: int, from_state: str):
    # Determine what state to return to based on what we were entering
    is_canceling_task = user_login in entering_task_users
    is_canceling_reminder = user_login in entering_reminder_users

    if is_canceling_task:
        entering_task_users.discard(user_login)

    if is_canceling_reminder:
        entering_reminder_users.discard(user_login)

    # Get current state and task_id from CurrentStatus
    current_state, current_task_id = await get_current_state(user_id)

    # For task entry: determine if returning to WORKING (had task) or IDLE (starting fresh)
    if from_state == UserState.ENTERING_TASK.value:
        if current_state == UserState.WORKING.value and current_task_id:
            return_state = UserState.WORKING.value
        else:
            return_state = UserState.IDLE.value
            await send_message(user_login, "Работа не начата. Чтобы начать работу введите название задачи:", CANCEL_KEYBOARD)
            logger.info(f"User {user_login} cancelled starting work")
            return
    else:
        return_state = current_state if current_state else UserState.IDLE.value

    # Don't call update_state - cancel doesn't change CurrentStatus

    # State names in Russian
    state_names = {
        UserState.IDLE.value: "не работает",
        UserState.WORKING.value: "работает",
        UserState.ON_BREAK.value: "на перерыве",
    }

    # Build message
    message = "Действие отменено. Текущий статус: " + state_names.get(return_state, return_state)

    # If working, show current task
    if return_state == UserState.WORKING.value:
        task_name = await get_task_name_by_id(current_task_id) if current_task_id else None
        if task_name:
            message += f"\nТекущая задача: {task_name}"

    # Get appropriate keyboard
    keyboard = fsm.get_keyboard_for_state(return_state)

    await send_message(user_login, message, keyboard)
    logger.info(f"User {user_login} cancelled action from {from_state}, returned to {return_state}")


async def process_message(user_login: str, chat_id: str, text: str):
    try:
        # Validate input
        if not user_login or not isinstance(user_login, str):
            raise ValidationError("Invalid user_login")
        if not text or not isinstance(text, str):
            raise ValidationError("Invalid text")
        if len(text) > 10000:
            raise ValidationError("Message too long")

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
            if text.strip():
                await update_user_name(user.id, text.strip())
                await send_message(
                    user_login,
                    f"Рад знакомству, {text.strip()}!\n\n"
                    "Нажмите «Начать работу», когда будете готовы приступить к задачам.",
                    IDLE_KEYBOARD
                )
                logger.info(f"User {user_login} set name: {text.strip()}")
            else:
                await send_message(user_login, "Пожалуйста, введите ваше имя.", NO_KEYBOARD)
            return

        current_state, _ = await get_current_state(user.id)

        if text == "/start":
            await handle_start(user_login, user.id)
            return

        if text == "/my_id":
            await handle_my_id(user_login, user.id)
            return

        if user_login in entering_task_users:
            if text == "Отмена":
                await handle_cancel(user_login, user.id, UserState.ENTERING_TASK.value)
            else:
                await handle_task_entry(user_login, user.id, text)
            return

        if user_login in entering_reminder_users:
            if text == "Отмена":
                await handle_cancel(user_login, user.id, UserState.ENTERING_REMINDER.value)
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
        elif action == "/del_rem":
            await handle_delete_reminders(user_login, user.id)
        elif action == "/my_id":
            await handle_my_id(user_login, user.id)
        elif action.startswith("/give_role"):
            await handle_give_role(user_login, user.id, action.replace("/give_role", ""))
        else:
            await send_message(
                user_login,
                "Не понимаю команду. Используйте кнопки клавиатуры.",
                fsm.get_keyboard_for_state(current_state)
            )

    except ValidationError as e:
        logger.warning(f"Validation error for {user_login}: {e}")
        await send_message(user_login, "Некорректный ввод. Пожалуйста, попробуйте снова.")
    except DatabaseError as e:
        logger.error(f"Database error for {user_login}: {e}")
        await send_message(user_login, "Ошибка сохранения данных. Попробуйте еще раз.")
    except Exception as e:
        logger.error(f"Unexpected error processing message from {user_login}: {e}\n{traceback.format_exc()}")
        await send_message(user_login, "Произошла непредвиденная ошибка. Попробуйте еще раз.")