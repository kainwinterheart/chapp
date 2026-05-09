"""Chat window UI with sidebar, message log, and input bar."""

import re

from markdown_it import MarkdownIt
from PyQt6.QtCore import QEvent, QObject, QPoint, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QKeyEvent, QMouseEvent, QPainter, QFont, QPalette, QPen, QTextCharFormat, QTextCursor
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
    QSlider,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from data_persistence import DataPersistenceManager
from settings import (
    DEFAULT_FONT_SIZES,
    read_font_sizes as _read_font_sizes,
    read_settings as _read_settings,
    save_settings as _save_settings,
)
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
_md = MarkdownIt("gfm-like2")
_md.disable("autolink")
_md.disable("linkify")


def _escape_for_markdown(s: str) -> str:
    """Escape < and > for HTML safety while preserving markdown code spans.

    Backtick-delimited code spans (`...`) are protected so their contents
    aren't mangled by the escaping step.
    """
    parts: list[str] = []
    saved: list[str] = []
    i = 0
    buf: list[str] = []

    while i < len(s):
        if s[i] == "`":
            # Flush accumulated text
            parts.append("".join(buf))
            buf = []
            # Count consecutive backticks
            j = i
            while j < len(s) and s[j] == "`":
                j += 1
            num_ticks = j - i
            # Find matching closing backticks
            k = j
            while k < len(s):
                if s[k] == "`":
                    m = k
                    while m < len(s) and s[m] == "`":
                        m += 1
                    if m - k == num_ticks:
                        # Found match — save the code span
                        saved.append(s[i:m])
                        parts.append(f"\x00{len(saved) - 1}\x00")
                        k = m
                        break
                    else:
                        k = m
                else:
                    k += 1
            else:
                # No match — treat as literal backticks
                buf.append("`" * num_ticks)
                i = j
                continue
            i = k
        else:
            buf.append(s[i])
            i += 1
    parts.append("".join(buf))

    # Join and escape < > (not &, so markdown entities render correctly)
    escaped = "".join(parts).replace("<", "&lt;").replace(">", "&gt;")

    # Restore saved code spans
    for idx, code in enumerate(saved):
        escaped = escaped.replace(f"\x00{idx}\x00", code)

    return escaped


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

class _FontSizeRow(QWidget):
    """A single font-size control row with label + slider + numeric input."""

    value_changed = pyqtSignal(str, int)

    def __init__(self, label: str, key: str, value: int, parent: QWidget | None = None):
        super().__init__(parent)
        self._key = key
        self._label: QLabel | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        lbl = QLabel(label)
        self._label = lbl
        lbl.setStyleSheet("color: #a6adc8; font-size: 12px;")
        row.addWidget(lbl, 0, Qt.AlignmentFlag.AlignLeft)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(6, 24)
        self._slider.setValue(value)
        self._slider.setStyleSheet("""
            QSlider::groove:horizontal {
                background: #313244;
                height: 4px;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #89b4fa;
                width: 12px;
                margin: -4px 0;
                border-radius: 6px;
            }
        """)
        self._slider.valueChanged.connect(self._on_slider)
        row.addWidget(self._slider, 1)

        self._spin = QSpinBox()
        self._spin.setRange(6, 24)
        self._spin.setValue(value)
        self._spin.setStyleSheet("""
            QSpinBox {
                background-color: #313244;
                color: #cdd6f4;
                border: none;
                border-radius: 4px;
                padding: 2px 6px;
                font-size: 12px;
                width: 40px;
            }
        """)
        self._spin.valueChanged.connect(self._on_spin)
        row.addWidget(self._spin, 0)

    def _on_slider(self, val: int) -> None:
        self._spin.blockSignals(True)
        self._spin.setValue(val)
        self._spin.blockSignals(False)
        self.value_changed.emit(self._key, val)

    def _on_spin(self, val: int) -> None:
        self._slider.blockSignals(True)
        self._slider.setValue(val)
        self._slider.blockSignals(False)
        self.value_changed.emit(self._key, val)

    def get_value(self) -> int:
        return self._spin.value()


class SettingsDialog(QDialog, _FramelessMixin):
    """Modal dialog for configuring binary path and font sizes.

    Uses the same frameless style, shadow, resize borders, and title bar as
    the main chat window.
    """

    _title_bar_height = _TITLE_BAR_HEIGHT
    fonts_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowTitle("Settings")

        self.setStyleSheet("QDialog { border: 1px solid #313244; border-radius: 8px; background-color: #1e1e2e; }")

        self._apply_shadow()

        self._tbar = _UnifiedTitleBar(
            self,
            buttons=[CloseButton(self)],
            title="Settings",
            on_double_click='close',
            has_context_menu=False,
        )
        self._tbar.setParent(self)
        self._tbar.raise_()

        # Read current settings
        self._binary_path = _read_settings().get("binary_path", "")
        font_sizes = _read_font_sizes()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, _TITLE_BAR_HEIGHT + 8, 16, 16)
        layout.setSpacing(12)

        # Store references for font styling
        self._styled_labels: list[QLabel] = []
        self._styled_inputs: list[QWidget] = []
        self._styled_buttons: list[QPushButton] = []

        # ── Section header ──────────────────────────────────────────────
        header = QLabel("Appearance")
        layout.addWidget(header)
        self._styled_labels.append(header)

        # ── Font size rows ──────────────────────────────────────────────
        self._font_rows: list[_FontSizeRow] = []
        font_labels = {
            "base": "Base text",
            "stderr": "Stderr / system",
            "title_bar": "Title bar",
            "settings_label": "Settings labels",
            "settings_input": "Settings input",
            "settings_button": "Settings buttons",
        }
        for key, label in font_labels.items():
            row = _FontSizeRow(label, key, font_sizes.get(key, DEFAULT_FONT_SIZES[key]))
            row.value_changed.connect(self._on_font_change)
            layout.addWidget(row)
            self._font_rows.append(row)
            if row._label:
                self._styled_labels.append(row._label)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        sep.setStyleSheet("background-color: #313244;")
        layout.addWidget(sep)

        # ── Binary path section ─────────────────────────────────────────
        path_header = QLabel("Subprocess")
        layout.addWidget(path_header)
        self._styled_labels.append(path_header)

        path_row = QHBoxLayout()
        self._path_edit = QTextEdit()
        self._path_edit.setPlainText(self._binary_path)
        self._path_edit.setMaximumHeight(40)
        path_row.addWidget(self._path_edit, 1)
        self._styled_inputs.append(self._path_edit)

        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self._browse_file)
        path_row.addWidget(browse_btn)
        self._styled_buttons.append(browse_btn)
        layout.addLayout(path_row)

        # ── Buttons ─────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save)

        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)
        self._styled_buttons.extend([cancel_btn, save_btn])

        # Apply font sizes to all widgets
        self._apply_dialog_fonts(font_sizes)
        self._resize_title_bar()

        # Finalize size now that layout is fully built — let Qt measure it
        self.adjustSize()
        self.setMinimumSize(480, self.height())

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

    # ── Font styling ──────────────────────────────────────────────────

    def _apply_dialog_fonts(self, font_sizes: dict) -> None:
        """Apply stored font sizes to all dialog widgets."""
        label_size = font_sizes.get("settings_label", 13)
        input_size = font_sizes.get("settings_input", 13)
        button_size = font_sizes.get("settings_button", 12)

        for lbl in self._styled_labels:
            lbl.setStyleSheet(f"color: #a6adc8; font-size: {label_size}px;")
        for inp in self._styled_inputs:
            inp.setStyleSheet("""
                QTextEdit {
                    background-color: #1e1e2e;
                    color: #cdd6f4;
                    border: 1px solid #313244;
                    border-radius: 4px;
                    padding: 4px 8px;
                    font-family: monospace;
                    font-size: %(input_size)spx;
                }
            """ % {"input_size": input_size})
        for btn in self._styled_buttons:
            if btn.text() == "Save":
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: #89b4fa;
                        color: #1e1e2e;
                        border: none;
                        border-radius: 4px;
                        padding: 6px 14px;
                        font-weight: bold;
                        font-size: %(btn_size)spx;
                    }
                    QPushButton:hover { background-color: #74c7ec; }
                """ % {"btn_size": button_size})
            else:
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: #313244;
                        color: #cdd6f4;
                        border: none;
                        border-radius: 4px;
                        padding: 6px 14px;
                        font-size: %(btn_size)spx;
                    }
                    QPushButton:hover { background-color: #45475a; }
                """ % {"btn_size": button_size})

    def _on_font_change(self, key: str, value: int) -> None:
        """Apply font sizes live as slider changes."""
        font_sizes = self.get_font_sizes()
        self._apply_dialog_fonts(font_sizes)
        # Notify main window to update its own widgets
        self.fonts_changed.emit()

    # ── Actions ─────────────────────────────────────────────────────────

    def _browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Binary", "", "Executable Files (*)"
        )
        if path:
            self._path_edit.setPlainText(path)

    def _save(self) -> None:
        settings = _read_settings()
        path = self._path_edit.toPlainText().strip()
        if path:
            settings["binary_path"] = path
        else:
            settings.pop("binary_path", None)

        font_sizes = {}
        for row in self._font_rows:
            key = row._key  # type: ignore[attr-defined]
            font_sizes[key] = row.get_value()
        settings["font_sizes"] = font_sizes

        _save_settings(settings)
        # Apply fonts to dialog widgets so they reflect saved values
        self._apply_dialog_fonts(font_sizes)
        self.fonts_changed.emit()
        self.accept()

    def get_font_sizes(self) -> dict:
        """Return current font size values from all sliders."""
        sizes = dict(DEFAULT_FONT_SIZES)
        for row in self._font_rows:
            key = row._key  # type: ignore[attr-defined]
            sizes[key] = row.get_value()
        return sizes


# ── Unified title bar ───────────────────────────────────────────────────

class _UnifiedTitleBar(QWidget):
    """Shared title bar for frameless windows (ChatWindow + SettingsDialog).

    Handles: drag-to-move, double-click action (maximise or close),
    optional right-click context menu, and macOS opaque background.
    """

    settings_requested = pyqtSignal()
    markdown_toggled = pyqtSignal(bool)

    def __init__(self, parent, buttons=None, title="Chapp",
                 on_double_click='maximize', has_context_menu=False,
                 font_size: int = 12):
        super().__init__(parent)
        self.setFixedHeight(_TITLE_BAR_HEIGHT)

        # ── macOS fix: ensure the title bar is opaque ───────────────────
        # Palette + explicit stylesheet.  The stylesheet targets this widget
        # directly (not via a QWidget selector) so it always wins.
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor("#1e1e2e"))
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self.setStyleSheet("background-color: #1e1e2e;")

        self._parent_win = parent
        self._on_double_click = on_double_click
        self._buttons = list(buttons or [])
        for btn in self._buttons:
            btn.setParent(self)

        # ── Context menu (main window only) ─────────────────────────────
        self._context_menu = None
        self._markdown_action = None
        if has_context_menu:
            self._context_menu = QMenu(self)
            self._settings_action = QAction("Settings", self)
            self._settings_action.triggered.connect(self.settings_requested.emit)
            self._context_menu.addAction(self._settings_action)

            from settings import read_markdown_enabled
            enabled = read_markdown_enabled()
            self._markdown_action = QAction("Markdown Rendering", self)
            self._markdown_action.setCheckable(True)
            self._context_menu.addAction(self._markdown_action)
            self._markdown_action.setChecked(enabled)
            self._markdown_action.toggled.connect(self.markdown_toggled)

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
            f"color: #a6adc8; font-size: {font_size}px; font-weight: bold; background: transparent;"
        )
        self._font_size = font_size
        layout.addWidget(self.title_label, 1)
        layout.addStretch(1)
        layout.addLayout(btn_layout)

    def apply_font_size(self, size: int) -> None:
        """Update the title label font size."""
        self._font_size = size
        self.title_label.setStyleSheet(
            f"color: #a6adc8; font-size: {size}px; font-weight: bold; background: transparent;"
        )

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
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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

    def __init__(self, parent=None, font_sizes: dict | None = None):
        super().__init__(parent)
        self._font_sizes = font_sizes or {}

        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._stderr_marker_pos = None

        # Stderr line buffer (keeps last 50 lines)
        self._stderr_text = ""
        self._MAX_STDERR_LINES = 50
        self._stderr_flush_timer = QTimer(self)
        self._stderr_flush_timer.setSingleShot(False)
        self._stderr_flush_timer.setInterval(200)
        self._stderr_flush_timer.timeout.connect(self._flush_stderr_buffer)

        self._stderr_fmt = QTextCharFormat()
        self._stderr_fmt.setFontPointSize(float(self._font_sizes.get("stderr", 8)))
        self._stderr_fmt.setForeground(QColor("#f38ba8"))

        # Markdown element styles for the message log
        self._md_stylesheet = """
            h1 { font-size: 1.4em; font-weight: bold; margin-top: 0.5em; margin-bottom: 0.3em; }
            h2 { font-size: 1.25em; font-weight: bold; margin-top: 0.5em; margin-bottom: 0.3em; }
            h3 { font-size: 1.1em; font-weight: bold; margin-top: 0.4em; margin-bottom: 0.2em; }
            p { margin: 0.3em 0; }
            ul, ol { margin: 0.3em 0; padding-left: 1.5em; }
            li { margin: 0.15em 0; }
            code {
                background-color: #313244;
                padding: 1px 4px;
                border-radius: 3px;
            }
            pre {
                background-color: #11111b;
                padding: 8px;
                border-radius: 4px;
                margin: 0.4em 0;
            }
            pre code {
                background-color: transparent;
                padding: 0;
            }
            blockquote {
                border-left: 3px solid #89b4fa;
                margin: 0.4em 0;
                padding-left: 0.8em;
            }
            table { border-collapse: collapse; margin: 0.4em 0; }
            th, td { border: 1px solid #313244; padding: 4px 8px; text-align: left; }
            th { background-color: #313244; }
        """
        self._text_edit.setStyleSheet(self._md_stylesheet)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._text_edit)
        self.setLayout(layout)

    def apply_font_sizes(self, font_sizes: dict) -> None:
        """Reapply font sizes after settings change."""
        self._font_sizes = font_sizes
        self._stderr_fmt.setFontPointSize(float(font_sizes.get("stderr", 8)))

    def append_message(self, role: str, content: str, markdown_enabled: bool = True):
        """Append a styled message block to the log."""
        cursor = self._text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        if role == "user":
            if markdown_enabled:
                trimmed = content.rstrip()
                escaped = _escape_for_markdown(trimmed)
                rendered = _md.render(escaped).rstrip("\n")
                wrapper = (
                    f'<table style="width:auto;border-collapse:collapse;margin:2px 0;" align="right">'
                    f'<tr><td style="background-color:#313244;padding:6px 12px;color:#a6e3a1;">'
                    f'{rendered}'
                    f'</td></tr></table>'
                )
                cursor.insertHtml(wrapper)
            else:
                escaped = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                html_plain = (
                    f'<table style="width:auto;border-collapse:collapse;margin:2px 0;" align="right">'
                    f'<tr><td style="background-color:#313244;padding:6px 12px;">'
                    f'<font color="#a6e3a1">{escaped.replace(chr(10), "<br>")}</font>'
                    f'</td></tr></table>'
                )
                cursor.insertHtml(html_plain)
        elif role == "assistant":
            if markdown_enabled:
                trimmed = content.rstrip()
                escaped = _escape_for_markdown(trimmed)
                rendered = _md.render(escaped).rstrip("\n")
                wrapper = (
                    f'<table style="width:auto;border-collapse:collapse;margin:2px 0;" align="left">'
                    f'<tr><td style="padding:6px 12px;color:#89b4fa;">'
                    f'{rendered}'
                    f'</td></tr></table>'
                )
                cursor.insertHtml(wrapper)
            else:
                fmt = self._format_for_role(role)
                cursor.insertText(content, fmt)
        else:
            # error role — render as plain text (not markdown)
            fmt = self._format_for_role(role)
            cursor.insertText(content, fmt)

        if not markdown_enabled:
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
        """Append chunk to internal line buffer."""
        self._stderr_text += chunk
        self._stderr_flush_timer.start()

    def _flush_stderr_buffer(self):
        """Truncate buffer to last N lines, then replace stderr region."""
        if not self._stderr_text:
            return
        if self._stderr_marker_pos is None:
            return

        # Truncate to last MAX_STDERR_LINES
        lines = self._stderr_text.splitlines(keepends=True)
        if len(lines) > self._MAX_STDERR_LINES:
            self._stderr_text = "".join(lines[-self._MAX_STDERR_LINES:])

        cursor = QTextCursor(self._text_edit.document())
        cursor.setPosition(self._stderr_marker_pos)
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        cursor.insertText(self._stderr_text, self._stderr_fmt)

        scrollbar = self._text_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def remove_stderr_region(self):
        """Remove the stderr block from the end of the document."""
        self._stderr_text = ""
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
        self._stderr_text = ""
        self._stderr_marker_pos = None
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
        self._text_edit.setAcceptRichText(False)
        # Plain text only — no markdown styling
        self._text_edit.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e2e;
                color: #cdd6f4;
                border: none;
                padding: 4px;
                font-family: monospace;
                font-size: 13px;
            }
            QTextEdit:disabled {
                background-color: #11111b;
                color: #585b70;
                border: 1px solid #313244;
                border-radius: 4px;
                padding: 4px;
            }
        """)

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

        # ── macOS fix: opaque window background ─────────────────────────
        # FramelessWindowHint on macOS makes the window translucent at the
        # compositor level.  Set both palette and explicit stylesheet so
        # the background is opaque everywhere.
        win_pal = self.palette()
        win_pal.setColor(QPalette.ColorRole.Window, QColor("#1e1e2e"))
        self.setPalette(win_pal)
        self.setAutoFillBackground(True)
        # Target this specific widget (not the generic QWidget selector) so
        # it beats the app-level stylesheet on macOS frameless windows.
        self.setStyleSheet("ChatWindow { background-color: #1e1e2e; }")

        self.setWindowTitle("Chat Window")
        self.resize(900, 600)

        # Load font sizes from settings
        self._font_sizes = _read_font_sizes()

        # Title bar
        self.custom_title_bar = _UnifiedTitleBar(
            self,
            buttons=[MinimizeButton(self), MaximizeButton(self), CloseButton(self)],
            title="Chapp",
            on_double_click='maximize',
            has_context_menu=True,
            font_size=self._font_sizes.get("title_bar", 12),
        )
        self.custom_title_bar.settings_requested.connect(self._open_settings)
        from settings import read_markdown_enabled, save_markdown_enabled
        self._markdown_enabled = read_markdown_enabled()
        self.custom_title_bar.markdown_toggled.connect(self._on_markdown_toggled)
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
        self.message_log = MessageLogPanel(font_sizes=self._font_sizes)
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
        sep_vertical.setFrameShadow(QFrame.Shadow.Sunken)
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
        sep_horizontal.setFrameShadow(QFrame.Shadow.Sunken)
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

        # Apply font sizes after everything is set up to avoid interfering with rendering
        self._apply_font_sizes(self._font_sizes)

    # ── Font size helpers ─────────────────────────────────────────────

    def _apply_base_font(self, font_sizes: dict) -> None:
        """Set base font on text-displaying widgets in the main window."""
        base = font_sizes.get("base", 13)
        font = QFont("monospace", int(base))
        for widget in self.findChildren(QWidget):
            # Skip dialog descendants — they manage their own fonts
            parent = widget.parent()
            in_dialog = False
            while parent:
                if isinstance(parent, QDialog):
                    in_dialog = True
                    break
                parent = parent.parent()
            if in_dialog:
                continue
            if isinstance(widget, QTextEdit):
                # QTextEdit ignores widget.setFont() for existing content.
                # Must set on the document itself.
                widget.document().setDefaultFont(font)
            else:
                widget.setFont(font)

    def _apply_font_sizes(self, font_sizes: dict) -> None:
        """Apply font sizes to all widgets and update app stylesheet."""
        self._apply_base_font(font_sizes)
        # Update app-level stylesheet so QPushButton (etc.) inherit correctly
        base = font_sizes.get("base", 13)
        QApplication.instance().setStyleSheet(
            STYLESHEET.replace("font-size: 13px;", f"font-size: {base}px;")
        )
        # Update title bar
        self.custom_title_bar.apply_font_size(font_sizes.get("title_bar", 12))
        # Update message log stderr font
        self.message_log.apply_font_sizes(font_sizes)

    # ── Layout helpers ──────────────────────────────────────────────────

    def _resize_title_bar(self):
        """Keep the title bar wide enough to cover the window."""
        self.custom_title_bar.resize(self.width(), self.custom_title_bar.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_title_bar()

    def paintEvent(self, event):
        """Force opaque background on macOS frameless windows."""
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1e1e2e"))
        painter.end()
        super().paintEvent(event)

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
        self.message_log.append_message("user", text, self._markdown_enabled)
        self.message_log.scroll_to_bottom()

        self.message_log.create_stderr_region()
        self.subprocess_mgr.submit(text, session_id)
        self.input_bar.set_enabled(False)

    def on_completed(self, stdout: str, session_id):
        """Handle subprocess completion: append assistant message, save session."""
        self.subprocess_mgr.stop()
        self.message_log.remove_stderr_region()
        self.message_log.append_message("assistant", stdout, self._markdown_enabled)
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
            self.message_log.append_message(role, content, self._markdown_enabled)
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
        dlg.fonts_changed.connect(lambda: self._apply_font_sizes(dlg.get_font_sizes()))
        dlg.exec()

    def _on_markdown_toggled(self, checked: bool):
        """Handle markdown rendering toggle from context menu."""
        from settings import save_markdown_enabled
        save_markdown_enabled(checked)
        self._markdown_enabled = checked
        # Reload current conversation to re-render messages
        conv_id = self.persistence.get_active_conversation_id()
        if conv_id:
            self._load_conversation_messages(conv_id)
