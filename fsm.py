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
            UserState.ENTERING_TASK.value: CANCEL_KEYBOARD,
            UserState.ENTERING_REMINDER.value: CANCEL_KEYBOARD,
        }
        return state_map.get(state, NO_KEYBOARD)

    def is_valid_transition(self, current_state: str, action: str) -> bool:
        transitions = {
            UserState.IDLE.value: ["/start", "Начать работу", "/new_rem", "/list_rem", "/del_rem", "/my_id", "/give_role", "/state", "/setworktime"],
            UserState.WORKING.value: ["/start", "Закончить", "Перерыв", "Сменить задачу", "/new_rem", "/list_rem", "/del_rem", "/my_id", "/give_role", "/state", "/setworktime"],
            UserState.ON_BREAK.value: ["/start", "Вернуться", "/new_rem", "/list_rem", "/del_rem", "/my_id", "/give_role", "/state", "/setworktime"],
            UserState.ENTERING_TASK.value: ["/start", "Отмена", "/my_id"],
            UserState.ENTERING_REMINDER.value: ["/start", "Отмена", "/my_id"],
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
            UserState.IDLE.value: {
                "Начать работу": UserState.ENTERING_TASK.value,
                "/new_rem": UserState.ENTERING_REMINDER.value,
            },
            UserState.WORKING.value: {
                "Закончить": UserState.IDLE.value,
                "Перерыв": UserState.ON_BREAK.value,
                "Сменить задачу": UserState.ENTERING_TASK.value,
                "/new_rem": UserState.ENTERING_REMINDER.value,
            },
            UserState.ON_BREAK.value: {
                "Вернуться": UserState.WORKING.value,
                "/new_rem": UserState.ENTERING_REMINDER.value,
            },
            UserState.ENTERING_TASK.value: {
                "Отмена": UserState.IDLE.value,
            },
            UserState.ENTERING_REMINDER.value: {
                "Отмена": UserState.IDLE.value,
            },
        }

        if current_state == UserState.ENTERING_TASK.value and action not in ("/start", "Отмена"):
            return UserState.WORKING.value

        if current_state == UserState.ENTERING_REMINDER.value and action not in ("/start", "Отмена"):
            return None

        return state_actions.get(current_state, {}).get(action)