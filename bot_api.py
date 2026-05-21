import aiohttp
import ssl
from typing import List, Optional
from config import YANDEX_OAUTH_TOKEN, BOT_API_URL, logger

# SSL context for outgoing connections
ssl_context = ssl.create_default_context()


def format_keyboard(buttons: Optional[List[List[str]]]) -> Optional[dict]:
    if not buttons:
        return None

    # Yandex uses suggest_buttons format
    buttons_list = []
    for row in buttons:
        for btn in row:
            buttons_list.append({
                "text": btn,
                "style": "default"
            })

    return buttons_list


async def send_message(login: str, text: str, keyboard: Optional[List[List[str]]] = None) -> bool:
    buttons_obj = format_keyboard(keyboard)

    payload = {
        "login": login,
        "text": text,
    }

    if buttons_obj:
        payload["suggest_buttons"] = buttons_obj

    headers = {
        "Authorization": f"OAuth {YANDEX_OAUTH_TOKEN}",
        "Content-Type": "application/json"
    }

    connector = aiohttp.TCPConnector(ssl=ssl_context)

    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            logger.info(f"Sending message to Yandex API for user: {login}, payload: {payload}")
            async with session.post(BOT_API_URL, json=payload, headers=headers, timeout=10) as response:
                logger.info(f"Yandex API response status: {response.status}")
                if response.status == 200:
                    logger.info(f"Message sent to user {login}")
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