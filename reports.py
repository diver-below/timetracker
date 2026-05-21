from datetime import datetime, timedelta

from db import get_all_users_today_sessions
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


def format_time(dt: datetime) -> str:
    # Convert UTC to GMT+3 for display
    local_dt = dt + timedelta(hours=3)
    return local_dt.strftime("%H:%M")


def generate_daily_report(users_data: list[dict]) -> str:
    """Generate combined daily report for all users."""
    now = datetime.utcnow()
    local_date = (now + timedelta(hours=3)).strftime("%d.%m.%Y")

    lines = [f"📊 Ежедневный отчёт за {local_date}:", ""]

    if not users_data:
        lines.append("За сегодня данные отсутствуют.")
        return "\n".join(lines)

    for user_data in sorted(users_data, key=lambda x: x["user_name"].lower()):
        user_name = user_data["user_name"]
        task_durations = user_data["task_durations"]
        break_seconds = user_data["break_seconds"]

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


async def send_daily_report_to_manager(user_login: str, user_id: int):
    """Send daily report to a manager user."""
    users_data = await get_all_users_today_sessions()
    report = generate_daily_report(users_data)

    await send_message(user_login, report)
    logger.info(f"Daily report sent to manager {user_login}")


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