"""Native confirmation for the local desktop automation preference."""

import logging

from .desktop_automation import save_desktop_enabled

LOG = logging.getLogger(__name__)


def show_preference(window, automation, directory):
    """Show the current state and persist the user's confirmed choice."""
    enabled = automation.enabled
    if enabled:
        title = "Выключить мост MCP?"
        message = "Мост включён. Локальные MCP-клиенты сразу потеряют доступ к окну. Настройка сохранится после перезапуска."
    else:
        title = "Включить мост MCP?"
        message = (
            "Мост выключен. Доверенные локальные MCP-клиенты смогут читать состояние окна, "
            "переключать разделы и получать снимки видимых данных. "
            "Настройка сохранится после перезапуска. Переподключите MCP-клиент, чтобы обновить список инструментов."
        )
    if not window.create_confirmation_dialog(title, message):
        return
    try:
        saved = automation.set_enabled(enabled, not enabled, lambda value: save_desktop_enabled(directory, value))
    except OSError:
        LOG.exception("Could not save the desktop MCP preference")
        window.create_confirmation_dialog("Настройка не сохранена", "Проверьте доступ на запись в каталог данных ag-tasks.")
        return
    if not saved:
        window.create_confirmation_dialog("Настройка изменилась", "Откройте «Мост MCP…» снова, чтобы увидеть текущее состояние.")
