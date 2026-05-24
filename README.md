# Яндекс Мессенджер Бот для Тайм-Трекинга

Бот для учёта рабочего времени сотрудников в Яндекс Мессенджере.

## Функциональность

- Фиксация начала и окончания работы над задачами
- Учёт перерывов
- Смена задач в течение рабочего дня
- Система напоминаний
- Шифрование данных в базе
- Многопользовательская работа

## Системные требования

- Linux с systemd
- Python 3.11+
- PostgreSQL 15+ с SSL
- Публичный HTTPS-адрес для вебхука

## Установка

### 1. Создание пользователя

```bash
sudo useradd -r -s /bin/false timetracker_bot
```

### 2. Клонирование репозитория

```bash
sudo git clone <repo-url> /opt/timetracker-bot
sudo chown -R timetracker_bot:timetracker_bot /opt/timetracker-bot
cd /opt/timetracker-bot
sudo -u timetracker_bot python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Настройка PostgreSQL

```sql
-- Создать базу данных и пользователя
CREATE DATABASE timetracker;
CREATE USER timetracker_bot WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE timetracker TO timetracker_bot;

-- Включить SSL (ssl=on в postgresql.conf)
-- В pg_hba.conf:
hostssl timetracker timetracker_bot 127.0.0.1/32 scram-sha-256
```

### 4. Генерация ключа шифрования

```bash
python scripts/generate_key.py
```

### 5. Настройка переменных окружения

Создать файл `/etc/timetracker-bot/env`:

```ini
YANDEX_OAUTH_TOKEN=your_oauth_token_here
DATABASE_URL=postgresql+asyncpg://timetracker_bot:password@localhost:5432/timetracker?sslmode=require
ENCRYPTION_KEY=<from generate_key.py>
WEBHOOK_URL=https://your-domain.com/webhook
LISTEN_PORT=8443
LOG_LEVEL=INFO
```

Установить права:

```bash
sudo chown root:timetracker_bot /etc/timetracker-bot/env
sudo chmod 640 /etc/timetracker-bot/env
```

### 6. Инициализация базы данных

```bash
python scripts/init_db.py
```

### 7. Настройка SSL для вебхука

Используйте nginx с Let's Encrypt или встроенный TLS:

```
# nginx.conf
server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location /webhook {
        proxy_pass http://localhost:8443/webhook;
    }
}
```

### 8. Установка systemd сервиса

```bash
sudo cp systemd/timetracker-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable timetracker-bot
sudo systemctl start timetracker-bot
```

### 9. Проверка работы

```bash
sudo systemctl status timetracker-bot
journalctl -u timetracker-bot -f
```

## Использование

### Команды бота

- `/start` — сброс состояния, запуск/перезапуск
- `/rem` — создать напоминание (формат: `<время> <текст>`)
- `/list_rem` — список активных напоминаний

### Клавиатура

**Состояние Idle (покой):**
- «Начать работу»

**Состояние Working (работа):**
- «Закончить» — завершить текущую задачу
- «Перерыв» — начать перерыв
- «Сменить задачу» — завершить текущую и начать новую

**Состояние OnBreak (перерыв):**
- «Вернуться» — вернуться к работе

### Пример создания напоминания

```
через 15мин проверить почту
в 14:30 встреча
завтра 10:00 написать отчёт
```

## Безопасность

- Все соединения с БД зашифрованы SSL
- Названия задач шифруются (Fernet)
- Секреты хранятся в защищённом файле `/etc/timetracker-bot/env`
- Бот запускается от непривилегированного пользователя
- systemd использует директивы защиты: `NoNewPrivileges`, `ProtectSystem`, `ProtectHome`

## Логи

```bash
journalctl -u timetracker-bot -f
```

## Тестирование

1. Отправьте `/start` боту
2. Нажмите «Начать работу» и введите задачу
3. Проверьте состояние в БД
4. Нажмите «Перерыв», затем «Вернуться»
5. Нажмите «Закончить» для завершения
6. Создайте напоминание `/rem`
7. Проверьте список `/list_rem`