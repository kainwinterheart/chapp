"""Application entry point."""

import sys
from PyQt6.QtWidgets import QApplication

from chat_window import ChatWindow, STYLESHEET
from settings import read_font_sizes

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    font_sizes = read_font_sizes()
    base = font_sizes.get("base", 13)
    app.setStyleSheet(STYLESHEET.replace("font-size: 13px;", f"font-size: {base}px;"))
    window = ChatWindow(schema={})
    window.show()
    sys.exit(app.exec())
