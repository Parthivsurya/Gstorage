"""Application entry point.

Opens (creating if needed) the local SQLite index, then launches the Qt window.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from gstore import applog
from gstore.config import APP_NAME
from gstore.db import Database
from gstore.ui.main_window import MainWindow


def main() -> int:
    applog.setup()
    log = applog.get(__name__)
    log.info("Gstore starting; log at %s", applog.log_path())

    db = Database().open().initialize()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    window = MainWindow(db)
    window.show()

    try:
        return app.exec()
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
