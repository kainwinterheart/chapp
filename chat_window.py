"""Chat window UI with sidebar, message log, and input bar."""

import re

from PyQt6.QtCore import QEvent, QObject, QPoint, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QKeyEvent, QMouseEvent, QPainter, QPen, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QDialog,
    QApplication,
    QFileDialog,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QScrollArea,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from data_persistence import DataPersistenceManager
from settings import read_settings as _read_settings, save_settings as _save_settings
from subprocess_manager import SubprocessManager

# ── Shared constants ───────────────────────────────────────────────────────

_TITLE_BAR_HEIGHT = 32
_BUTTON_SPACING = 0
BUTTON_SIZE = 28
ICON_SPACER = 10


# ── Qt Stylesheet Constants ──────────────────────────────────────────────

STYLESHEET = """
QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: monospace;
    font-size: 13px;
}

QListWidget {
    background-color: #181825;
    color: #cdd6f4;
    border: none;
    padding: 4px;
}

QListWidget::item {
    padding: 6px 8px;
    border-radius: 4px;
}

QListWidget::item:selected {
    background-color: #313244;
}

QTextEdit {
    background-color: #1e1e2e;
    color: #cdd6f4;
    border: none;
    padding: 4px;
}

QTextEdit:disabled {
    background-color: #11111b;
    color: #585b70;
    border: 1px solid #313244;
    border-radius: 4px;
    padding: 4px;
}

QPushButton {
    background-color: #89b4fa;
    color: #1e1e2e;
    border: none;
    border-radius: 4px;
    padding: 8px 16px;
    font-weight: bold;
}

QPushButton:hover {
    background-color: #74c7ec;
}

QPushButton:disabled {
    background-color: #45475a;
    color: #6c7086;
}

QScrollBar:vertical {
    background-color: #181825;
    width: 8px;
    border: none;
}

QScrollBar::handle:vertical {
    background-color: #313244;
    border-radius: 4px;
    min-height: 20px;
}

QLabel {
    color: #a6adc8;
}
"""


# ── Title bar button states ──────────────────────────────────────────────

_TITLE_BAR_NORMAL, _TITLE_BAR_HOVER, _TITLE_BAR_PRESSED = 0, 1, 2


# ── Frameless-window mixin (shared shadow + resize borders) ──────────────

class _FramelessMixin:
    """Shared behaviour for frameless windows: shadow effect and resize-border
    helpers.  Event-filter plumbing is provided via the module-level
    ``_handle_frameless_event`` helper so subclasses can call it from their
    own ``eventFilter`` without hitting MRO issues.
    """

    _RESIZE_BORDER = 5
    _SHADOW_BLUR = 24
    _SHADOW_ALPHA = 80

    # ── Shadow ────────────────────────────────────────────────────────

    def _apply_shadow(self) -> None:
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(self._SHADOW_BLUR)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, self._SHADOW_ALPHA))
        self.setGraphicsEffect(shadow)

    # ── Resize-edge helpers ───────────────────────────────────────────

    def _edges_at(self, x: int, y: int) -> Qt.Edge:
        edges = Qt.Edge(0)
        if x < self._RESIZE_BORDER:
            edges |= Qt.Edge.LeftEdge
        if x >= self.width() - self._RESIZE_BORDER:
            edges |= Qt.Edge.RightEdge
        if y < self._RESIZE_BORDER:
            edges |= Qt.Edge.TopEdge
        if y >= self.height() - self._RESIZE_BORDER:
            edges |= Qt.Edge.BottomEdge
        return edges

    @staticmethod
    def _cursor_for_edges(edges: int | Qt.Edge) -> Qt.CursorShape:
        diag1 = Qt.Edge.LeftEdge | Qt.Edge.TopEdge
        diag2 = Qt.Edge.RightEdge | Qt.Edge.BottomEdge
        diag3 = Qt.Edge.RightEdge | Qt.Edge.TopEdge
        diag4 = Qt.Edge.LeftEdge | Qt.Edge.BottomEdge
        if edges & diag1 or edges & diag2:
            return Qt.CursorShape.SizeFDiagCursor
        if edges & diag3 or edges & diag4:
            return Qt.CursorShape.SizeBDiagCursor
        if edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge):
            return Qt.CursorShape.SizeVerCursor
        if edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge):
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.ArrowCursor

    # ── Global event filter installer ─────────────────────────────────

    def _install_frameless_filter(self) -> None:
        QApplication.instance().installEventFilter(self)


def _title_bar_height_for(widget: QWidget) -> int:
    """Return the title-bar height for *widget*, falling back to the
    module-level constant when the widget doesn't carry one."""
    return getattr(widget, "_title_bar_height", _TITLE_BAR_HEIGHT)


def _handle_frameless_event(
    self_obj: object,
    obj: QObject,
    event: QEvent,
) -> bool:
    """Shared event-filter logic for frameless windows.

    Handles:
    - Ctrl+C → kill subprocess (if ``self_obj`` has ``subprocess_mgr``)
    - Resize-border mouse events

    Returns ``True`` if the event was handled, ``False`` to let the
    subclass handle it further.
    """
    # Skip when a modal dialog is open (e.g. Settings) — let Qt deliver
    # events to the dialog instead of stealing them via this filter.
    if getattr(self_obj, "modalDialogOpen", False):
        return False

    etype = event.type()

    # --- Ctrl+C kill subprocess (catches events from any widget) ---
    if (etype == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_C
            and event.modifiers() == Qt.KeyboardModifier.ControlModifier):
        mgr = getattr(self_obj, "subprocess_mgr", None)
        if mgr and mgr.is_running():
            mgr.signal_kill_requested.emit()
            return True

    # --- Resize-border handling ---
    if etype not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove,
                     QEvent.Type.MouseButtonRelease):
        return False

    me = event  # type: ignore[assignment]
    button = me.button() if hasattr(me, "button") else Qt.MouseButton.NoButton

    def _window_local(evt: QEvent) -> QPoint:
        """Convert any mouse event's position to window-local coords."""
        lp = evt.localPos() if hasattr(evt, "localPos") else evt.pos()
        wx, wy = int(lp.x()), int(lp.y())
        if obj is not self_obj and hasattr(obj, "mapTo") and self_obj.isAncestorOf(obj):  # type: ignore[attr-defined]
            return obj.mapTo(self_obj, QPoint(wx, wy))  # type: ignore[arg-type]
        return QPoint(wx, wy)

    # Access per-instance resize state
    edges_attr = "_resize_edges"
    pos_attr = "_resize_start_pos"
    geom_attr = "_resize_start_geom"

    if etype == QEvent.Type.MouseButtonPress:
        if (button == Qt.MouseButton.LeftButton
                and self_obj.windowState() == Qt.WindowState.WindowNoState):  # type: ignore[attr-defined]
            wp = _window_local(me)
            if wp.y() >= _title_bar_height_for(self_obj):  # type: ignore[arg-type]
                edges = getattr(self_obj, "_edges_at")(wp.x(), wp.y())  # type: ignore[attr-defined]
                if edges:
                    setattr(self_obj, edges_attr, edges)
                    gx = int(me.globalPosition().x())
                    gy = int(me.globalPosition().y())
                    setattr(self_obj, pos_attr, (gx, gy))
                    g = self_obj.geometry()  # type: ignore[attr-defined]
                    setattr(self_obj, geom_attr, (g.x(), g.y(), g.width(), g.height()))
                    self_obj.setCursor(  # type: ignore[attr-defined]
                        _FramelessMixin._cursor_for_edges(edges)
                    )
                    return True

    elif etype == QEvent.Type.MouseMove:
        if getattr(self_obj, edges_attr, 0):  # type: ignore[attr-defined]
            gx = int(me.globalPosition().x())
            gy = int(me.globalPosition().y())
            sx, sy, sw, sh = getattr(self_obj, geom_attr, (0, 0, 0, 0))  # type: ignore[attr-defined]
            dx = gx - getattr(self_obj, pos_attr, (0, 0))[0]  # type: ignore[attr-defined]
            dy = gy - getattr(self_obj, pos_attr, (0, 0))[1]  # type: ignore[attr-defined]
            ex, ey, ew, eh = sx, sy, sw, sh
            if getattr(self_obj, edges_attr, 0) & Qt.Edge.LeftEdge:  # type: ignore[attr-defined]
                ex, ew = sx + dx, sw - dx
            if getattr(self_obj, edges_attr, 0) & Qt.Edge.TopEdge:  # type: ignore[attr-defined]
                ey, eh = sy + dy, sh - dy
            if getattr(self_obj, edges_attr, 0) & Qt.Edge.RightEdge:  # type: ignore[attr-defined]
                ew = sw + dx
            if getattr(self_obj, edges_attr, 0) & Qt.Edge.BottomEdge:  # type: ignore[attr-defined]
                eh = sh + dy
            self_obj.setGeometry(ex, ey, max(ew, self_obj.minimumWidth()),  # type: ignore[attr-defined]
                                 max(eh, self_obj.minimumHeight()))  # type: ignore[attr-defined]
            return True
        if self_obj.windowState() == Qt.WindowState.WindowNoState:  # type: ignore[attr-defined]
            wp = _window_local(me)
            if wp.y() >= _title_bar_height_for(self_obj):  # type: ignore[arg-type]
                edges = getattr(self_obj, "_edges_at")(wp.x(), wp.y())  # type: ignore[attr-defined]
                if edges:
                    self_obj.setCursor(  # type: ignore[attr-defined]
                        _FramelessMixin._cursor_for_edges(edges)
                    )
                    return True
            self_obj.setCursor(Qt.CursorShape.ArrowCursor)  # type: ignore[attr-defined]

    elif etype == QEvent.Type.MouseButtonRelease:
        if getattr(self_obj, edges_attr, 0):  # type: ignore[attr-defined]
            setattr(self_obj, edges_attr, 0)
            self_obj.setCursor(Qt.CursorShape.ArrowCursor)  # type: ignore[attr-defined]
            return True

    return False


# ── Title bar button base ────────────────────────────────────────────────

class _TitleBarButton(QPushButton):
    """Base for title-bar buttons — tracks hover/press state and colours."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._state = _TITLE_BAR_NORMAL
        self._normalColor = QColor(0, 0, 0)
        self._hoverColor = QColor(0, 0, 0)
        self._pressedColor = QColor(0, 0, 0)
        self._normalBgColor = QColor(0, 0, 0)
        self._hoverBgColor = QColor(0, 0, 0, 26)
        self._pressedBgColor = QColor(0, 0, 0, 51)

    # ── State management ──────────────────────────────────────────────

    def setState(self, state: int) -> None:
        self._state = state
        self.update()

    def _get_colors(self) -> tuple[QColor, QColor]:
        if self._state == _TITLE_BAR_HOVER:
            return self._hoverColor, self._hoverBgColor
        if self._state == _TITLE_BAR_PRESSED:
            return self._pressedColor, self._pressedBgColor
        return self._normalColor, self._normalBgColor

    # ── Mouse / keyboard events ───────────────────────────────────────

    def enterEvent(self, event) -> None:
        self.setState(_TITLE_BAR_HOVER)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.setState(_TITLE_BAR_NORMAL)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setState(_TITLE_BAR_PRESSED)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self.setState(_TITLE_BAR_HOVER if self.isActiveWindow() else _TITLE_BAR_NORMAL)
        super().mouseReleaseEvent(event)


# ── Settings Dialog ──────────────────────────────────────────────────────

class SettingsDialog(QDialog, _FramelessMixin):
    """Modal dialog for configuring the subprocess binary path.

    Uses the same frameless style, shadow, resize borders, and title bar as
    the main chat window.
    """

    _title_bar_height = _TITLE_BAR_HEIGHT

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowTitle("Settings")
        self.setMinimumSize(400, 140)
        self.setFixedSize(400, 180)

        # Visible border so the dialog doesn't blend into the main window
        self.setStyleSheet("QDialog { border: 1px solid #313244; border-radius: 8px; background-color: #1e1e2e; }")

        # Frameless window plumbing
        self._apply_shadow()
        # NOTE: we deliberately do NOT install a global event filter on the
        # dialog – the parent window's filter already handles global events.
        # Installing one here caused the dialog to intercept clicks on the
        # parent window and close itself.

        # Title bar (shared widget)
        self._tbar = _UnifiedTitleBar(
            self,
            buttons=[CloseButton(self)],
            title="Settings",
            on_double_click='close',
            has_context_menu=False,
        )
        self._tbar.setParent(self)
        self._tbar.raise_()

        # Body
        current = _read_settings().get("binary_path", "")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, _TITLE_BAR_HEIGHT + 8, 16, 16)
        layout.setSpacing(12)

        # Label
        label = QLabel("Binary path:")
        label.setStyleSheet("color: #a6adc8; font-size: 13px;")
        layout.addWidget(label)

        # Path input row
        path_row = QHBoxLayout()
        self._path_edit = QTextEdit()
        self._path_edit.setPlainText(current)
        self._path_edit.setMaximumHeight(60)
        self._path_edit.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e2e;
                color: #cdd6f4;
                border: 1px solid #313244;
                border-radius: 4px;
                padding: 4px 8px;
                font-family: monospace;
                font-size: 13px;
            }
        """)
        path_row.addWidget(self._path_edit, 1)

        browse_btn = QPushButton("Browse")
        browse_btn.setStyleSheet("""
            QPushButton {
                background-color: #313244;
                color: #cdd6f4;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #45475a; }
        """)
        browse_btn.clicked.connect(self._browse_file)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(browse_btn.styleSheet())
        cancel_btn.clicked.connect(self.reject)

        save_btn = QPushButton("Save")
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #89b4fa;
                color: #1e1e2e;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-weight: bold;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #74c7ec; }
        """)
        save_btn.clicked.connect(self._save)

        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        self._resize_title_bar()

    def _resize_title_bar(self) -> None:
        if hasattr(self, "_tbar"):
            self._tbar.resize(self.width(), _TITLE_BAR_HEIGHT)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_title_bar()

    # ── Event filter (resize borders + keyboard) ───────────────────────

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if _handle_frameless_event(self, obj, event):
            return True
        return super().eventFilter(obj, event)  # type: ignore[misc]

    # ── Actions ─────────────────────────────────────────────────────────

    def _browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Binary", "", "Executable Files (*)"
        )
        if path:
            self._path_edit.setPlainText(path)

    def _save(self) -> None:
        path = self._path_edit.toPlainText().strip()
        settings = _read_settings()
        if path:
            settings["binary_path"] = path
        else:
            settings.pop("binary_path", None)
        _save_settings(settings)
        self.accept()


# ── Unified title bar ───────────────────────────────────────────────────

class _UnifiedTitleBar(QWidget):
    """Shared title bar for frameless windows (ChatWindow + SettingsDialog).

    Handles: drag-to-move, double-click action (maximise or close),
    optional right-click context menu, and macOS opaque background.
    """

    settings_requested = pyqtSignal()

    def __init__(self, parent, buttons=None, title="Chapp",
                 on_double_click='maximize', has_context_menu=False):
        super().__init__(parent)
        self.setFixedHeight(_TITLE_BAR_HEIGHT)

        # ── macOS fix: ensure the title bar is opaque ───────────────────
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setStyleSheet("background-color: #1e1e2e;")

        self._parent_win = parent
        self._on_double_click = on_double_click
        self._buttons = list(buttons or [])
        for btn in self._buttons:
            btn.setParent(self)

        # ── Context menu (main window only) ─────────────────────────────
        self._context_menu = None
        if has_context_menu:
            self._context_menu = QMenu(self)
            self._settings_action = QAction("Settings", self)
            self._settings_action.triggered.connect(self.settings_requested.emit)
            self._context_menu.addAction(self._settings_action)

        # ── Layout ──────────────────────────────────────────────────────
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(_BUTTON_SPACING)
        for btn in self._buttons:
            btn_layout.addWidget(btn)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(6)
        layout.addSpacing(ICON_SPACER)

        self.title_label = QLabel(title)
        self.title_label.setStyleSheet(
            "color: #a6adc8; font-size: 12px; font-weight: bold; background: transparent;"
        )
        layout.addWidget(self.title_label, 1)
        layout.addStretch(1)
        layout.addLayout(btn_layout)

        # ── Install event filter on parent for window-state tracking ─────
        if on_double_click == 'maximize':
            self._parent_win.installEventFilter(self)

    # ── Event filter (sync maximise button state) ───────────────────────

    def eventFilter(self, obj, event):
        if obj is self._parent_win and event.type() == QEvent.Type.WindowStateChange:
            for btn in self._buttons:
                if isinstance(btn, MaximizeButton):
                    btn.set_max_state(self._parent_win.isMaximized())
        return super().eventFilter(obj, event)

    # ── Drag helpers ────────────────────────────────────────────────────

    def _is_drag_region(self, pos):
        width = sum(btn.width() for btn in self._buttons if btn.isVisible())
        return 0 < pos.x() < self.width() - width

    def _has_button_pressed(self):
        return any(
            getattr(btn, '_state', _TITLE_BAR_NORMAL) == _TITLE_BAR_PRESSED
            for btn in self._buttons
        )

    def _can_drag(self, pos):
        return self._is_drag_region(pos) and not self._has_button_pressed()

    # ── Mouse events ────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.RightButton:
            if self._context_menu:
                self._context_menu.exec(event.globalPosition().toPoint())
                event.accept()
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and self._can_drag(event.pos())):
            self._parent_win.windowHandle().startSystemMove()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if (event.buttons() & Qt.MouseButton.LeftButton
                and self._can_drag(event.pos())):
            self._parent_win.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.RightButton:
            return
        if self._on_double_click == 'maximize':
            if self._parent_win.isMaximized():
                self._parent_win.showNormal()
            else:
                self._parent_win.showMaximized()
        elif self._on_double_click == 'close':
            if hasattr(self._parent_win, 'reject'):
                self._parent_win.reject()
            else:
                self._parent_win.close()


# ── Title bar button classes ─────────────────────────────────────────────

class CloseButton(_TitleBarButton):
    """Styled close button with red hover state."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._normalColor = QColor(186, 194, 222)
        self._hoverColor = QColor(255, 255, 255)
        self._pressedColor = QColor(255, 255, 255)
        self._normalBgColor = QColor(0, 0, 0)
        self._hoverBgColor = QColor(243, 136, 168)
        self._pressedBgColor = QColor(243, 136, 168, 180)
        self.clicked.connect(self._close)

    def _close(self):
        if self.parent():
            self.parent().window().close()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing)
        color, bg_color = self._get_colors()
        painter.setBrush(bg_color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(self.rect())
        painter.setPen(QPen(color, 2))
        size = 8
        offset = (self.width() - size) // 2
        painter.drawLine(offset, offset, offset + size, offset + size)
        painter.drawLine(offset + size, offset, offset, offset + size)


class MinimizeButton(_TitleBarButton):
    """Styled minimize button."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._normalColor = QColor(166, 173, 200)
        self._hoverColor = QColor(255, 255, 255)
        self._pressedColor = QColor(255, 255, 255)
        self._normalBgColor = QColor(0, 0, 0)
        self._hoverBgColor = QColor(49, 50, 68)
        self._pressedBgColor = QColor(49, 50, 68, 180)
        self.clicked.connect(self._minimize)

    def _minimize(self):
        if self.parent():
            self.parent().window().showMinimized()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing)
        color, bg_color = self._get_colors()
        painter.setBrush(bg_color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(self.rect())
        painter.setPen(QPen(color, 2))
        center_y = self.height() // 2
        painter.drawLine(8, center_y, 20, center_y)


class MaximizeButton(_TitleBarButton):
    """Styled maximize/restore button."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._normalColor = QColor(166, 173, 200)
        self._hoverColor = QColor(255, 255, 255)
        self._pressedColor = QColor(255, 255, 255)
        self._normalBgColor = QColor(0, 0, 0)
        self._hoverBgColor = QColor(49, 50, 68)
        self._pressedBgColor = QColor(49, 50, 68, 180)
        self._is_max = False
        self.clicked.connect(self._toggle_max)

    def _toggle_max(self):
        if self.window().isMaximized():
            self.window().showNormal()
        else:
            self.window().showMaximized()

    def set_max_state(self, is_max):
        if self._is_max == is_max:
            return
        self._is_max = is_max
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing)
        color, bg_color = self._get_colors()
        painter.setBrush(bg_color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(self.rect())
        painter.setPen(QPen(color, 1.5))
        s = 8
        o = (self.width() - s) // 2
        if not self._is_max:
            painter.drawRect(o, o + 2, s, s)
        else:
            painter.drawRect(o, o + 4, s - 2, s - 2)
            painter.drawLine(o + 2, o + 4, o + 2, o + 2)
            painter.drawLine(o + 2, o + 2, o + s, o + 2)


# ── SidebarPanel ─────────────────────────────────────────────────────────

class SidebarPanel(QWidget):
    """Left panel displaying the conversation list."""

    conversation_clicked = pyqtSignal(str)
    new_conversation_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active_id = None

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        self._list = QListWidget()
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.setWordWrap(True)
        scroll.setWidget(self._list)

        self._new_btn = QPushButton("+ New Conversation")
        self._new_btn.clicked.connect(self._on_new_conversation)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll, 1)
        layout.addWidget(self._new_btn)

        self.setLayout(layout)

    def _on_item_clicked(self, item: QListWidgetItem):
        conv_id = item.data(Qt.ItemDataRole.UserRole)
        if conv_id is not None:
            self.conversation_clicked.emit(conv_id)

    def _on_new_conversation(self):
        self.new_conversation_requested.emit()

    def update_conversations(self, conversations: list):
        """Refresh the sidebar with the given conversation summaries."""
        self._list.clear()
        for conv in conversations:
            conv_id = conv.get("conversation_id", "")
            preview = conv.get("preview", "").replace("\n", " ")
            preview = re.sub(r"\s+", " ", preview)
            if len(preview) > 60:
                preview = preview[:57] + "..."
            label = preview if preview else conv_id
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, conv_id)
            if conv_id == self._active_id:
                item.setBackground(QColor("#45475a"))
            self._list.addItem(item)

    def set_active_conversation(self, conversation_id: str):
        """Mark a conversation as active and highlight it."""
        self._active_id = conversation_id
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == conversation_id:
                item.setBackground(QColor("#45475a"))
            else:
                item.setBackground(Qt.GlobalColor.transparent)


# ── MessageLogPanel ──────────────────────────────────────────────────────


class MessageLogPanel(QWidget):
    """Center panel displaying message history with optional stderr overlay."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._stderr_marker_pos = None

        # Chunk buffering
        self._stderr_buffer = ""
        self._stderr_flush_timer = QTimer(self)
        self._stderr_flush_timer.setSingleShot(True)
        self._stderr_flush_timer.setInterval(16)
        self._stderr_flush_timer.timeout.connect(self._flush_stderr_buffer)

        self._stderr_fmt = QTextCharFormat()
        self._stderr_fmt.setFontPointSize(8)
        self._stderr_fmt.setForeground(QColor("#f38ba8"))

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._text_edit)
        self.setLayout(layout)

    def append_message(self, role: str, content: str):
        """Append a styled message block to the log."""
        cursor = self._text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        if role == "user":
            escaped = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            html = (
                f'<table style="width:auto;border-collapse:collapse;margin:2px 0;" align="right">'
                f'<tr><td style="background-color:#313244;padding:6px 12px;">'
                f'<font color="#a6e3a1">{escaped.replace(chr(10), "<br>")}</font>'
                f'</td></tr></table>'
            )
            cursor.insertHtml(html)
        else:
            fmt = self._format_for_role(role)
            cursor.insertText(content, fmt)

        cursor.insertBlock()
        cursor.insertBlock()

        self._text_edit.setTextCursor(cursor)

    def _format_for_role(self, role: str) -> QTextCharFormat:
        fmt = QTextCharFormat()
        if role == "error":
            fmt.setForeground(QColor("#f38ba8"))
        else:
            fmt.setForeground(QColor("#89b4fa"))
        return fmt

    def create_stderr_region(self):
        """Mark the document position where stderr content will begin."""
        cursor = self._text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertBlock()
        self._stderr_marker_pos = cursor.position()

    def append_stderr_chunk(self, chunk: str):
        """Buffer a stderr chunk and schedule a periodic flush."""
        self._stderr_buffer += chunk
        self._stderr_flush_timer.start()

    def _flush_stderr_buffer(self):
        """Flush buffered stderr into the document in a single operation."""
        if not self._stderr_buffer:
            return
        text, self._stderr_buffer = self._stderr_buffer, ""
        cursor = QTextCursor(self._text_edit.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text, self._stderr_fmt)
        scrollbar = self._text_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def remove_stderr_region(self):
        """Remove the stderr block from the end of the document."""
        self._stderr_buffer = ""
        self._stderr_flush_timer.stop()
        if self._stderr_marker_pos is None:
            return
        cursor = QTextCursor(self._text_edit.document())
        cursor.setPosition(self._stderr_marker_pos)
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        self._stderr_marker_pos = None

    def clear(self):
        """Clear all messages from the log."""
        self._text_edit.clear()
        self._stderr_marker_pos = None
        self._stderr_buffer = ""
        self._stderr_flush_timer.stop()

    def scroll_to_bottom(self):
        """Scroll the view to the bottom."""
        scrollbar = self._text_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


# ── InputBar ─────────────────────────────────────────────────────────────

class InputBar(QWidget):
    """Bottom panel with message composition text edit. Submit via Ctrl+Enter."""

    submitted = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._text_edit = QTextEdit()
        self._text_edit.setPlaceholderText("Type your message...")

        layout = QHBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self._text_edit, 1)
        self.setLayout(layout)

    def _on_submit(self):
        text = self._text_edit.toPlainText().strip()
        if text:
            self.submitted.emit(text)
            self._text_edit.clear()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Return and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._on_submit()
            return
        super().keyPressEvent(event)

    def get_text(self) -> str:
        return self._text_edit.toPlainText().strip()

    def set_enabled(self, enabled: bool):
        self._text_edit.setEnabled(enabled)


# ── ChatWindow ───────────────────────────────────────────────────────────

class ChatWindow(QWidget, _FramelessMixin):
    """Main window containing SidebarPanel, MessageLogPanel, and InputBar.

    Uses a custom title bar for native drag, resize borders, and maximise
    / minimise controls.
    """

    _title_bar_height = _TITLE_BAR_HEIGHT

    def __init__(self, schema: dict, timeout: str | None = None):
        super().__init__()

        # Strip the native window frame
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)

        self.setWindowTitle("Chat Window")
        self.resize(900, 600)

        # Title bar
        self.custom_title_bar = _UnifiedTitleBar(
            self,
            buttons=[MinimizeButton(self), MaximizeButton(self), CloseButton(self)],
            title="Chapp",
            on_double_click='maximize',
            has_context_menu=True,
        )
        self.custom_title_bar.settings_requested.connect(self._open_settings)
        self.custom_title_bar.setParent(self)
        self.custom_title_bar.raise_()

        # Shadow effect for window edges
        self._apply_shadow()

        # Install global event filter (resize borders + keyboard shortcuts)
        self._install_frameless_filter()

        self.persistence = DataPersistenceManager()
        self.persistence.load_all()

        self.subprocess_mgr = SubprocessManager(schema, timeout)

        self.sidebar = SidebarPanel()
        self.message_log = MessageLogPanel()
        self.input_bar = InputBar()

        # Wire subprocess signals
        self.subprocess_mgr.signal_stderr_chunk.connect(
            self.message_log.append_stderr_chunk
        )
        self.subprocess_mgr.signal_completed.connect(self.on_completed)
        self.subprocess_mgr.signal_error.connect(self.on_error)
        self.subprocess_mgr.signal_finished.connect(self.on_finished)

        # Wire sidebar click
        self.sidebar.conversation_clicked.connect(self.on_conversation_selected)
        self.sidebar.new_conversation_requested.connect(self.reset_active_state)

        # Wire input submit
        self.input_bar.submitted.connect(self.on_submit)

        # Two-level QSplitter layout with visible separators as splitter children
        sep_vertical = QFrame()
        sep_vertical.setFrameShape(QFrame.Shape.HLine)
        sep_vertical.setFixedHeight(1)
        sep_vertical.setStyleSheet("background-color: #313244;")
        sep_vertical.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        vertical_splitter.addWidget(self.message_log)
        vertical_splitter.addWidget(sep_vertical)
        vertical_splitter.addWidget(self.input_bar)
        vertical_splitter.setSizes([450, 1, 150])
        vertical_splitter.setHandleWidth(1)
        vertical_splitter.setStyleSheet("""
            QSplitter::handle:vertical {
                background-color: #313244;
                height: 1px;
            }
        """)

        center_container = QWidget()
        center_layout = QVBoxLayout()
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.addWidget(vertical_splitter)
        center_container.setLayout(center_layout)

        sep_horizontal = QFrame()
        sep_horizontal.setFrameShape(QFrame.Shape.VLine)
        sep_horizontal.setFixedWidth(1)
        sep_horizontal.setStyleSheet("background-color: #313244;")
        sep_horizontal.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        horizontal_splitter = QSplitter(Qt.Orientation.Horizontal)
        horizontal_splitter.addWidget(self.sidebar)
        horizontal_splitter.addWidget(sep_horizontal)
        horizontal_splitter.addWidget(center_container)
        horizontal_splitter.setSizes([225, 1, 675])
        horizontal_splitter.setHandleWidth(1)
        horizontal_splitter.setStyleSheet("""
            QSplitter::handle:horizontal {
                background-color: #313244;
                width: 1px;
            }
        """)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self.custom_title_bar)
        main_layout.addWidget(horizontal_splitter)
        self.setLayout(main_layout)

        self._refresh_sidebar()

        self._load_conversation_messages(self.persistence.get_active_conversation_id())

    # ── Layout helpers ──────────────────────────────────────────────────

    def _resize_title_bar(self):
        """Keep the title bar wide enough to cover the window."""
        self.custom_title_bar.resize(self.width(), self.custom_title_bar.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_title_bar()

    # ── Event filter (resize borders + keyboard) ───────────────────────

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if _handle_frameless_event(self, obj, event):
            return True
        return super().eventFilter(obj, event)  # type: ignore[misc]

    # ── Business logic ─────────────────────────────────────────────────

    def _refresh_sidebar(self):
        """Reload and display the conversation list."""
        conversations = self.persistence.get_conversations()
        self.sidebar.update_conversations(conversations)
        active_id = self.persistence.get_active_conversation_id()
        if active_id:
            self.sidebar.set_active_conversation(active_id)

    def on_submit(self, text: str):
        """Handle submit button click: send prompt to subprocess manager."""
        if self.subprocess_mgr.is_running():
            return

        conversation_id = self.persistence.get_active_conversation_id()
        is_new = False

        if not conversation_id:
            conversation_id = self.persistence.generate_conversation_id()
            self.persistence.create_conversation_and_add_message(
                conversation_id, "user", text, session_id=None
            )
            self._refresh_sidebar()
            is_new = True

        session_id = self.persistence.get_active_session_id(conversation_id)
        if not is_new:
            self.persistence.add_message(conversation_id, "user", text)
        self.message_log.append_message("user", text)
        self.message_log.scroll_to_bottom()

        self.message_log.create_stderr_region()
        self.subprocess_mgr.submit(text, session_id)
        self.input_bar.set_enabled(False)

    def on_completed(self, stdout: str, session_id):
        """Handle subprocess completion: append assistant message, save session."""
        self.subprocess_mgr.stop()
        self.message_log.remove_stderr_region()
        self.message_log.append_message("assistant", stdout)
        self.message_log.scroll_to_bottom()

        conversation_id = self.persistence.get_active_conversation_id()
        if conversation_id:
            self.persistence.add_message(conversation_id, "assistant", stdout)
            if session_id is not None:
                self.persistence.set_active_session_id(conversation_id, session_id)
            self._refresh_sidebar()

        self.input_bar.set_enabled(True)

    def on_error(self, message: str):
        """Handle subprocess error."""
        self.subprocess_mgr.stop()
        self.message_log.remove_stderr_region()
        self.message_log.append_message("error", message)
        self.message_log.scroll_to_bottom()
        self.input_bar.set_enabled(True)

    def on_finished(self):
        """Universal safety net to re-enable input."""
        self.input_bar.set_enabled(True)

    def _load_conversation_messages(self, conversation_id: str):
        """Load and append all messages for a conversation to the message log."""
        self.message_log.clear()
        messages = self.persistence.get_messages(conversation_id)
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            self.message_log.append_message(role, content)
        self.message_log.scroll_to_bottom()

    def reset_active_state(self):
        """Reset all conversation state to prepare for a new conversation."""
        self.persistence._active_conversation_id = None
        self.message_log.clear()
        self.input_bar._text_edit.clear()
        self.sidebar._active_id = None
        self.sidebar.update_conversations(self.persistence.get_conversations())

    def on_conversation_selected(self, conversation_id: str):
        """Load and display messages for the selected conversation."""
        self.persistence.activate_conversation(conversation_id)
        self._load_conversation_messages(conversation_id)
        self.sidebar.set_active_conversation(conversation_id)

    def _open_settings(self):
        """Open the settings modal dialog."""
        dlg = SettingsDialog(self)
        dlg.exec()
