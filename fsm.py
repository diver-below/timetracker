from enum import Enum
from typing import List, Optional
from db import UserState


IDLE_KEYBOARD = [["Начать работу"]]
WORKING_KEYBOARD = [["Закончить", "Перерыв"], ["Сменить задачу"]]
ON_BREAK_KEYBOARD = [["Вернуться"]]
CANCEL_KEYBOARD = [["Отмена"]]
NO_KEYBOARD = None


class FSM:
    def __init__(self):
        self.current_state = UserState.IDLE

    def get_keyboard_for_state(self, state: str) -> Optional[List[List[str]]]:
        state_map = {
            UserState.IDLE.value: IDLE_KEYBOARD,
            UserState.WORKING.value: WORKING_KEYBOARD,
            UserState.ON_BREAK.value: ON_BREAK_KEYBOARD,
        }
        return state_map.get(state, NO_KEYBOARD)

    def is_valid_transition(self, current_state: str, action: str) -> bool:
        transitions = {
            UserState.IDLE.value: ["/start", "Начать работу", "/rem", "/list_rem", "/del_rem", "/my_id", "/give_role", "/delete_role", "/state", "/setworktime", "/vacation"],
            UserState.WORKING.value: ["/start", "Закончить", "Перерыв", "Сменить задачу", "/rem", "/list_rem", "/del_rem", "/my_id", "/give_role", "/delete_role", "/state", "/setworktime", "/vacation"],
            UserState.ON_BREAK.value: ["/start", "Вернуться", "/rem", "/list_rem", "/del_rem", "/my_id", "/give_role", "/delete_role", "/state", "/setworktime", "/vacation"],
        }
        valid_actions = transitions.get(current_state, [])
        for valid in valid_actions:
            if action == valid or action.startswith(valid + " "):
                return True
        return False

    def get_next_state(self, current_state: str, action: str, text: Optional[str] = None) -> Optional[str]:
        if action == "/start":
            return UserState.IDLE.value

        state_actions = {
            UserState.IDLE.value: {},
            UserState.WORKING.value: {
                "Закончить": UserState.IDLE.value,
                "Перерыв": UserState.ON_BREAK.value,
            },
            UserState.ON_BREAK.value: {
                "Вернуться": UserState.WORKING.value,
            },
        }

        return state_actions.get(current_state, {}).get(action)