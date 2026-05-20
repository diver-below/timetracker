import aiohttp
import ssl
from typing import List, Optional
from config import YANDEX_OAUTH_TOKEN, BOT_API_URL, logger

# SSL context for outgoing connections (verify Yandex's cert)
ssl_context = ssl.create_default_context()


def format_keyboard(buttons: Optional[List[List[str]]]) -> Optional[dict]:
    if not buttons:
        return None

    return {
        "buttons": [
            [{"action": {"type": "text", "label": btn}, "color": "default"} for btn in row]
            for row in buttons
        ],
        "one_time": False
    }


async def send_message(chat_id: str, text: str, keyboard: Optional[List[List[str]]] = None) -> bool:
    keyboard_obj = format_keyboard(keyboard)

    payload = {
        "chat_id": chat_id,
        "text": text,
    }

    if keyboard_obj:
        payload["keyboard"] = keyboard_obj

    headers = {
        "Authorization": f"OAuth {YANDEX_OAUTH_TOKEN}",
        "Content-Type": "application/json"
    }

    connector = aiohttp.TCPConnector(ssl=ssl_context)

    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            logger.info(f"Sending message to Yandex API: {BOT_API_URL}")
            async with session.post(BOT_API_URL, json=payload, headers=headers, timeout=10) as response:
                logger.info(f"Yandex API response status: {response.status}")
                if response.status == 200:
                    logger.info(f"Message sent to chat {chat_id}")
                    return True
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to send message: {response.status} - {error_text}")
                    return False
    except aiohttp.ClientConnectorError as e:
        logger.error(f"Connection error to Yandex API: {e}")
        return False
    except aiohttp.ClientError as e:
        logger.error(f"Network error sending message: {type(e).__name__}: {e}")
        return False


def parse_webhook_payload(data: dict) -> tuple[str, str, str]:
    updates = data.get("updates", [])
    if not updates:
        return "", "", ""

    update = updates[0]
    from_user = update.get("from", {})
    chat = update.get("chat", {})

    user_login = from_user.get("login", from_user.get("id", ""))
    chat_id = chat.get("id", "")
    text = update.get("text", "")

    if "payload" in update:
        text = update["payload"].get("text", text)

    return user_login, chat_id, text