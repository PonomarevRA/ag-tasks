"""Native update result and lifecycle contracts without opening browser or dialogs."""

from threading import Event
from types import SimpleNamespace

import pytest

from ag_tasks import desktop_update_menu
from ag_tasks.desktop_update_menu import DesktopUpdateMenu


RELEASES = "https://github.com/PonomarevRA/ag-tasks/releases"
LATEST = RELEASES + "/tag/v0.8.4"


def checked_window(monkeypatch, fetch, *, confirm=True):
    """Capture dialog text and browser dispatch for one owned native window."""
    dialogs = []
    opened = []

    def dialog(title, message):
        """Record the single prompt and return the user's fixture decision."""
        dialogs.append((title, message))
        return confirm

    monkeypatch.setattr(desktop_update_menu.webbrowser, "open", opened.append)
    window = SimpleNamespace(create_confirmation_dialog=dialog)
    return DesktopUpdateMenu(window, current_version="0.8.3", releases_url=RELEASES, fetch=fetch), dialogs, opened


def finish_check(menu):
    """Wait briefly for an admitted worker to release its manual-check lock."""
    assert menu.in_flight.acquire(timeout=2), "manual update worker did not settle"
    menu.in_flight.release()


@pytest.mark.parametrize("confirm", [True, False])
def test_menu_offers_latest_release_and_short_russian_notes(monkeypatch, confirm):
    """A manual prompt identifies the installed version and honors the explicit choice."""
    menu, dialogs, opened = checked_window(monkeypatch, lambda: {
        "available": True, "version": "v0.8.4", "url": LATEST,
        "changelog": {"ru": ["Быстрее открывается карточка"], "en": ["Faster task opening"]},
    }, confirm=confirm)

    assert [title for title, _ in menu.actions()] == ["Проверить обновления…", "Релизы на GitHub"]
    menu.actions()[0][1]()
    finish_check(menu)
    assert len(dialogs) == 1
    assert "0.8.3" in dialogs[0][1] and "v0.8.4" in dialogs[0][1]
    assert "• Быстрее открывается карточка" in dialogs[0][1]
    assert "Faster task opening" not in dialogs[0][1]
    assert opened == ([LATEST] if confirm else [])


def test_up_to_date_and_unavailable_checks_can_open_fixed_index(monkeypatch):
    """The Help index and fallback destination point to the product release list."""
    menu, dialogs, opened = checked_window(monkeypatch, lambda: {
        "available": False, "version": "v0.8.3", "url": RELEASES + "/tag/v0.8.3",
        "changelog": {"ru": [], "en": []},
    })
    menu.actions()[1][1]()
    assert opened == [RELEASES]
    assert dialogs == []

    menu.check()
    finish_check(menu)
    assert "Последний релиз на GitHub — v0.8.3" in dialogs[0][1]
    assert "Более новой версии пока нет" in dialogs[0][1]
    assert opened == [RELEASES, RELEASES]

    def offline():
        """Simulate an unavailable service without returning a remote error body."""
        raise OSError("network details")

    menu.fetch = offline
    menu.check()
    finish_check(menu)
    assert "Не удалось проверить релизы" in dialogs[1][1]
    assert "network details" not in dialogs[1][1]
    assert opened == [RELEASES, RELEASES, RELEASES]


def test_one_check_owns_help_and_tray_and_quit_suppresses_dialog(monkeypatch):
    """The UI callback returns during a network wait, and a closed window gets no result."""
    entered = Event()
    release = Event()
    fetches = []

    def slow():
        """Hold the first check while exercising re-entry and close."""
        fetches.append(True)
        entered.set()
        assert release.wait(timeout=2)
        return {"available": True, "version": "v0.8.4", "url": LATEST,
                "changelog": {"ru": [], "en": ["A bounded note"]}}

    menu, dialogs, opened = checked_window(monkeypatch, slow)
    try:
        menu.actions()[0][1]()
        assert entered.wait(timeout=1)
        menu.check()  # The tray points to this same action.
        menu.close()
        menu.open_releases()
    finally:
        release.set()
        finish_check(menu)
    menu.check()
    assert fetches == [True]
    assert dialogs == []
    assert opened == []


def test_background_update_action_uses_the_shared_manual_check(monkeypatch):
    """The tray entry avoids its legacy worker when the application supplies a checker."""
    from desktop_support.background import BackgroundHandle

    def reject_legacy(*args):
        """The tray must never bypass the application's check policy."""
        pytest.fail("legacy tray checker was called")

    monkeypatch.setattr("desktop_support.background.check_release", reject_legacy)
    called = []
    handle = SimpleNamespace(check_updates=lambda: called.append(True))
    BackgroundHandle.updates(handle)
    assert called == [True]



def test_newer_release_with_english_notes_and_without_notes(monkeypatch):
    """Missing Russian notes use the available translation and missing notes add no empty label."""
    result = {"available": True, "version": "v0.8.4", "url": LATEST,
              "changelog": {"ru": [], "en": ["A compact note"]}}
    menu, dialogs, _ = checked_window(monkeypatch, lambda: result)
    menu.check()
    finish_check(menu)
    assert "Что нового" in dialogs[0][1] and "• A compact note" in dialogs[0][1]
    result["changelog"]["en"].clear()
    menu.check()
    finish_check(menu)
    assert "Что нового" not in dialogs[1][1]
