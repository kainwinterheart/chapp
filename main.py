"""Application entry point."""

import sys
from PyQt6.QtWidgets import QApplication

from chat_window import ChatWindow, STYLESHEET

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    window = ChatWindow(schema={})
    window.show()
    sys.exit(app.exec())
