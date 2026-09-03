"""The Alt+Tab hook state machine.

Driven through `_handle` with real KBDLLHOOKSTRUCT values, so the tests cover
the same code path Windows calls. Only the modifier probes (which read live
keyboard state) and the wheel-state callbacks are stubbed.
"""

from __future__ import annotations

import pytest

from fun_tab import hook as hook_mod
from fun_tab import win32_types as w
from fun_tab.hook import AltTabHook


class Keyboard:
    """A hook wired to a fake keyboard and a fake wheel."""

    def __init__(self, *, opened: bool = False, sticky: bool = False, query: str = ""):
        self.hook = AltTabHook()
        self.opened = opened
        self.sticky = sticky
        self.query = query
        self.shift = False
        self.ctrl = False
        self.alt = False

        self.hook.is_open = lambda: self.opened
        self.hook.is_sticky = lambda: self.sticky
        self.hook.has_query = lambda: bool(self.query)
        self.hook._alt_held = lambda: self.alt  # type: ignore[method-assign]
        self.hook._shift_held = lambda: self.shift  # type: ignore[method-assign]
        self.hook._ctrl_held = lambda: self.ctrl  # type: ignore[method-assign]

    def press(self, vk: int, *, up: bool = False, alt_flag: bool = False) -> bool:
        info = w.KBDLLHOOKSTRUCT()
        info.vkCode = vk
        info.flags = w.LLKHF_ALTDOWN if alt_flag else 0
        message = w.WM_KEYUP if up else w.WM_KEYDOWN
        return self.hook._handle(message, info)

    def release(self, vk: int) -> bool:
        return self.press(vk, up=True)

    def actions(self) -> list[tuple[str, object]]:
        return [(a.kind, a.value) for a in self.hook.drain()]

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.actions()]


# -- opening ----------------------------------------------------------------


def test_alt_tab_opens_the_wheel_and_is_swallowed():
    kb = Keyboard()
    kb.alt = True
    assert kb.press(w.VK_TAB) is True, "Windows must not also see the Tab"
    assert kb.actions() == [(hook_mod.OPEN, {"sticky": False, "reverse": False})]


def test_tab_without_alt_is_left_alone():
    kb = Keyboard()
    assert kb.press(w.VK_TAB) is False
    assert kb.actions() == []


# -- backdrop pre-roll ------------------------------------------------------


def test_alt_down_asks_for_the_backdrop_early():
    """The desktop blur takes ~32ms to read back, so it starts on Alt, not Tab."""
    kb = Keyboard()
    assert kb.press(w.VK_LMENU) is False, "Alt itself must still reach apps"
    assert kb.kinds() == [hook_mod.PREPARE]


def test_held_alt_asks_only_once():
    kb = Keyboard()
    kb.press(w.VK_LMENU)
    assert kb.kinds() == [hook_mod.PREPARE]  # drains the queue
    for _ in range(5):  # auto-repeat while the key stays down
        kb.press(w.VK_LMENU)
    assert kb.kinds() == []


def test_alt_down_is_quiet_while_the_wheel_is_open():
    kb = Keyboard(opened=True)
    kb.alt = True
    kb.press(w.VK_LMENU)
    assert hook_mod.PREPARE not in kb.kinds()


def test_shift_alt_tab_opens_backwards():
    kb = Keyboard()
    kb.alt = True
    kb.shift = True
    kb.press(w.VK_TAB)
    assert kb.actions() == [(hook_mod.OPEN, {"sticky": False, "reverse": True})]


def test_ctrl_alt_tab_opens_sticky():
    kb = Keyboard()
    kb.alt = True
    kb.ctrl = True
    kb.press(w.VK_TAB)
    assert kb.actions() == [(hook_mod.OPEN, {"sticky": True, "reverse": False})]


def test_alt_backtick_opens_the_current_app_only():
    kb = Keyboard()
    kb.alt = True
    assert kb.press(w.VK_OEM_3) is True
    assert kb.actions() == [(hook_mod.OPEN, {"same_app": True, "reverse": False})]


def test_alt_down_is_learned_from_the_event_flag():
    """A synthetic Alt+Tab may arrive with no separate Alt keystroke."""
    kb = Keyboard()
    kb.hook._alt_held = lambda: kb.hook._alt_down  # type: ignore[method-assign]
    assert kb.press(w.VK_TAB, alt_flag=True) is True
    assert kb.kinds() == [hook_mod.OPEN]


# -- committing and cancelling ---------------------------------------------


def test_releasing_alt_commits():
    kb = Keyboard(opened=True)
    assert kb.release(w.VK_LMENU) is False, "Alt-up must pass through"
    assert kb.kinds() == [hook_mod.COMMIT]


def test_releasing_alt_in_sticky_mode_keeps_the_wheel_open():
    kb = Keyboard(opened=True, sticky=True)
    kb.release(w.VK_LMENU)
    assert kb.kinds() == []


def test_releasing_alt_while_closed_does_nothing():
    kb = Keyboard(opened=False)
    kb.release(w.VK_LMENU)
    assert kb.kinds() == []


@pytest.mark.parametrize("vk", [w.VK_RETURN, w.VK_SPACE])
def test_enter_and_space_commit(vk):
    kb = Keyboard(opened=True)
    assert kb.press(vk) is True
    assert kb.kinds() == [hook_mod.COMMIT]


def test_escape_cancels():
    kb = Keyboard(opened=True)
    assert kb.press(w.VK_ESCAPE) is True
    assert kb.kinds() == [hook_mod.CANCEL]


# -- navigation -------------------------------------------------------------


def test_tab_cycles_forward_and_shift_tab_back():
    kb = Keyboard(opened=True)
    kb.press(w.VK_TAB)
    kb.shift = True
    kb.press(w.VK_TAB)
    assert kb.actions() == [(hook_mod.CYCLE, 1), (hook_mod.CYCLE, -1)]


@pytest.mark.parametrize(
    "vk, step",
    [
        (w.VK_LEFT, -1),
        (w.VK_UP, -1),
        (w.VK_RIGHT, 1),
        (w.VK_DOWN, 1),
    ],
)
def test_arrows_cycle(vk, step):
    kb = Keyboard(opened=True)
    assert kb.press(vk) is True
    assert kb.actions() == [(hook_mod.CYCLE, step)]


def test_home_and_end_jump_to_the_ends():
    kb = Keyboard(opened=True)
    kb.press(w.VK_HOME)
    kb.press(w.VK_END)
    assert kb.kinds() == [hook_mod.FIRST, hook_mod.LAST]


def test_backtick_cycles_within_the_selected_app():
    kb = Keyboard(opened=True)
    kb.press(w.VK_OEM_3)
    kb.shift = True
    kb.press(w.VK_OEM_3)
    assert kb.actions() == [(hook_mod.SAME_APP, 1), (hook_mod.SAME_APP, -1)]


# -- digits and search ------------------------------------------------------


def test_digits_jump_straight_to_a_slice():
    kb = Keyboard(opened=True)
    kb.press(0x33)  # '3'
    assert kb.actions() == [(hook_mod.JUMP, 3)]


def test_numpad_digits_jump_too():
    kb = Keyboard(opened=True)
    kb.press(w.VK_NUMPAD0 + 7)
    assert kb.actions() == [(hook_mod.JUMP, 7)]


def test_digits_type_into_an_existing_query():
    kb = Keyboard(opened=True, query="co")
    kb.press(0x33)
    assert kb.actions() == [(hook_mod.TYPE, "3")], "digits extend a query, not jump"


def test_letters_type_into_the_query():
    kb = Keyboard(opened=True)
    kb.press(0x43)  # 'c'
    assert kb.actions() == [(hook_mod.TYPE, "c")]


def test_backspace_edits_the_query():
    kb = Keyboard(opened=True, query="co")
    assert kb.press(w.VK_BACK) is True
    assert kb.kinds() == [hook_mod.BACKSPACE]


def test_escape_clears_the_query_before_cancelling():
    kb = Keyboard(opened=True, query="code")
    kb.press(w.VK_ESCAPE)
    assert kb.kinds() == [hook_mod.CLEAR]

    kb.query = ""
    kb.press(w.VK_ESCAPE)
    assert kb.kinds() == [hook_mod.CANCEL]


def test_search_can_be_disabled():
    kb = Keyboard(opened=True)
    kb.hook.search_enabled = False
    assert kb.press(0x43) is True, "keys are still swallowed, just not typed"
    assert kb.kinds() == []


def test_digit_jump_can_be_disabled():
    kb = Keyboard(opened=True)
    kb.hook.digit_jump = False
    kb.press(0x33)
    assert kb.actions() == [(hook_mod.TYPE, "3")]


# -- window actions ---------------------------------------------------------


def test_delete_closes_the_selected_window():
    kb = Keyboard(opened=True)
    assert kb.press(w.VK_DELETE) is True
    assert kb.kinds() == [hook_mod.CLOSE]


@pytest.mark.parametrize("vk", [0x57, 0x51])  # Ctrl+W, Ctrl+Q
def test_ctrl_w_and_ctrl_q_close(vk):
    kb = Keyboard(opened=True)
    kb.ctrl = True
    kb.press(vk)
    assert kb.kinds() == [hook_mod.CLOSE]


def test_ctrl_m_minimizes():
    kb = Keyboard(opened=True)
    kb.ctrl = True
    kb.press(0x4D)
    assert kb.kinds() == [hook_mod.MINIMIZE]


def test_close_keys_can_be_disabled():
    kb = Keyboard(opened=True)
    kb.hook.close_key_enabled = False
    kb.press(w.VK_DELETE)
    assert kb.kinds() == []


def test_ctrl_letters_are_not_typed_into_the_query():
    kb = Keyboard(opened=True)
    kb.ctrl = True
    kb.press(0x5A)  # Ctrl+Z
    assert kb.kinds() == []


# -- general contract -------------------------------------------------------


def test_open_wheel_swallows_every_key_down():
    """Nothing typed at the wheel may leak through to the app behind it."""
    kb = Keyboard(opened=True)
    for vk in (0x41, w.VK_TAB, 0x70, 0xBA, w.VK_PRIOR):  # 'a', Tab, F1, ';', PgUp
        assert kb.press(vk) is True, f"vk {vk:#x} leaked"


def test_key_releases_are_never_swallowed():
    kb = Keyboard(opened=True)
    for vk in (0x41, w.VK_TAB, w.VK_ESCAPE, w.VK_RETURN):
        assert kb.release(vk) is False


def test_closed_wheel_ignores_unmodified_keys():
    kb = Keyboard(opened=False)
    for vk in (0x41, w.VK_ESCAPE, w.VK_RETURN, w.VK_DELETE):
        assert kb.press(vk) is False
    assert kb.kinds() == []


def test_queue_survives_a_burst_and_drains_in_order():
    kb = Keyboard(opened=True)
    for _ in range(5):
        kb.press(w.VK_TAB)
    kb.press(w.VK_RETURN)
    assert kb.kinds() == [hook_mod.CYCLE] * 5 + [hook_mod.COMMIT]
    assert kb.kinds() == [], "draining empties the queue"


def test_actions_are_queued_without_a_ui_thread():
    """Emitting before set_ui_thread must not raise; the wake is best effort."""
    kb = Keyboard(opened=True)
    assert kb.hook._ui_thread_id == 0
    kb.press(w.VK_RETURN)
    assert kb.kinds() == [hook_mod.COMMIT]


def test_queue_is_bounded():
    """A stuck UI thread must not let the queue grow without limit."""
    kb = Keyboard(opened=True)
    for _ in range(500):
        kb.press(w.VK_TAB)
    assert len(kb.hook.drain()) == 64
