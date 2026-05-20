import aiohttp
from typing import List, Optional
from config import YANDEX_OAUTH_TOKEN, BOT_API_URL, logger


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

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(BOT_API_URL, json=payload, headers=headers) as response:
                if response.status == 200:
                    logger.info(f"Message sent to chat {chat_id}")
                    return True
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to send message: {response.status} - {error_text}")
                    return False
    except aiohttp.ClientError as e:
        logger.error(f"Network error sending message: {e}")
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