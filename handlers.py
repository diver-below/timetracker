from datetime import datetime, timedelta, time
from typing import Optional
import traceback
import re

from config import logger
from db import (
    UserState, get_or_create_user, get_current_state, update_state,
    create_session, end_session, end_break_session, end_task_session, save_task, get_user_tasks,
    create_reminder, get_active_reminders, delete_all_user_reminders, delete_break_reminders, decrypt_value,
    get_current_encrypted_task, update_user_name, get_task_name_by_id,
    get_user_roles, has_role, add_role, remove_role, get_user_by_yandex_login, get_user_by_id,
    get_today_sessions, get_user_state_info, update_user_work_times, toggle_vacation
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
switching_task_context = {}  # {user_login: (old_task_id, old_task_name)}
entering_work_start_users = set()
entering_work_end_users = {}
setting_work_time_users = set()  # users using /setworktime command
setting_work_time_context = {}  # {user_login: start_time}


def parse_work_time(text: str) -> Optional[time]:
    """Parse work time in format HH:MM (e.g., '9:00', '14:30')"""
    text = text.strip().replace(".", ":")

    try:
        parts = text.split(":")
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0

        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            return time(hour=hours, minute=minutes)
    except (ValueError, IndexError):
        pass

    return None


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


def format_duration(seconds: int) -> str:
    """Format duration in seconds to readable format like '1ч30мин', '30мин'."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    if hours > 0 and minutes > 0:
        return f"{hours}ч{minutes}мин"
    elif hours > 0:
        return f"{hours}ч"
    elif minutes > 0:
        return f"{minutes}мин"
    else:
        return "0мин"


async def handle_start(user_login: str, user_id: int):
    entering_reminder_users.discard(user_login)
    entering_task_users.discard(user_login)

    # Clean up switching context if user was switching tasks
    if user_login in switching_task_context:
        switching_task_context.pop(user_login, None)
        # Close the session that was left open during task switch
        await end_session(user_id)

    # Clean up work time tracking
    entering_work_start_users.discard(user_login)
    entering_work_end_users.pop(user_login, None)
    setting_work_time_users.discard(user_login)
    setting_work_time_context.pop(user_login, None)

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

    # If user was switching tasks, close old session first
    if user_login in switching_task_context:
        old_task_id, old_task_name = switching_task_context.pop(user_login)
        await end_session(user_id)

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

    # Get today's session summary
    task_durations, break_seconds = await get_today_sessions(user_id)

    # Format task durations
    lines = []
    if task_durations:
        lines.append("Итоги за сегодня:")
        for task, duration in task_durations:
            lines.append(f"- {task} {format_duration(duration)}")
    else:
        lines.append("За сегодня работы не было.")

    # Format break duration
    if break_seconds > 0:
        lines.append(f"- Перерыв {format_duration(break_seconds)}")

    lines.append("")
    lines.append("Хорошего дня!")

    await send_message(
        user_login,
        "\n".join(lines),
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

    # Create 45-minute break timer reminder
    break_time = datetime.utcnow() + timedelta(minutes=45)
    await create_reminder(user_id, "BREAK_TIMER:break_over", break_time)

    await send_message(
        user_login,
        "Перерыв начат. Нажмите «Вернуться», когда будете готовы продолжить работу.",
        ON_BREAK_KEYBOARD
    )
    logger.info(f"User {user_login} went on break, timer set for {break_time}")


async def handle_return_from_break(user_login: str, user_id: int):
    # Cancel break timer
    await delete_break_reminders(user_id)

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
    _, current_task_id = await get_current_state(user_id)
    current_encrypted = await get_current_encrypted_task(user_id)
    old_task = decrypt_value(current_encrypted) if current_encrypted else None

    # Store old task context for potential cancel
    switching_task_context[user_login] = (current_task_id, old_task)

    entering_task_users.add(user_login)

    if old_task:
        await send_message(
            user_login,
            f"Введите название новой задачи (текущая «{old_task}»):",
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


async def handle_delete_role(user_login: str, user_id: int, args: str):
    """Admin only: /delete_role <user_id> <role>"""
    # Check if user has admin role
    is_admin = await has_role(user_id, "admin")
    if not is_admin:
        await send_message(user_login, "У вас нет прав для этой команды.")
        logger.warning(f"Non-admin user {user_login} tried to use /delete_role")
        return

    # Parse arguments
    parts = args.strip().split()
    if len(parts) != 2:
        await send_message(user_login, "Формат: /delete_role <user_id> <role>\nДоступные роли: admin, manager\nРоль employee не может быть удалена.")
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
        await send_message(user_login, "Неверная роль. Доступные: admin, manager\nРоль employee не может быть удалена.")
        return

    # Validate target user exists
    target_user = await get_user_by_id(target_user_id)
    if not target_user:
        await send_message(user_login, f"Пользователь с ID {target_user_id} не найден.")
        logger.info(f"Admin {user_login} tried to delete role from non-existent user {target_user_id}")
        return

    # Remove role from target user
    success = await remove_role(target_user_id, role)
    if success:
        await send_message(user_login, f"Роль '{role}' удалена у пользователя {target_user_id} ({target_user.name}).")
        logger.info(f"Admin {user_login} removed role '{role}' from user {target_user_id}")
    else:
        await send_message(user_login, f"Не удалось удалить роль. У пользователя {target_user_id} нет роли '{role}'.")
        logger.info(f"Admin {user_login} failed to remove role '{role}' from user {target_user_id} - doesn't have role")


async def handle_set_work_time_command(user_login: str, user_id: int):
    setting_work_time_users.add(user_login)
    await send_message(
        user_login,
        "Укажите время начала работы (формат ЧЧ:ММ, например: 8:00 или 14:30):",
        CANCEL_KEYBOARD
    )
    logger.info(f"User {user_login} started setting work time")


async def handle_vacation(user_login: str, user_id: int):
    is_on_vacation = await toggle_vacation(user_id)
    current_state, _ = await get_current_state(user_id)
    keyboard = fsm.get_keyboard_for_state(current_state)

    if is_on_vacation:
        await send_message(user_login, "🏖️ Отпуск включён. Уведомления о расписании отключены. Хорошего отдыха!", keyboard)
        logger.info(f"User {user_login} enabled vacation")
    else:
        await send_message(user_login, "🏖️ Отпуск отключён. Уведомления о расписании включены! Добро пожаловать обратно!", keyboard)
        logger.info(f"User {user_login} disabled vacation")


async def handle_state(user_login: str, user_id: int, args: str):
    """Admin only: /state <user_id>"""
    # Check if user has admin role
    is_admin = await has_role(user_id, "admin")
    if not is_admin:
        await send_message(user_login, "У вас нет прав для этой команды.")
        logger.warning(f"Non-admin user {user_login} tried to use /state")
        return

    # Parse user_id
    parts = args.strip().split()
    if len(parts) != 1:
        await send_message(user_login, "Формат: /state <user_id>")
        return

    # Validate user_id is a number
    try:
        target_user_id = int(parts[0])
    except ValueError:
        await send_message(user_login, "Неверный формат user_id. Должно быть число.")
        return

    # Validate target user exists
    target_user = await get_user_by_id(target_user_id)
    if not target_user:
        await send_message(user_login, f"Пользователь с ID {target_user_id} не найден.")
        logger.info(f"Admin {user_login} tried to get state for non-existent user {target_user_id}")
        return

    # Get state info
    state_info = await get_user_state_info(target_user_id)

    # Build response
    lines = [
        f"📊 Состояние пользователя {target_user.name} (ID: {target_user_id}):",
        ""
    ]

    # Current status
    state_names = {
        UserState.IDLE.value: "не работает",
        UserState.WORKING.value: "работает",
        UserState.ON_BREAK.value: "на перерыве",
    }
    current_state = state_info["current_state"]
    lines.append(f"Текущее состояние: {state_names.get(current_state, current_state)}")

    # Current task
    if state_info["current_task_id"]:
        lines.append(f"Текущая задача ID: {state_info['current_task_id']}")

    # Last task
    if state_info["last_task_name"]:
        lines.append(f"Последняя задача: {state_info['last_task_name']} (ID: {state_info['last_task_id']})")

    lines.append("")

    # Last session
    if state_info["last_session_id"]:
        lines.append(f"Последняя сессия (ID: {state_info['last_session_id']}):")
        lines.append(f"  - Задача: {state_info['last_session_task']}")

        # Format times
        from datetime import timedelta
        start_time = state_info["last_session_start"]
        if start_time:
            local_start = start_time + timedelta(hours=3)
            lines.append(f"  - Начало: {local_start.strftime('%d.%m.%Y %H:%M')}")

        end_time = state_info["last_session_end"]
        if end_time:
            local_end = end_time + timedelta(hours=3)
            lines.append(f"  - Конец: {local_end.strftime('%d.%m.%Y %H:%M')}")
        else:
            lines.append(f"  - Конец: (не завершена)")

    await send_message(user_login, "\n".join(lines))
    logger.info(f"Admin {user_login} requested state for user {target_user_id}")


async def handle_cancel(user_login: str, user_id: int, from_state: str):
    # Determine what state to return to based on what we were entering
    is_canceling_task = user_login in entering_task_users
    is_canceling_reminder = user_login in entering_reminder_users

    # Check if user was switching tasks
    was_switching_task = user_login in switching_task_context

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

    # Restore old task session if user was switching tasks
    if was_switching_task and return_state == UserState.WORKING.value:
        old_task_id, old_task_name = switching_task_context.pop(user_login, (None, None))
        if old_task_name:
            # Close any dangling sessions first
            await end_session(user_id)
            # Restore the old task session
            await create_session(user_id, old_task_name)
            message += f"\nПродолжаем задачу «{old_task_name}»."
            logger.info(f"User {user_login} cancelled task switch, restored task: {old_task_name}")
    elif user_login in switching_task_context:
        switching_task_context.pop(user_login, None)

    # If working, show current task
    if return_state == UserState.WORKING.value:
        task_name = await get_task_name_by_id(current_task_id) if current_task_id else None
        if task_name and not was_switching_task:
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
                entering_work_start_users.add(user_login)
                await send_message(
                    user_login,
                    f"Рад знакомству, {text.strip()}!\n\n"
                    "Укажите ваше обычное время начала работы (формат ЧЧ:ММ, например: 9:00 или 14:30):",
                    CANCEL_KEYBOARD
                )
                logger.info(f"User {user_login} set name: {text.strip()}")
            else:
                await send_message(user_login, "Пожалуйста, введите ваше имя.", NO_KEYBOARD)
            return

        if user_login in entering_work_start_users:
            if text == "Отмена":
                entering_work_start_users.discard(user_login)
                await send_message(user_login, "Время работы не указано. Используйте /my_id чтобы узнать свой ID, затем свяжитесь с администратором.", NO_KEYBOARD)
                return

            work_time = parse_work_time(text)
            if work_time:
                entering_work_end_users[user_login] = work_time
                entering_work_start_users.discard(user_login)
                await send_message(
                    user_login,
                    f"Время начала работы: {work_time.strftime('%H:%M')}\n\n"
                    "Укажите ваше обычное время окончания работы (формат ЧЧ:ММ):",
                    CANCEL_KEYBOARD
                )
                logger.info(f"User {user_login} set work start: {work_time}")
            else:
                await send_message(user_login, "Неверный формат. Используйте ЧЧ:ММ, например: 9:00 или 14:30", CANCEL_KEYBOARD)
            return

        if user_login in entering_work_end_users:
            if text == "Отмена":
                entering_work_end_users.pop(user_login, None)
                await send_message(user_login, "Настройка отменена. Свяжитесь с администратором для указания времени.", NO_KEYBOARD)
                return

            work_time = parse_work_time(text)
            if work_time:
                start_time = entering_work_end_users.pop(user_login)
                await update_user_work_times(user.id, start_time, work_time)
                await send_message(
                    user_login,
                    f"Время работы: {start_time.strftime('%H:%M')} - {work_time.strftime('%H:%M')}\n\n"
                    "Нажмите «Начать работу», когда будете готовы приступить к задачам.",
                    IDLE_KEYBOARD
                )
                logger.info(f"User {user_login} set work times: {start_time} - {work_time}")
            else:
                await send_message(user_login, "Неверный формат. Используйте ЧЧ:ММ, например: 18:00 или 18:30", CANCEL_KEYBOARD)
            return

        # Handle /setworktime command flow
        if user_login in setting_work_time_users:
            if text == "Отмена":
                setting_work_time_users.discard(user_login)
                current_state, _ = await get_current_state(user.id)
                keyboard = fsm.get_keyboard_for_state(current_state)
                await send_message(user_login, "Изменение времени отменено.", keyboard)
                return

            work_time = parse_work_time(text)
            if work_time:
                setting_work_time_users.discard(user_login)
                setting_work_time_context[user_login] = work_time
                await send_message(
                    user_login,
                    f"Время начала: {work_time.strftime('%H:%M')}\n\n"
                    "Укажите время окончания работы (формат ЧЧ:ММ):",
                    CANCEL_KEYBOARD
                )
                logger.info(f"User {user_login} set work start: {work_time}")
            else:
                await send_message(user_login, "Неверный формат. Используйте ЧЧ:ММ, например: 9:00", CANCEL_KEYBOARD)
            return

        if user_login in setting_work_time_context:
            if text == "Отмена":
                setting_work_time_context.pop(user_login, None)
                current_state, _ = await get_current_state(user.id)
                keyboard = fsm.get_keyboard_for_state(current_state)
                await send_message(user_login, "Изменение времени отменено.", keyboard)
                return

            work_time = parse_work_time(text)
            if work_time:
                start_time = setting_work_time_context.pop(user_login)
                await update_user_work_times(user.id, start_time, work_time)

                current_state, _ = await get_current_state(user.id)
                keyboard = fsm.get_keyboard_for_state(current_state)

                await send_message(
                    user_login,
                    f"Время работы изменено: {start_time.strftime('%H:%M')} - {work_time.strftime('%H:%M')}",
                    keyboard
                )
                logger.info(f"User {user_login} updated work times: {start_time} - {work_time}")
            else:
                await send_message(user_login, "Неверный формат. Используйте ЧЧ:ММ, например: 17:00", CANCEL_KEYBOARD)
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
        elif action == "/rem":
            await handle_new_reminder_start(user_login, user.id)
        elif action == "/list_rem":
            await handle_list_reminders(user_login, user.id)
        elif action == "/del_rem":
            await handle_delete_reminders(user_login, user.id)
        elif action == "/my_id":
            await handle_my_id(user_login, user.id)
        elif action.startswith("/give_role"):
            await handle_give_role(user_login, user.id, action.replace("/give_role", ""))
        elif action.startswith("/delete_role"):
            await handle_delete_role(user_login, user.id, action.replace("/delete_role", ""))
        elif action.startswith("/state"):
            await handle_state(user_login, user.id, action.replace("/state", ""))
        elif action == "/setworktime":
            await handle_set_work_time_command(user_login, user.id)
        elif action == "/vacation":
            await handle_vacation(user_login, user.id)
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