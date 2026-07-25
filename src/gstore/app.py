"""Application entry point.

Opens (creating if needed) the local SQLite index, then launches the Qt window.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from gstore.config import APP_NAME
from gstore.db import Database
from gstore.ui.main_window import MainWindow


def main() -> int:
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
