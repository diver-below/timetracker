import os
import logging
from dotenv import load_dotenv

load_dotenv()

YANDEX_OAUTH_TOKEN = os.getenv("YANDEX_OAUTH_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8443"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

BOT_API_URL = "https://botapi.messenger.yandex.net/api/v1/sendMessage"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)

logger = logging.getLogger(__name__)


def validate_config():
    missing = []
    if not YANDEX_OAUTH_TOKEN:
        missing.append("YANDEX_OAUTH_TOKEN")
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if not ENCRYPTION_KEY:
        missing.append("ENCRYPTION_KEY")
    if not WEBHOOK_URL:
        missing.append("WEBHOOK_URL")

    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")