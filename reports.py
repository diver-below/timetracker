from datetime import datetime, timedelta

from db import get_all_users_today_sessions, get_user_today_sessions, get_weekly_sessions, get_user_weekly_sessions
from bot_api import send_message
from config import logger


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


def format_duration_hm(seconds: int) -> str:
    """Format duration in seconds to 'HhMm' format like '45h40min'."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    if hours > 0 and minutes > 0:
        return f"{hours}h{minutes}min"
    elif hours > 0:
        return f"{hours}h0min"
    elif minutes > 0:
        return f"0h{minutes}min"
    else:
        return "0h0min"


def format_time(dt: datetime) -> str:
    # Convert UTC to GMT+3 for display
    local_dt = dt + timedelta(hours=3)
    return local_dt.strftime("%H:%M")


def generate_daily_report(users_data: list[dict]) -> str:
    """Generate combined daily report for all users."""
    now = datetime.utcnow()
    local_date = (now + timedelta(hours=3)).strftime("%d.%m.%Y")
    local_time = (now + timedelta(hours=3)).strftime("%H:%M")

    lines = [f"📊 Ежедневный отчёт за {local_date} ({local_time}):", ""]

    if not users_data:
        lines.append("За сегодня данные отсутствуют.")
        return "\n".join(lines)

    for user_data in sorted(users_data, key=lambda x: x["user_name"].lower()):
        user_name = user_data["user_name"]
        task_durations = user_data["task_durations"]
        break_seconds = user_data["break_seconds"]
        is_working = user_data.get("is_working", False)
        current_task = user_data.get("current_task")

        # Status indicator
        status = "● работает" if is_working else ""
        if status:
            user_line = f"👤 {user_name} {status}"
            if current_task and current_task != "Break":
                user_line += f" (на: {current_task})"
            lines.append(user_line)
        else:
            lines.append(f"👤 {user_name}:")

        if task_durations:
            for task, duration in sorted(task_durations.items()):
                lines.append(f"  - {task} {format_duration(duration)}")
        else:
            lines.append("  - Работ не было")

        if break_seconds > 0:
            lines.append(f"  - Перерыв {format_duration(break_seconds)}")

        lines.append("")

    return "\n".join(lines).strip()


def generate_team_report(users_data: list[dict]) -> str:
    """Generate team daily report excluding managers."""
    now = datetime.utcnow()
    local_date = (now + timedelta(hours=3)).strftime("%d.%m.%Y")
    local_time = (now + timedelta(hours=3)).strftime("%H:%M")

    lines = [f"📊 Ежедневный отчёт команды за {local_date} ({local_time}):", ""]

    # Filter out managers
    team_data = [
        u for u in users_data
        if "manager" not in u.get("roles", [])
    ]

    if not team_data:
        lines.append("За сегодня данные отсутствуют.")
        return "\n".join(lines)

    for user_data in sorted(team_data, key=lambda x: x["user_name"].lower()):
        user_name = user_data["user_name"]
        task_durations = user_data["task_durations"]
        break_seconds = user_data["break_seconds"]
        is_working = user_data.get("is_working", False)
        current_task = user_data.get("current_task")
        roles = user_data.get("roles", [])

        # Status indicator
        status = "● работает" if is_working else ""
        if status:
            user_line = f"👤 {user_name} {status}"
            if current_task and current_task != "Break":
                user_line += f" (на: {current_task})"
            lines.append(user_line)
        else:
            lines.append(f"👤 {user_name}:")

        # Add role badge for admins
        if "admin" in roles:
            lines.append(f"  🏷 admin")

        if task_durations:
            for task, duration in sorted(task_durations.items()):
                lines.append(f"  - {task} {format_duration(duration)}")
        else:
            lines.append("  - Работ не было")

        if break_seconds > 0:
            lines.append(f"  - Перерыв {format_duration(break_seconds)}")

        lines.append("")

    return "\n".join(lines).strip()


def generate_personal_report(task_durations: list[tuple[str, int]], break_seconds: int) -> str:
    """Generate personal daily report like when user ends work."""
    lines = []

    if task_durations:
        lines.append("Итоги за сегодня:")
        for task, duration in task_durations:
            lines.append(f"- {task} {format_duration(duration)}")
    else:
        lines.append("За сегодня работы не было.")

    if break_seconds > 0:
        lines.append(f"- Перерыв {format_duration(break_seconds)}")

    lines.append("")
    lines.append("Хорошего дня!")

    return "\n".join(lines)


async def send_daily_report_to_manager(user_login: str, user_id: int):
    """Send daily report to a manager user (team + personal)."""
    users_data = await get_all_users_today_sessions()

    # Send team report
    team_report = generate_team_report(users_data)
    await send_message(user_login, team_report)
    logger.info(f"Team daily report sent to manager {user_login}")

    # Send personal report
    task_durations, break_seconds = await get_user_today_sessions(user_id)
    personal_report = generate_personal_report(task_durations, break_seconds)
    await send_message(user_login, personal_report)
    logger.info(f"Personal daily report sent to manager {user_login}")


async def get_managers_logins() -> list[tuple[int, str]]:
    """Get list of all manager users: [(user_id, yandex_user_login), ...]"""
    import db

    async with db.async_session_factory() as session:
        result = await session.execute(
            db.select(db.User.id, db.User.yandex_user_login).where(
                db.User.roles.like("%manager%")
            )
        )
        return [(row.id, row.yandex_user_login) for row in result.all()]


def generate_weekly_report(users_data: list[dict]) -> str:
    """Generate combined weekly report for all users (employees + admins only)."""
    now = datetime.utcnow()
    gmt3_now = now + timedelta(hours=3)
    week_start = gmt3_now - timedelta(days=gmt3_now.weekday())
    week_end = week_start + timedelta(days=6)
    week_range = f"{week_start.strftime('%d.%m')} - {week_end.strftime('%d.%m')}"

    lines = [f"📊 Еженедельный отчёт команды ({week_range}):", ""]

    # Filter out managers
    team_data = [
        u for u in users_data
        if "manager" not in u.get("roles", [])
    ]

    if not team_data:
        lines.append("За эту неделю данные отсутствуют.")
        return "\n".join(lines)

    total_work = 0
    total_break = 0
    user_count = 0

    for user_data in sorted(team_data, key=lambda x: x["user_name"].lower()):
        user_name = user_data["user_name"]
        work_seconds = user_data["work_seconds"]
        break_seconds = user_data["break_seconds"]
        roles = user_data.get("roles", [])

        # Role badge
        badge = " (admin)" if "admin" in roles else ""
        lines.append(f"- {user_name}{badge} - {format_duration_hm(work_seconds)}")

        total_work += work_seconds
        total_break += break_seconds
        user_count += 1

    lines.append("")
    lines.append(f"Всего: {user_count} человек, работа: {format_duration_hm(total_work)}, перерывы: {format_duration_hm(total_break)}")

    return "\n".join(lines)


def generate_personal_weekly_report(work_seconds: int, break_seconds: int) -> str:
    """Generate personal weekly report."""
    lines = []
    lines.append("Итоги за неделю:")

    if work_seconds > 0 or break_seconds > 0:
        lines.append(f"- Работа: {format_duration_hm(work_seconds)}")
        lines.append(f"- Перерывы: {format_duration_hm(break_seconds)}")
    else:
        lines.append("За эту неделю работы не было.")

    lines.append("")
    lines.append("Хорошей недели!")

    return "\n".join(lines)


async def send_weekly_report_to_manager(user_login: str, user_id: int):
    """Send weekly report to a manager user (team + personal)."""
    users_data = await get_weekly_sessions()

    # Send team report
    team_report = generate_weekly_report(users_data)
    await send_message(user_login, team_report)
    logger.info(f"Team weekly report sent to manager {user_login}")

    # Send personal report
    work_seconds, break_seconds = await get_user_weekly_sessions(user_id)
    personal_report = generate_personal_weekly_report(work_seconds, break_seconds)
    await send_message(user_login, personal_report)
    logger.info(f"Personal weekly report sent to manager {user_login}")