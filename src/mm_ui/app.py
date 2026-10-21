"""GUI entry point (command name: mm_ui._meta.APP_NAME).

Env vars:
  MM_BACKEND=fake|real|auto   which service to use (see mm_ui/backend.py; default fake)
  MM_BACKEND_PATH=<checkout>  where `music_manager.service` lives for real/auto
  MM_SMOKE=1     quit the event loop after ~1 s and exit 0 (packaging smoke test); no dialogs
  MM_WINDOW=review  open the review window instead of the library window
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    from . import __version__
    from ._meta import APP_ID, APP_NAME, DISPLAY_NAME
    from .backend import BackendUnavailable, get_service

    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationDisplayName(DISPLAY_NAME)
    QApplication.setApplicationVersion(__version__)
    # Wayland app_id: lets compositors match the window to <APP_ID>.desktop
    QApplication.setDesktopFileName(APP_ID)
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon.fromTheme(APP_ID))

    def notify(msg: str, fatal: bool) -> None:
        print(("ERROR: " if fatal else "WARNING: ") + msg, file=sys.stderr, flush=True)  # always on stderr
        if not os.environ.get("MM_SMOKE"):
            box = QMessageBox.critical if fatal else QMessageBox.warning
            box(None, f"{DISPLAY_NAME} — backend", msg)

    try:
        svc = get_service(notify)
    except BackendUnavailable:
        return 2

    if os.environ.get("MM_WINDOW") == "review" or "--review" in sys.argv:
        from .review import ReviewWindow
        win = ReviewWindow(svc)
    else:
        from .library import LibraryWindow
        win = LibraryWindow(svc)
    win.show()

    if os.environ.get("MM_SMOKE"):
        def _done():
            print(f"MM_SMOKE ok: platform={app.platformName()} window={type(win).__name__} "
                  f"title={win.windowTitle()!r} backend={svc.kind}", flush=True)
            app.quit()
        QTimer.singleShot(1000, _done)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
