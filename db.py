from datetime import datetime, time
from typing import Optional
from enum import Enum
import base64

from cryptography.fernet import Fernet
from sqlalchemy import String, Integer, BigInteger, Time, ForeignKey, Text, Boolean, DateTime, func, select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import NullPool

from config import DATABASE_URL, ENCRYPTION_KEY, logger

cipher_suite = Fernet(ENCRYPTION_KEY.encode())


class Base(DeclarativeBase):
    pass


class UserState(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    ON_BREAK = "on_break"
    ENTERING_TASK = "entering_task"
    ENTERING_REMINDER = "entering_reminder"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    yandex_user_login: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    scheduled_work_start: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    scheduled_work_end: Mapped[Optional[time]] = mapped_column(Time, nullable=True)

    current_status: Mapped["CurrentStatus"] = relationship(
        "CurrentStatus", back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    user_tasks: Mapped[list["UserTask"]] = relationship(
        "UserTask", back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["Session"]] = relationship(
        "Session", back_populates="user", cascade="all, delete-orphan"
    )
    reminders: Mapped[list["Reminder"]] = relationship(
        "Reminder", back_populates="user", cascade="all, delete-orphan"
    )


class CurrentStatus(Base):
    __tablename__ = "current_status"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    current_state: Mapped[str] = mapped_column(String(50), nullable=False, default=UserState.IDLE.value)
    current_task_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("user_tasks.id"), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="current_status")


class UserTask(Base):
    __tablename__ = "user_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    task_name_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="user_tasks")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    task_name_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="sessions")


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    user: Mapped["User"] = relationship("User", back_populates="reminders")


# Parse DATABASE_URL and add SSL for asyncpg
from urllib.parse import urlparse, parse_qs

db_url = DATABASE_URL
parsed = urlparse(db_url)
query = parse_qs(parsed.query)

# Remove sslmode from query (asyncpg doesn't support it in URL)
query.pop('sslmode', None)

# Rebuild query string
new_query = '&'.join(f"{k}={v[0]}" for k, v in query.items())
db_url_parsed = parsed._replace(query=new_query)
db_url_ssl = db_url_parsed.geturl()

# Create SSL context that doesn't verify CA cert (for self-signed certs)
import ssl
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

engine = create_async_engine(
    db_url_ssl,
    echo=False,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=10,
    connect_args={"ssl": ssl_context},
)


async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


def encrypt_value(plain: str) -> str:
    if plain == "Break":
        return plain
    encrypted_bytes = cipher_suite.encrypt(plain.encode())
    return base64.b64encode(encrypted_bytes).decode()


def decrypt_value(cipher: str) -> str:
    if cipher == "Break":
        return cipher
    encrypted_bytes = base64.b64decode(cipher.encode())
    return cipher_suite.decrypt(encrypted_bytes).decode()


def generate_encryption_key() -> str:
    return Fernet.generate_key().decode()


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized successfully")


async def get_session() -> AsyncSession:
    async with async_session_factory() as session:
        yield session


async def get_or_create_user(user_login: str, name: str = None) -> tuple[User, bool]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.yandex_user_login == user_login)
        )
        user = result.scalar_one_or_none()

        if user is None:
            user = User(yandex_user_login=user_login, name=name or "")
            session.add(user)
            await session.flush()

            current_status = CurrentStatus(
                user_id=user.id,
                current_state=UserState.IDLE.value
            )
            session.add(current_status)
            await session.commit()
            await session.refresh(user)
            return user, True

        return user, False


async def get_current_state(user_id: int) -> tuple[str, Optional[int]]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(CurrentStatus).where(CurrentStatus.user_id == user_id)
        )
        status = result.scalar_one_or_none()
        if status:
            return status.current_state, status.current_task_id
        return UserState.IDLE.value, None


async def update_state(user_id: int, state: str, task_id: Optional[int] = None):
    async with async_session_factory() as session:
        result = await session.execute(
            select(CurrentStatus).where(CurrentStatus.user_id == user_id)
        )
        status = result.scalar_one_or_none()

        if status:
            status.current_state = state
            status.current_task_id = task_id
        else:
            status = CurrentStatus(user_id=user_id, current_state=state, current_task_id=task_id)
            session.add(status)

        await session.commit()


async def create_session(user_id: int, task_name: str) -> int:
    async with async_session_factory() as session:
        encrypted_task = encrypt_value(task_name)
        new_session = Session(
            user_id=user_id,
            task_name_encrypted=encrypted_task,
            start_time=datetime.utcnow()
        )
        session.add(new_session)
        await session.commit()
        await session.refresh(new_session)
        return new_session.id


async def end_session(user_id: int) -> Optional[Session]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Session)
            .where(Session.user_id == user_id)
            .where(Session.end_time.is_(None))
            .order_by(Session.start_time.desc())
        )
        session_obj = result.scalar_one_or_none()

        if session_obj:
            session_obj.end_time = datetime.utcnow()
            await session.commit()
            await session.refresh(session_obj)
            return session_obj
        return None


async def save_task(user_id: int, task_name: str) -> int:
    async with async_session_factory() as session:
        encrypted_task = encrypt_value(task_name)
        new_task = UserTask(user_id=user_id, task_name_encrypted=encrypted_task)
        session.add(new_task)
        await session.commit()
        await session.refresh(new_task)
        return new_task.id


async def get_user_tasks(user_id: int) -> list[str]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(UserTask)
            .where(UserTask.user_id == user_id)
            .order_by(UserTask.id.desc())
        )
        tasks = result.scalars().all()
        return [decrypt_value(t.task_name_encrypted) for t in tasks]


async def create_reminder(user_id: int, text: str, scheduled_time: datetime) -> int:
    async with async_session_factory() as session:
        reminder = Reminder(
            user_id=user_id,
            text=text,
            scheduled_time=scheduled_time,
            is_done=False
        )
        session.add(reminder)
        await session.commit()
        await session.refresh(reminder)
        return reminder.id


async def get_active_reminders(user_id: int) -> list[Reminder]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Reminder)
            .where(Reminder.user_id == user_id)
            .where(Reminder.is_done.is_(False))
            .order_by(Reminder.scheduled_time.asc())
        )
        return result.scalars().all()


async def mark_reminder_done(reminder_id: int):
    async with async_session_factory() as session:
        result = await session.execute(
            select(Reminder).where(Reminder.id == reminder_id)
        )
        reminder = result.scalar_one_or_none()
        if reminder:
            reminder.is_done = True
            await session.commit()


async def get_due_reminders() -> list[tuple[int, str, str]]:
    async with async_session_factory() as session:
        from sqlalchemy import and_
        result = await session.execute(
            select(Reminder, User)
            .join(User, Reminder.user_id == User.id)
            .where(and_(
                Reminder.scheduled_time <= datetime.utcnow(),
                Reminder.is_done.is_(False)
            ))
        )
        reminders = result.all()
        return [(r.Reminder.id, r.Reminder.text, str(r.User.yandex_user_login)) for r in reminders]


async def get_user_by_id(user_id: int) -> Optional[User]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.id == user_id)
        )
        return result.scalar_one_or_none()


async def get_current_encrypted_task(user_id: int) -> Optional[str]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(CurrentStatus).where(CurrentStatus.user_id == user_id)
        )
        status = result.scalar_one_or_none()
        if status and status.current_task_id:
            task_result = await session.execute(
                select(UserTask).where(UserTask.id == status.current_task_id)
            )
            task = task_result.scalar_one_or_none()
            if task:
                return task.task_name_encrypted
        return None


async def update_user_name(user_id: int, name: str):
    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.id == user_id)
        )
        user = result.scalar_one_or_none()
        if user:
            user.name = name
            await session.commit()