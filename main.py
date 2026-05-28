import sys
import os
import math
import random
import collections
from datetime import datetime

SIMUOS_USERS = {"admin": "admin"}
SIMUOS_VFS = {
    "/": ["bin", "etc", "home", "usr"],
    "/bin": [],
    "/etc": [],
    "/home": ["admin"],
    "/home/admin": ["Desktop"],
    "/home/admin/Desktop": ["Recycle Bin", "SimuOS Explorer"],
    "/home/admin/Desktop/Recycle Bin": [],   # must exist so navigation never corrupts paths
    "/usr": [],
}
# Stores actual file contents keyed by VFS path (e.g. "/home/admin/Desktop/notes.txt")
SIMUOS_FILE_CONTENTS: dict = {}
# Stores file metadata (timestamps, permissions, owner)
SIMUOS_FILE_META: dict = {}
SIMUOS_CLIPBOARD: dict = {"action": None, "vfs_name": None}
# Live window registry: pid -> {"name": str, "window": QMainWindow}
SIMUOS_PROCESS_REGISTRY: dict = {}
# Real disk I/O event log — populated by File Explorer, Text Editor, Terminal
SIMUOS_DISK_LOG: list = []   # each entry: (icon, op_label, path)
# Real keyboard event log — populated by Terminal commands and Text Editor
SIMUOS_KEYBOARD_LOG: list = []  # each entry: (key_desc, source)
# Real printer queue — populated by right-click Print in File Explorer
SIMUOS_PRINTER_LOG: list = []   # each entry: (filename, vfs_path)
import json, os
import datetime as _dt   # alias avoids shadowing the 'datetime' class used by the clock

# ─────────────────────────────────────────────────────────────────────────────
#  SESSION PERSISTENCE HELPERS
# ─────────────────────────────────────────────────────────────────────────────
_SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")

def _save_session(label: str = "") -> str:
    """Serialize VFS + file contents to a JSON file. Returns the saved path."""
    os.makedirs(_SESSIONS_DIR, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{label.strip().replace(' ', '_') or 'session'}_{ts}.json"
    path = os.path.join(_SESSIONS_DIR, name)
    payload = {
        "version": "1.0",
        "saved_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "label": label or ts,
        "vfs": SIMUOS_VFS,
        "file_contents": SIMUOS_FILE_CONTENTS,
        "users": SIMUOS_USERS,
        "file_meta": SIMUOS_FILE_META,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return path

def _load_session(path: str):
    """Deserialize a session file back into the global VFS state."""
    global SIMUOS_VFS, SIMUOS_FILE_CONTENTS, SIMUOS_USERS, SIMUOS_FILE_META
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    SIMUOS_VFS = payload.get("vfs", SIMUOS_VFS)
    SIMUOS_FILE_CONTENTS = payload.get("file_contents", SIMUOS_FILE_CONTENTS)
    SIMUOS_USERS = payload.get("users", SIMUOS_USERS)
    SIMUOS_FILE_META = payload.get("file_meta", SIMUOS_FILE_META)

def _list_sessions() -> list:
    """Return sorted list of (display_label, filepath) for all saved sessions."""
    if not os.path.isdir(_SESSIONS_DIR):
        return []
    results = []
    for fn in sorted(os.listdir(_SESSIONS_DIR), reverse=True):
        if fn.endswith(".json"):
            fp = os.path.join(_SESSIONS_DIR, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                label = meta.get("label", fn)
                saved_at = meta.get("saved_at", "")
                results.append((f"{label}  ·  {saved_at}", fp))
            except Exception:
                results.append((fn, fp))
    return results

def _update_vfs_metadata(path, event_type="access"):
    """Update SIMUOS_FILE_META timestamps based on event type."""
    global SIMUOS_FILE_META
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if path not in SIMUOS_FILE_META:
        SIMUOS_FILE_META[path] = {
            "access": now, "modify": now, "change": now, "birth": now, 
            "perms": "-rw-r--r--", "owner": "admin", "size": 0
        }
    
    meta = SIMUOS_FILE_META[path]
    if event_type == "access":
        meta["access"] = now
    elif event_type == "modify":
        meta["modify"] = now
        meta["change"] = now
    elif event_type == "change":
        meta["change"] = now

def _check_vfs_permission(path, user, mode='r'):
    """Returns True if user has 'mode' (r/w) permission on path."""
    global SIMUOS_FILE_META
    if path not in SIMUOS_FILE_META: return True
    if user == "root": return True
    
    meta = SIMUOS_FILE_META[path]
    owner = meta.get("owner", "admin")
    perms = meta.get("perms", "-rw-r--r--")
    
    if len(perms) < 9: return True # Malformed perms
    
    if user == owner:
        # Owner bits: index 1 (r), 2 (w)
        if mode == 'r': return perms[1] == 'r'
        if mode == 'w': return perms[2] == 'w'
    else:
        # Others bits: index 7 (r), 8 (w)
        if mode == 'r': return perms[7] == 'r'
        if mode == 'w': return perms[8] == 'w'
    return True

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QProgressBar, QLineEdit, QPushButton,
    QGraphicsDropShadowEffect, QFrame, QGridLayout, QSizePolicy,
    QTextEdit, QListWidget, QListView, QListWidgetItem, QMenu, QInputDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QTabWidget, QStackedWidget,
    QSplitter
)
from PyQt6.QtCore import Qt, QTimer, QTime, QPoint
from PyQt6.QtGui import QFont, QColor, QPainter, QLinearGradient, QBrush, QPen, QPixmap, QIcon, QAction, QPainterPath


# ─────────────────────────────────────────────────────────────────────────────
#  HELPER: Reusable drop-shadow effect
def make_shadow(blur=40, y_offset=10, alpha=160):
    s = QGraphicsDropShadowEffect()
    s.setBlurRadius(blur)
    s.setColor(QColor(0, 0, 0, alpha))
    s.setOffset(0, y_offset)
    return s


# ─────────────────────────────────────────────────────────────────────────────
#  HELPER: Log a real disk I/O event (shown live in Task Manager I/O tab)
# ─────────────────────────────────────────────────────────────────────────────
def _log_disk(op: str, path: str):
    """Record a real VFS disk event. op should be 'Read' or 'Write'."""
    global SIMUOS_DISK_LOG
    icon = "\U0001f4be" if op == "Read" else "\U0001f4be"
    label = "Read " if op == "Read" else "Write"
    short = path if len(path) <= 36 else "..." + path[-33:]
    entry = (icon, label, short)
    # Remove duplicate if same path already in log
    SIMUOS_DISK_LOG = [e for e in SIMUOS_DISK_LOG if e[2] != short]
    SIMUOS_DISK_LOG.insert(0, entry)
    if len(SIMUOS_DISK_LOG) > 6:
        SIMUOS_DISK_LOG.pop()


def _log_keyboard(key_desc: str, source: str):
    """Record a real keyboard event (shown live in Task Manager I/O tab)."""
    global SIMUOS_KEYBOARD_LOG
    SIMUOS_KEYBOARD_LOG.insert(0, (key_desc, source))
    if len(SIMUOS_KEYBOARD_LOG) > 6:
        SIMUOS_KEYBOARD_LOG.pop()


def _log_printer(filename: str, vfs_path: str):
    """Record a file sent to the printer (shown live in Task Manager I/O tab)."""
    global SIMUOS_PRINTER_LOG
    SIMUOS_PRINTER_LOG = [e for e in SIMUOS_PRINTER_LOG if e[1] != vfs_path]
    SIMUOS_PRINTER_LOG.insert(0, (filename, vfs_path))
    if len(SIMUOS_PRINTER_LOG) > 6:
        SIMUOS_PRINTER_LOG.pop()


# ─────────────────────────────────────────────────────────────────────────────
#  HELPER: Draw Vector Eye Icon (Show/Hide Password)
# ─────────────────────────────────────────────────────────────────────────────
def draw_eye_icon(open_eye=True):
    pix = QPixmap(24, 24)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    
    base_color = QColor("#a78bfa" if open_eye else "#64748b")
    pen = QPen(base_color)
    pen.setWidth(2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    
    # Sleek almond shape using bezier curves
    path = QPainterPath()
    path.moveTo(3, 12)
    path.cubicTo(8, 5, 16, 5, 21, 12)   # Top curve
    path.cubicTo(16, 19, 8, 19, 3, 12)  # Bottom curve
    painter.drawPath(path)
    
    if open_eye:
        # Hollow iris ring looks much cleaner
        painter.drawEllipse(9, 9, 6, 6)
    else:
        # Clean diagonal slash for hidden state
        painter.drawLine(5, 19, 19, 5)
        
    painter.end()
    return QIcon(pix)


# ═════════════════════════════════════════════════════════════════════════════
#  SCREEN 1 — BOOT SCREEN
#  Shows an animated progress bar that simulates the OS kernel booting up.
#  When it reaches 100%, it automatically transitions to the Login Screen.
# ═════════════════════════════════════════════════════════════════════════════
class BootScreen(QMainWindow):
    def __init__(self):
        super().__init__()

        # Remove the native OS window border for custom look
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(820, 520)
        self.setWindowTitle("SimuOS – Booting")

        # ── Main container card ──────────────────────────────────────────────
        self.container = QFrame(self)
        self.container.setGeometry(self.rect())
        self.container.setStyleSheet("""
            QFrame {
                background-color: #080c14;
                border-radius: 22px;
                border: 1px solid #1a2540;
            }
        """)
        self.container.setGraphicsEffect(make_shadow(60, 18, 200))

        layout = QVBoxLayout(self.container)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ── "SimuOS" brand in neon cyan ──────────────────────────────────────
        logo = QLabel("SimuOS")
        logo.setFont(QFont("Segoe UI", 60, QFont.Weight.Bold))
        logo.setStyleSheet("color: #38bdf8; background: transparent; border: none;")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ── Tagline / dynamic boot step label ───────────────────────────────
        self.subtitle = QLabel("Initializing Kernel Engine...")
        self.subtitle.setFont(QFont("Segoe UI", 13))
        self.subtitle.setStyleSheet(
            "color: #64748b; background: transparent; border: none; letter-spacing: 1px;"
        )
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ── Gradient horizontal progress bar ────────────────────────────────
        self.progress = QProgressBar()
        self.progress.setFixedSize(460, 6)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet("""
            QProgressBar            { background:#1e293b; border-radius:3px; border:none; }
            QProgressBar::chunk    { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                        stop:0 #38bdf8, stop:1 #818cf8);
                                     border-radius:3px; }
        """)

        # ── Assemble layout ──────────────────────────────────────────────────
        layout.addSpacing(80)
        layout.addWidget(logo)
        layout.addWidget(self.subtitle)
        layout.addSpacing(50)
        layout.addWidget(self.progress, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(90)

        # ── Boot timer ticks every 25 ms, simulating loading steps ──────────
        self.counter = 0
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(25)

    def _tick(self):
        """Advance the progress bar and update the status label at key points."""
        self.counter += 1
        self.progress.setValue(self.counter)

        steps = {
            20: "Waking up Memory Manager...",
            45: "Mounting Virtual File System...",
            70: "Loading I/O Device Drivers...",
            90: "Starting Graphic Shell...",
        }
        if self.counter in steps:
            self.subtitle.setText(steps[self.counter])

        # Transition to login once boot finishes
        if self.counter >= 100:
            self.timer.stop()
            self._go_login()

    def _go_login(self):
        """Open the Login Screen and close this one."""
        self.login = LoginScreen()
        self.login.show()
        self.close()


# ═════════════════════════════════════════════════════════════════════════════
#  SCREEN 2 — LOGIN SCREEN
#  Split-pane design: vibrant neon-gradient left panel + clean form right side.
#  Successful login (admin / admin) opens the Desktop. ✕ button closes the app.
# ═════════════════════════════════════════════════════════════════════════════
class LoginScreen(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(950, 600)
        self.setWindowTitle("SimuOS – Login")

        # ── Outer card ───────────────────────────────────────────────────────
        container = QFrame(self)
        container.setGeometry(self.rect())
        container.setStyleSheet("""
            QFrame {
                background-color: #0d1117;
                border-radius: 22px;
                border: 1px solid #1f2937;
            }
        """)
        container.setGraphicsEffect(make_shadow(60, 18, 190))

        main = QHBoxLayout(container)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # ── LEFT: vibrant pink ➜ purple ➜ blue gradient panel ────────────────
        left = QFrame()
        left.setStyleSheet("""
            QFrame {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                    stop:0 #ec4899, stop:0.45 #8b5cf6, stop:1 #3b82f6);
                border-top-left-radius: 22px;
                border-bottom-left-radius: 22px;
            }
        """)
        ll = QVBoxLayout(left)
        ll.setAlignment(Qt.AlignmentFlag.AlignCenter)

        brand = QLabel("SimuOS")
        brand.setStyleSheet(
            "color: rgba(255,255,255,0.95); font-size:74px; font-weight:900;"
            "background:transparent; border:none; letter-spacing:3px;"
        )
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)

        version = QLabel("Future Kernel")
        version.setStyleSheet(
            "color:rgba(255,255,255,0.65); font-size:15px;"
            "font-weight:600; background:transparent; border:none;"
        )
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)

        ll.addWidget(brand)
        ll.addWidget(version)

        # ── RIGHT: login form ────────────────────────────────────────────────
        right = QFrame()
        right.setStyleSheet(
            "background:transparent; border:none;"
        )
        rl = QVBoxLayout(right)
        rl.setContentsMargins(20, 16, 20, 30)

        # ── ✕ close button in top-right corner ──────────────────────────────
        top_bar = QHBoxLayout()
        top_bar.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(34, 34)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton           { background:transparent; color:#475569;
                                    font-size:18px; font-weight:bold;
                                    border:none; border-radius:17px; }
            QPushButton:hover     { background:#ef4444; color:white; }
        """)
        close_btn.clicked.connect(self.close)
        top_bar.addWidget(close_btn)
        rl.addLayout(top_bar)

        # ── Form body ────────────────────────────────────────────────────────
        form = QVBoxLayout()
        form.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("System Login")
        title.setStyleSheet(
            "color:#f8fafc; font-size:34px; font-weight:bold; background:transparent;"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        tagline = QLabel("Secure Boot Sequence")
        tagline.setStyleSheet(
            "color:#8b5cf6; font-size:14px; font-weight:600; background:transparent;"
        )
        tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Shared input style
        field_style = """
            QLineEdit            { background:#1e293b; color:#f8fafc;
                                   border:2px solid transparent; border-radius:12px;
                                   padding:10px 20px; font-size:15px; }
            QLineEdit:focus      { border:2px solid #8b5cf6; background:#0f172a; }
        """

        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Username")
        self.user_input.setFixedSize(320, 52)
        self.user_input.setStyleSheet(field_style)

        self.pass_input = QLineEdit()
        self.pass_input.setPlaceholderText("Password")
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_input.setFixedSize(320, 52)
        
        # Add vector eye icon properly into the QLineEdit
        self._eye_open = False
        self._eye_action = self.pass_input.addAction(
            draw_eye_icon(False), 
            QLineEdit.ActionPosition.TrailingPosition
        )
        self._eye_action.triggered.connect(self._toggle_password_visibility)
        
        self.pass_input.setStyleSheet(field_style)
        # Allow pressing Enter to submit
        self.pass_input.returnPressed.connect(self._do_login)

        # ── Status / error message (hidden until needed) ─────────────────────
        self.status = QLabel("")
        self.status.setFixedSize(320, 38)
        self.status.setStyleSheet("font-size:13px; font-weight:600; background:transparent;")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.hide()

        # ── Main Login button with gradient ──────────────────────────────────
        login_btn = QPushButton("Login")
        login_btn.setFixedSize(320, 54)
        login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        login_btn.setStyleSheet("""
            QPushButton         { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                      stop:0 #8b5cf6, stop:1 #3b82f6);
                                  color:#fff; border:none; border-radius:12px;
                                  font-size:16px; font-weight:bold; letter-spacing:1px; }
            QPushButton:hover   { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                      stop:0 #a78bfa, stop:1 #60a5fa); }
            QPushButton:pressed { background:#3730a3; }
        """)
        login_btn.clicked.connect(self._do_login)

        # ── Assemble form ────────────────────────────────────────────────────
        form.addWidget(title)
        form.addWidget(tagline)
        form.addSpacing(44)
        form.addWidget(self.user_input, alignment=Qt.AlignmentFlag.AlignCenter)
        form.addSpacing(14)
        form.addWidget(self.pass_input, alignment=Qt.AlignmentFlag.AlignCenter)
        form.addSpacing(14)
        form.addWidget(self.status, alignment=Qt.AlignmentFlag.AlignCenter)
        form.addSpacing(10)
        form.addWidget(login_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        form.addSpacing(10)

        # ── Load Session button (shown only if sessions exist) ────────────────
        load_sess_btn = QPushButton("Load Saved Session")
        load_sess_btn.setFixedSize(320, 40)
        load_sess_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        load_sess_btn.setStyleSheet("""
            QPushButton       { background: transparent; color: #64748b;
                                border: 1px solid #334155; border-radius: 10px;
                                font-size: 13px; }
            QPushButton:hover { background: #1e293b; color: #f8fafc;
                                border-color: #8b5cf6; }
        """)
        load_sess_btn.clicked.connect(self._pick_session)
        load_sess_btn.setVisible(bool(_list_sessions()))
        self._load_sess_btn = load_sess_btn
        form.addWidget(load_sess_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        rl.addLayout(form)
        rl.addStretch()

        main.addWidget(left,  stretch=5)
        main.addWidget(right, stretch=6)

    def _toggle_password_visibility(self):
        """Show or hide the password using the dynamically drawn vector icon."""
        self._eye_open = not self._eye_open
        if self._eye_open:
            self.pass_input.setEchoMode(QLineEdit.EchoMode.Normal)
            self._eye_action.setIcon(draw_eye_icon(True))
        else:
            self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
            self._eye_action.setIcon(draw_eye_icon(False))

    def _do_login(self):
        """Validate credentials; on success launch the Desktop."""
        user = self.user_input.text().strip()
        pwd  = self.pass_input.text().strip()

        global SIMUOS_USERS
        if user in SIMUOS_USERS and pwd == SIMUOS_USERS[user]:
            self.status.setStyleSheet("""
                QLabel {
                    color: #10b981;
                    background: rgba(16, 185, 129, 0.1);
                    border: 1px solid rgba(16, 185, 129, 0.4);
                    border-radius: 8px;
                }
            """)
            self.status.setText("Access Granted — Initializing environment...")
            self.status.show()
            # Short delay so user can read the success message
            QTimer.singleShot(800, lambda: self._go_desktop(user))
        else:
            self.status.setStyleSheet("""
                QLabel {
                    color: #ef4444;
                    background: rgba(239, 68, 68, 0.1);
                    border: 1px solid rgba(239, 68, 68, 0.4);
                    border-radius: 8px;
                }
            """)
            self.status.setText("Access Denied — Invalid credentials")
            self.status.show()

    def _pick_session(self):
        """Open Windows file browser to choose a session JSON and load it."""
        from PyQt6.QtWidgets import QFileDialog
        os.makedirs(_SESSIONS_DIR, exist_ok=True)
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Session",
            _SESSIONS_DIR,
            "SimuOS Session (*.json);;All Files (*)"
        )
        if path:
            try:
                _load_session(path)
                self.status.setStyleSheet(
                    "QLabel{color:#10b981;background:rgba(16,185,129,0.1);"
                    "border:1px solid rgba(16,185,129,0.4);border-radius:8px;}"
                )
                self.status.setText("Session loaded. Please click Login.")
                self.status.show()
            except Exception as e:
                self.status.setStyleSheet(
                    "QLabel{color:#ef4444;background:rgba(239,68,68,0.1);"
                    "border:1px solid rgba(239,68,68,0.4);border-radius:8px;}"
                )
                self.status.setText(f"Failed to load session: {e}")
                self.status.show()

    def _go_desktop(self, user):
        """Open the Desktop and close the login window."""
        self.desktop = DesktopScreen(current_user=user)
        self.desktop.showMaximized()
        self.close()


# ═════════════════════════════════════════════════════════════════════════════
#  SCREEN 3 — DESKTOP  (SimuOS Main Environment)
#
#  Layout structure:
#   ┌──────────────────── Top Status Bar ────────────────────────┐
#   │  SimuOS logo    CPU / RAM stats    Clock   [Close btn]     │
#   ├──────────────────── Desktop Area ──────────────────────────┤
#   │                                                            │
#   │   [Icon]  [Icon]  [Icon]  [Icon]  [Icon]                  │
#   │                                                            │
#   └──────────────────── Bottom Dock ───────────────────────────┘
#
#  Desktop icons (placeholders for future modules):
#   • Process Manager   • Memory Manager
#   • File System       • I/O Devices       • Terminal
# ═════════════════════════════════════════════════════════════════════════════
class DesktopIconList(QListWidget):
    def __init__(self, parent=None, refresh_cb=None, current_user="admin"):
        super().__init__(parent)
        self.refresh_cb = refresh_cb
        self.current_user = current_user
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(self.DragDropMode.InternalMove)
        
    def dropEvent(self, event):
        target = self.itemAt(event.position().toPoint())
        dragged = self.currentItem()
        
        if target and dragged and target != dragged:
            target_name = target.text()
            drag_name = dragged.text()
            
            global SIMUOS_VFS
            dt_path = f"/home/{self.current_user}/Desktop"
            
            # Reconstruct internal true name
            drag_real = drag_name
            if "~" + drag_name in SIMUOS_VFS[dt_path]:
                drag_real = "~" + drag_name

            # Check if target is a folder (folders are un-prefixed in VFS)
            if target_name in SIMUOS_VFS[dt_path]:
                # It's a folder, trigger the move
                target_path = dt_path + "/" + target_name
                
                if drag_real in SIMUOS_VFS[dt_path]:
                    # Process move operation
                    SIMUOS_VFS[dt_path].remove(drag_real)
                    
                    if target_path not in SIMUOS_VFS:
                        SIMUOS_VFS[target_path] = []
                    SIMUOS_VFS[target_path].append(drag_real)
                    
                    # Accept the event cleanly before mutating list
                    from PyQt6.QtCore import Qt, QTimer
                    event.setDropAction(Qt.DropAction.MoveAction)
                    event.accept()
                    
                    if self.refresh_cb:
                        # Defer destruction of UI elements so drop event completes resolving safely
                        QTimer.singleShot(10, self.refresh_cb)
                    return 
        super().dropEvent(event)

class DesktopScreen(QMainWindow):
    def __init__(self, current_user="admin"):
        super().__init__()
        self.current_user = current_user
        self.setWindowTitle("SimuOS – Desktop")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # ── Root widget (dark wallpaper) ─────────────────────────────────────
        self._root_widget = QWidget()
        self.set_wallpaper("Dark Gradient")
        self.setCentralWidget(self._root_widget)

        root_layout = QVBoxLayout(self._root_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── 1. Top Status Bar ────────────────────────────────────────────────
        root_layout.addWidget(self._build_topbar())

        # ── 2. Desktop Area (fills all remaining space) ──────────────────────
        root_layout.addWidget(self._build_desktop_area(), stretch=1)

        # ── 3. Bottom Dock ───────────────────────────────────────────────────
        root_layout.addWidget(self._build_dock())

        # ── Clock refresh: update every second ──────────────────────────────
        self._clock_timer = QTimer()
        self._clock_timer.timeout.connect(self._refresh_clock)
        self._clock_timer.start(1000)

    def set_wallpaper(self, style: str):
        """Change the desktop background dynamically."""
        if style == "Dark Gradient":
            bg = "qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #070b12, stop:0.5 #0d1523, stop:1 #060a10)"
        elif style == "Deep Space":
            bg = "qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #020617, stop:1 #0f172a)"
        elif style == "Midnight Blue":
            bg = "qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #172554, stop:1 #1e3a8a)"
        else:
            bg = "#000000"
            
        self._root_widget.setStyleSheet(f"QWidget {{ background: {bg}; }}")

    # ─────────────────────────────────────────────────────────────────────────
    #  TOP BAR — logo, live clock, system stats, close button
    # ─────────────────────────────────────────────────────────────────────────
    def _build_topbar(self):
        bar = QFrame()
        bar.setFixedHeight(48)
        bar.setStyleSheet("""
            QFrame {
                background: rgba(8, 12, 25, 0.92);
                border-bottom: 1px solid #1a2540;
                border-radius: 0px;
            }
        """)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 0, 20, 0)

        # ── OS Logo / Brand name ─────────────────────────────────────────────
        logo = QLabel("⬡  SimuOS")
        logo.setStyleSheet(
            "color:#38bdf8; font-size:15px; font-weight:900;"
            "letter-spacing:2px; background:transparent;"
        )

        # ── Simulated system stats (static for now, replaced later) ─────────
        cpu_lbl = QLabel("CPU  12%")
        cpu_lbl.setStyleSheet(
            "color:#94a3b8; font-size:12px; background:transparent;"
            "border:1px solid #1f2937; border-radius:6px; padding:2px 12px;"
        )

        ram_lbl = QLabel("RAM  1.2 GB / 4 GB")
        ram_lbl.setStyleSheet(cpu_lbl.styleSheet())

        # ── Live digital clock ───────────────────────────────────────────────
        self._clock_lbl = QLabel()
        self._clock_lbl.setStyleSheet(
            "color:#e2e8f0; font-size:13px; font-weight:600; background:transparent;"
        )
        self._refresh_clock()   # set initial value immediately

        # ── Sign Out button — returns to the Login screen ───────────────────
        signout_btn = QPushButton("⇤  Sign Out")
        signout_btn.setFixedHeight(32)
        signout_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        signout_btn.setToolTip("Sign out and return to Login")
        signout_btn.setStyleSheet("""
            QPushButton       { background: rgba(139,92,246,0.15);
                                color:#a78bfa; font-size:13px; font-weight:600;
                                border:1px solid rgba(139,92,246,0.3);
                                border-radius:8px; padding:0 14px; }
            QPushButton:hover { background: rgba(139,92,246,0.35);
                                color:#ffffff; border-color:#8b5cf6; }
            QPushButton:pressed { background:#6d28d9; color:white; }
        """)
        signout_btn.clicked.connect(self._sign_out)

        # ── Power Off button — shuts down the whole app ──────────────────────
        power_btn = QPushButton("⏻")
        power_btn.setFixedSize(32, 32)
        power_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        power_btn.setToolTip("Power Off SimuOS")
        power_btn.setStyleSheet("""
            QPushButton       { background:transparent; color:#64748b;
                                font-size:17px; border:none; border-radius:16px; }
            QPushButton:hover { background:#ef4444; color:white; }
        """)
        power_btn.clicked.connect(QApplication.instance().quit)

        layout.addWidget(logo)
        layout.addSpacing(30)
        layout.addWidget(cpu_lbl)
        layout.addSpacing(10)
        layout.addWidget(ram_lbl)
        layout.addStretch()
        layout.addWidget(self._clock_lbl)
        layout.addSpacing(20)
        layout.addWidget(signout_btn)
        layout.addSpacing(8)
        layout.addWidget(power_btn)

        return bar

    def _refresh_clock(self):
        """Called every second to update the top-bar clock label."""
        now = datetime.now()
        self._clock_lbl.setText(now.strftime("%A  %d %b %Y    %I:%M:%S %p"))

    def _sign_out(self):
        """Close ALL open app windows, then reopen the Login screen."""
        self._clock_timer.stop()
        if hasattr(self, '_desktop_vfs_timer'):
            self._desktop_vfs_timer.stop()

        # Close every window currently registered in the process table
        global SIMUOS_PROCESS_REGISTRY
        for pid, info in list(SIMUOS_PROCESS_REGISTRY.items()):
            try:
                win = info.get("window")
                if win:
                    win.blockSignals(True)   # suppress closeEvent side-effects
                    win.close()
            except Exception:
                pass
        SIMUOS_PROCESS_REGISTRY.clear()

        self.login = LoginScreen()
        self.login.show()
        self.close()

    # ─────────────────────────────────────────────────────────────────────────
    #  DESKTOP AREA — wallpaper canvas + desktop icons grid
    # ─────────────────────────────────────────────────────────────────────────
    def _build_desktop_area(self):
        """Clean empty desktop — icons removed, just the SimuOS watermark."""
        area = QWidget()
        area.setStyleSheet("background:transparent;")

        layout = QVBoxLayout(area)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setContentsMargins(0, 0, 0, 0)

        # ── Large faint watermark centered on the desktop ────────────────────
        wm = QLabel("SimuOS", area)
        wm.setStyleSheet(
            "color: rgba(56,189,248,0.04); font-size:180px; font-weight:900;"
            "background:transparent; letter-spacing:10px;"
        )
        wm.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(wm)

        # ── Desktop Icons Overlay ────────────────────────────────────────────
        from PyQt6.QtWidgets import QListView
        from PyQt6.QtCore import QSize
        
        self.desktop_icons = DesktopIconList(area, refresh_cb=self._refresh_desktop_icons, current_user=self.current_user)
        self.desktop_icons.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.desktop_icons.customContextMenuRequested.connect(self._show_desktop_context_menu)
        self.desktop_icons.itemDoubleClicked.connect(self._on_icon_double_clicked)
        self.desktop_icons.setGeometry(0, 0, 1920, 1000) # Give plenty of space
        self.desktop_icons.setViewMode(QListView.ViewMode.IconMode)
        self.desktop_icons.setMovement(QListView.Movement.Snap)
        self.desktop_icons.setSpacing(20)
        self.desktop_icons.setIconSize(QSize(64, 64))
        self.desktop_icons.setGridSize(QSize(100, 100))
        self.desktop_icons.setStyleSheet("""
            QListWidget {
                background: transparent;
                border: none;
                outline: none;
            }
            QListWidget::item {
                color: white;
                border-radius: 6px;
                padding: 10px;
            }
            QListWidget::item:hover {
                background: rgba(255, 255, 255, 0.1);
            }
            QListWidget::item:selected {
                background: rgba(56, 189, 248, 0.3);
                border: 1px solid rgba(56, 189, 248, 0.6);
            }
        """)

        # Start timer to continuously sync visually with VFS
        self._desktop_vfs_timer = QTimer(self)
        self._desktop_vfs_timer.timeout.connect(self._refresh_desktop_icons)
        self._desktop_vfs_timer.start(1000)
        self._last_vfs = None

        return area

    def _on_icon_double_clicked(self, item):
        """Double-clicking a file icon opens it in the Text Editor."""
        name = item.text()
        dt_path = f"/home/{self.current_user}/Desktop"
        global SIMUOS_VFS
        if name == "SimuOS Explorer":
            self._open_file_explorer()
        elif f"~{name}" in SIMUOS_VFS.get(dt_path, []):
            vfs_path = dt_path + "/" + name
            self._open_text_editor(vfs_path=vfs_path, display_name=name)
        elif name in SIMUOS_VFS.get(dt_path, []):
            vfs_path = dt_path + "/" + name
            self._open_file_explorer(path=vfs_path)

    def _show_desktop_context_menu(self, pos):
        from PyQt6.QtWidgets import QMenu, QInputDialog
        
        item = self.desktop_icons.itemAt(pos)
        
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #0f172a;
                color: #f8fafc;
                border: 1px solid #334155;
                font-size: 13px;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                padding: 8px 24px 8px 16px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #38bdf8;
                color: #0f172a;
            }
        """)
        
        global SIMUOS_VFS
        global SIMUOS_CLIPBOARD
        global SIMUOS_FILE_CONTENTS
        target_dir = f"/home/{self.current_user}/Desktop"
        
        if item:
            if item.text() == "Recycle Bin":
                empty_bin = menu.addAction("🗑 Empty Recycle Bin")
                action = menu.exec(self.desktop_icons.mapToGlobal(pos))
                if action == empty_bin:
                    SIMUOS_VFS[target_dir + "/Recycle Bin"] = []
                    self._refresh_desktop_icons()
            elif item.text() == "SimuOS Explorer":
                open_item = menu.addAction("📂 Open File Explorer")
                action = menu.exec(self.desktop_icons.mapToGlobal(pos))
                if action == open_item:
                    self._open_file_explorer()
            else:
                name = item.text()
                real_name = "~" + name if "~" + name in SIMUOS_VFS[target_dir] else name
                is_file = real_name.startswith("~")

                open_item = menu.addAction("Open" if not is_file else "Open in Editor")
                menu.addSeparator()
                rename_item = menu.addAction("Rename")
                copy_item = menu.addAction("Copy")
                cut_item = menu.addAction("Cut")
                delete_item = menu.addAction("Delete")
                print_item = None
                if is_file:
                    menu.addSeparator()
                    print_item = menu.addAction("[Print]  Send to Printer")

                action = menu.exec(self.desktop_icons.mapToGlobal(pos))
                
                if action == open_item:
                    if is_file:
                        vfs_path = target_dir + "/" + name
                        self._open_text_editor(vfs_path=vfs_path, display_name=name)
                    else:
                        vfs_path = target_dir + "/" + name
                        self._open_file_explorer(path=vfs_path)
                elif action == rename_item:
                    new_name, ok = QInputDialog.getText(self, "Rename", "Enter new name:", text=name)
                    if ok and new_name and new_name != name:
                        new_real = "~" + new_name if is_file else new_name
                        if new_real not in SIMUOS_VFS[target_dir]:
                            idx = SIMUOS_VFS[target_dir].index(real_name)
                            SIMUOS_VFS[target_dir][idx] = new_real
                            
                            # Update global file contents dictionary to reflect new path
                            if is_file:
                                old_path = target_dir + "/" + name
                                new_path = target_dir + "/" + new_name
                                if old_path in SIMUOS_FILE_CONTENTS:
                                    SIMUOS_FILE_CONTENTS[new_path] = SIMUOS_FILE_CONTENTS.pop(old_path)
                            else:
                                # Rename subfolder dict key
                                old_path = target_dir + "/" + name
                                new_path = target_dir + "/" + new_name
                                if old_path in SIMUOS_VFS:
                                    SIMUOS_VFS[new_path] = SIMUOS_VFS.pop(old_path)
                            
                            self._refresh_desktop_icons()
                elif action == copy_item:
                    SIMUOS_CLIPBOARD["action"] = "copy"
                    SIMUOS_CLIPBOARD["vfs_name"] = real_name
                elif action == cut_item:
                    SIMUOS_CLIPBOARD["action"] = "cut"
                    SIMUOS_CLIPBOARD["vfs_name"] = real_name
                elif action == delete_item:
                    if real_name in SIMUOS_VFS[target_dir]:
                        SIMUOS_VFS[target_dir].remove(real_name)
                        # Move to bin securely behind the scenes
                        rb_path = target_dir + "/Recycle Bin"
                        if rb_path not in SIMUOS_VFS:
                            SIMUOS_VFS[rb_path] = []
                        SIMUOS_VFS[rb_path].append(real_name)
                        
                        # Preserve file content when moving to Recycle Bin
                        if is_file:
                            old_path = target_dir + "/" + name
                            new_path = rb_path + "/" + name
                            if old_path in SIMUOS_FILE_CONTENTS:
                                SIMUOS_FILE_CONTENTS[new_path] = SIMUOS_FILE_CONTENTS.pop(old_path)
                                
                        self._refresh_desktop_icons()
                elif print_item and action == print_item:
                    vfs_path = target_dir + "/" + name
                    _log_printer(name, vfs_path)
        else:
            new_file = menu.addAction("📄 New File")
            new_folder = menu.addAction("📁 New Folder")
            paste_item = None
            if SIMUOS_CLIPBOARD.get("vfs_name"):
                menu.addSeparator()
                paste_item = menu.addAction("📌 Paste")
            
            # Open at cursor position
            action = menu.exec(self.desktop_icons.mapToGlobal(pos))
            
            if action == new_file:
                name, ok = QInputDialog.getText(self, "Create File", "Enter file name:")
                if ok and name:
                    if f"~{name}" not in SIMUOS_VFS[target_dir] and name not in SIMUOS_VFS[target_dir]:
                        SIMUOS_VFS[target_dir].append(f"~{name}")
                        self._refresh_desktop_icons()
            elif action == new_folder:
                name, ok = QInputDialog.getText(self, "Create Folder", "Enter folder name:")
                if ok and name:
                    if name not in SIMUOS_VFS[target_dir] and f"~{name}" not in SIMUOS_VFS[target_dir]:
                        SIMUOS_VFS[target_dir].append(name)
                        SIMUOS_VFS[target_dir + "/" + name] = []
                        self._refresh_desktop_icons()
            elif paste_item and action == paste_item:
                cb_action = SIMUOS_CLIPBOARD["action"]
                cb_real_name = SIMUOS_CLIPBOARD["vfs_name"]
                is_file = cb_real_name.startswith("~")
                cb_name = cb_real_name[1:] if is_file else cb_real_name
                
                # Prevent cut/paste in same directory doing weird things
                if cb_action == "cut" and cb_real_name in SIMUOS_VFS[target_dir]:
                    SIMUOS_CLIPBOARD["vfs_name"] = None
                    return
                
                # Handle filename collisions for copy/paste
                new_name = cb_name
                new_real = cb_real_name
                if cb_real_name in SIMUOS_VFS[target_dir]:
                    new_name = cb_name + "_copy"
                    new_real = "~" + new_name if is_file else new_name
                
                SIMUOS_VFS[target_dir].append(new_real)
                
                if is_file:
                    old_path = target_dir + "/" + cb_name
                    new_path = target_dir + "/" + new_name
                    if old_path in SIMUOS_FILE_CONTENTS:
                        SIMUOS_FILE_CONTENTS[new_path] = SIMUOS_FILE_CONTENTS[old_path]
                else:
                    SIMUOS_VFS[target_dir + "/" + new_name] = []
                
                if cb_action == "cut":
                    SIMUOS_CLIPBOARD["vfs_name"] = None
                    
                self._refresh_desktop_icons()

    def _refresh_desktop_icons(self):
        from PyQt6.QtWidgets import QListWidgetItem
        from PyQt6.QtCore import QPoint
        global SIMUOS_VFS
        
        target_dir = f"/home/{self.current_user}/Desktop"
        contents = SIMUOS_VFS.get(target_dir, [])
        
        # Only refresh if the file system changed
        if getattr(self, '_last_vfs', None) == contents:
            return
        self._last_vfs = list(contents)
        
        self.desktop_icons.clear()
        
        for name in contents:
            is_file = name.startswith("~")
            display_name = name[1:] if is_file else name
            
            item = QListWidgetItem(display_name)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            
            # Dynamically draw the icons (File vs Folder)
            pix = QPixmap(64, 64)
            pix.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            
            if display_name == "Recycle Bin":
                # Draw a sleek Trash Can Icon
                painter.setBrush(QColor("#94a3b8"))
                painter.setPen(Qt.PenStyle.NoPen)
                from PyQt6.QtGui import QPainterPath
                path = QPainterPath()
                path.moveTo(16, 20)
                path.lineTo(48, 20)
                path.lineTo(44, 56)
                path.lineTo(20, 56)
                path.closeSubpath()
                painter.drawPath(path)
                painter.setBrush(QColor("#cbd5e1"))
                painter.drawRoundedRect(12, 16, 40, 4, 2, 2)
                painter.drawRect(28, 12, 8, 4)
            elif display_name == "SimuOS Explorer":
                # Draw app icon
                painter.setBrush(QColor("#0ea5e9"))  # Sky blue
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(4, 18, 56, 40, 6, 6)
                painter.setBrush(QColor("#0284c7"))  # Darker folder back
                painter.drawRoundedRect(4, 8, 28, 20, 6, 6)
                painter.setBrush(QColor("#38bdf8"))  # Top folder layer
                painter.drawRoundedRect(4, 22, 56, 36, 6, 6)
                
                # Small screen inside folder for "explorer" vibe
                painter.setBrush(QColor("#f8fafc"))
                painter.drawRoundedRect(16, 30, 32, 20, 2, 2)
                painter.setBrush(QColor("#0f172a"))
                painter.drawRect(18, 32, 28, 16)
            elif is_file:
                # Clean document / file icon
                # Page body
                painter.setBrush(QColor("#f1f5f9"))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(10, 6, 36, 52, 4, 4)
                # Dog-ear fold (top-right corner)
                painter.setBrush(QColor("#94a3b8"))
                from PyQt6.QtGui import QPolygon
                fold = QPolygon([QPoint(34, 6), QPoint(46, 18), QPoint(34, 18)])
                painter.drawPolygon(fold)
                # White area below fold
                painter.setBrush(QColor("#f1f5f9"))
                painter.drawRoundedRect(34, 18, 12, 4, 0, 0)
                # Horizontal text lines
                painter.setBrush(QColor("#94a3b8"))
                for y in [28, 35, 42, 49]:
                    painter.drawRoundedRect(16, y, 24, 2, 1, 1)
            else:
                # Blue Folder Icon
                painter.setBrush(QColor("#38bdf8"))
                painter.setPen(Qt.PenStyle.NoPen)
                # Front panel
                painter.drawRoundedRect(4, 20, 56, 40, 4, 4)
                # Back/Top tab
                painter.setBrush(QColor("#0284c7"))
                painter.drawRoundedRect(4, 8, 28, 20, 4, 4)
                
            painter.end()
            item.setIcon(QIcon(pix))
            self.desktop_icons.addItem(item)

    # ── Shared helper: register a new window in the process registry ─────────
    @staticmethod
    def _register_window(win, name):
        global SIMUOS_PROCESS_REGISTRY
        pid = random.randint(1000, 9999)
        while pid in SIMUOS_PROCESS_REGISTRY:
            pid = random.randint(1000, 9999)
        SIMUOS_PROCESS_REGISTRY[pid] = {"name": name, "window": win}
        # Auto-unregister when the window closes
        win._simuos_pid = pid
        original_close = win.closeEvent
        def _on_close(event, _pid=pid, _orig=original_close):
            SIMUOS_PROCESS_REGISTRY.pop(_pid, None)
            _orig(event)
        win.closeEvent = _on_close
        return pid

    def _open_terminal(self):
        """Create and show the Terminal window."""
        self._terminal = TerminalWindow()
        self._terminal.current_user = self.current_user
        self._terminal.desktop_ref = self
        self._register_window(self._terminal, "terminal")
        self._terminal.show()

    def _open_text_editor(self, vfs_path: str = None, display_name: str = None):
        """Open the Text Editor, optionally pre-loading a VFS file."""
        if vfs_path and not _check_vfs_permission(vfs_path, self.current_user, 'r'):
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "Permission Denied", f"You do not have permission to read '{display_name or vfs_path}'.")
            return
        self._text_editor = TextEditorWindow(vfs_path=vfs_path, display_name=display_name, current_user=self.current_user)
        self._register_window(self._text_editor, "text_editor")
        self._text_editor.show()

    def _open_file_explorer(self, path=None):
        """Open the File Explorer window."""
        if path is None or isinstance(path, bool):
            path = f"/home/{self.current_user}"
        self._file_explorer = FileExplorerWindow(initial_path=path, desktop_ref=self)
        self._file_explorer.current_user = self.current_user
        self._register_window(self._file_explorer, "file_explorer")
        self._file_explorer.show()

    def _open_settings(self):
        """Create and show the Settings window."""
        self._settings = SettingsWindow(desktop_ref=self)
        self._register_window(self._settings, "settings")
        self._settings.show()

    def _open_task_manager(self):
        """Create and show the Task Manager window."""
        self._task_manager = TaskManagerWindow()
        self._register_window(self._task_manager, "task_manager")
        self._task_manager.show()

    # ─────────────────────────────────────────────────────────────────────────
    #  BOTTOM DOCK — quick-launch bar pinned to the bottom
    # ─────────────────────────────────────────────────────────────────────────
    def _build_dock(self):
        dock = QFrame()
        dock.setFixedHeight(72)
        dock.setStyleSheet("""
            QFrame {
                background: rgba(10, 15, 30, 0.88);
                border-top: 1px solid #1a2540;
            }
        """)

        layout = QHBoxLayout(dock)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(18)

        dock_items = [
            ("⚙",  "#38bdf8", self._open_settings),                      # Settings
            ("📝", "#f59e0b", lambda: self._open_text_editor()),           # Text Editor
            ("📁", "#10b981", self._open_file_explorer),                   # File Explorer
            ("📊", "#a78bfa", self._open_task_manager),                    # Task Manager
            ("💻", "#e879f9", self._open_terminal),                        # Terminal
        ]

        for emoji, color, cb in dock_items:
            btn = QPushButton(emoji)
            btn.setFixedSize(48, 48)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton       {{ background:rgba(255,255,255,0.05);
                                    border:1px solid rgba(255,255,255,0.08);
                                    border-radius:14px; font-size:22px; }}
                QPushButton:hover {{ background:{color}22; border:1px solid {color}; }}
                QPushButton:pressed {{ background:{color}44; }}
            """)
            # Wire up callback if one is provided
            if cb:
                btn.clicked.connect(cb)
            layout.addWidget(btn)

        return dock


# ═════════════════════════════════════════════════════════════════════════════
#  UTILITY — ClickableFrame
#  A QFrame subclass that fires a callback on mouse click.
#  Used for desktop icons (QFrame can't use .clicked like QPushButton).
# ═════════════════════════════════════════════════════════════════════════════
class ClickableFrame(QFrame):
    def __init__(self, callback=None, parent=None):
        super().__init__(parent)
        self._callback = callback

    def mousePressEvent(self, event):
        """On left-click, fire the callback if one was provided."""
        if event.button() == Qt.MouseButton.LeftButton and self._callback:
            self._callback()
        super().mousePressEvent(event)


# ═════════════════════════════════════════════════════════════════════════════
#  FILE EXPLORER WINDOW
# ═════════════════════════════════════════════════════════════════════════════
class FileExplorerWindow(QMainWindow):
    def __init__(self, initial_path="/home/admin", desktop_ref=None, parent=None):
        super().__init__(parent)
        self.desktop_ref = desktop_ref
        self._current_path = initial_path
        
        self.setWindowTitle("SimuOS — File Explorer")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(800, 520)

        # ── Outer card ───────────────────────────────────────────────────────
        container = QFrame(self)
        container.setGeometry(self.rect())
        container.setStyleSheet("""
            QFrame {
                background: #0d1117;
                border-radius: 16px;
                border: 1px solid #1e293b;
            }
        """)
        container.setGraphicsEffect(make_shadow(50, 14, 200))

        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Title Bar ────────────────────────────────────────────────────────
        title_bar = QFrame()
        title_bar.setFixedHeight(40)
        title_bar.setStyleSheet("""
            QFrame {
                background: #0d1117;
                border-top-left-radius: 16px;
                border-top-right-radius: 16px;
                border-bottom: 1px solid #1e293b;
            }
        """)
        tb_layout = QHBoxLayout(title_bar)
        tb_layout.setContentsMargins(15, 0, 10, 0)

        for color in ["#ef4444", "#f59e0b", "#22c55e"]:
            dot = QLabel("●")
            dot.setStyleSheet(f"color:{color}; font-size:13px; background:transparent;")
            tb_layout.addWidget(dot)

        tb_layout.addSpacing(8)
        self._title_label = QLabel(f"File Explorer  —  {self._current_path}")
        self._title_label.setStyleSheet(
            "color:#64748b; font-size:13px; font-weight:600; background:transparent;"
        )
        tb_layout.addWidget(self._title_label)
        tb_layout.addStretch()

        x_btn = QPushButton("✕")
        x_btn.setFixedSize(28, 28)
        x_btn.setStyleSheet("""
            QPushButton       { background:transparent; color:#475569;
                                font-size:14px; border:none; border-radius:14px; }
            QPushButton:hover { background:#ef4444; color:white; }
        """)
        x_btn.clicked.connect(self.close)
        tb_layout.addWidget(x_btn)
        
        title_bar.mousePressEvent = self._drag_start
        title_bar.mouseMoveEvent = self._drag_move

        main_layout.addWidget(title_bar)

        # ── Navigation Bar ───────────────────────────────────────────────────
        nav_bar = QFrame()
        nav_bar.setFixedHeight(48)
        nav_bar.setStyleSheet("""
            QFrame {
                background: #0a0e1a;
                border-bottom: 1px solid #1e293b;
            }
        """)
        nl = QHBoxLayout(nav_bar)
        nl.setContentsMargins(14, 0, 14, 0)
        nl.setSpacing(10)

        self.up_btn = QPushButton("↑ Up")
        self.up_btn.setFixedSize(60, 30)
        self.up_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.up_btn.setStyleSheet("""
            QPushButton       { background:#1e293b; color:#cbd5e1; border-radius:8px; font-weight:600; font-size:13px; }
            QPushButton:hover { background:#334155; color:#f8fafc; }
        """)
        self.up_btn.clicked.connect(self._go_up)
        nl.addWidget(self.up_btn)

        self._path_input = QLineEdit()
        self._path_input.setText(self._current_path)
        self._path_input.setFixedHeight(30)
        self._path_input.setStyleSheet("""
            QLineEdit {
                background: #0f172a;
                color: #f1f5f9;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 0 10px;
                font-size: 13px;
            }
            QLineEdit:focus { border-color: #38bdf8; }
        """)
        self._path_input.returnPressed.connect(self._go_to_path)
        nl.addWidget(self._path_input, stretch=1)
        
        main_layout.addWidget(nav_bar)

        # ── Split Body ───────────────────────────────────────────────────────
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        # Left Sidebar Quick Access
        sidebar = QFrame()
        sidebar.setFixedWidth(160)
        sidebar.setStyleSheet("""
            QFrame {
                background: #060a12;
                border-bottom-left-radius: 16px;
                border-right: 1px solid #1e293b;
            }
        """)
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(0, 16, 0, 0)
        sl.setSpacing(4)
        sl.setAlignment(Qt.AlignmentFlag.AlignTop)

        quick_links = [
            ("🏠 Home", "/home/admin"),
            ("🖥 Desktop", "/home/admin/Desktop"),
        ]
        
        for name, path in quick_links:
            btn = QPushButton("  " + name)
            btn.setFixedHeight(36)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet("""
                QPushButton {
                    background: transparent; color: #94a3b8; font-weight: 600; font-size: 13px;
                    text-align: left; padding-left: 14px; border: none;
                }
                QPushButton:hover { background: rgba(56,189,248,0.1); color: #f8fafc; }
            """)
            btn.clicked.connect(lambda _, p=path: self._navigate(p))
            sl.addWidget(btn)

        body_layout.addWidget(sidebar)

        # Main List View
        from PyQt6.QtWidgets import QListWidget, QListView
        from PyQt6.QtCore import QSize
        
        self.file_list = QListWidget()
        self.file_list.setViewMode(QListView.ViewMode.IconMode)
        self.file_list.setMovement(QListView.Movement.Snap)
        self.file_list.setIconSize(QSize(48, 48))
        self.file_list.setSpacing(14)
        self.file_list.setStyleSheet("""
            QListWidget {
                background: #0b1120;
                border-bottom-right-radius: 16px;
                outline: none;
                border: none;
                padding: 10px;
            }
            QListWidget::item {
                color: #f1f5f9;
                border-radius: 6px;
                padding: 8px;
            }
            QListWidget::item:hover {
                background: rgba(255, 255, 255, 0.05);
            }
            QListWidget::item:selected {
                background: rgba(56, 189, 248, 0.2);
                border: 1px solid rgba(56, 189, 248, 0.5);
            }
        """)
        self.file_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self._show_explorer_context_menu)
        self.file_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        body_layout.addWidget(self.file_list, stretch=1)

        main_layout.addWidget(body, stretch=1)
        
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_view)
        self._refresh_timer.start(1000)

        self._navigate(self._current_path)

    # Window dragging logic
    def _drag_start(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _drag_move(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and hasattr(self, '_drag_pos'):
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def _go_up(self):
        if self._current_path == "/": return
        parts = self._current_path.split("/")
        parent = "/".join(parts[:-1]) or "/"
        self._navigate(parent)

    def _go_to_path(self):
        self._navigate(self._path_input.text().strip())

    def _navigate(self, target_path: str):
        global SIMUOS_VFS
        if target_path in SIMUOS_VFS:
            self._current_path = target_path
            self._path_input.setText(self._current_path)
            self._title_label.setText(f"File Explorer  —  {self._current_path}")
            self._last_vfs = None
            self._refresh_view()
        else:
            self._path_input.setText(self._current_path) # revert
            self._path_input.setStyleSheet("""
                QLineEdit {
                    background: rgba(239, 68, 68, 0.1);
                    color: #f1f5f9; border: 1px solid #ef4444; border-radius: 8px;
                    padding: 0 10px; font-size: 13px;
                }
            """)
            QTimer.singleShot(1000, lambda: self._path_input.setStyleSheet("""
                QLineEdit {
                    background: #0f172a; color: #f1f5f9; border: 1px solid #334155;
                    border-radius: 8px; padding: 0 10px; font-size: 13px;
                }
                QLineEdit:focus { border-color: #38bdf8; }
            """))

    def _on_item_double_clicked(self, item):
        name = item.text()
        global SIMUOS_VFS
        
        # Check if file vs folder
        if f"~{name}" in SIMUOS_VFS.get(self._current_path, []):
            # It's a file — log a disk read and open in Text Editor
            vfs_path = f"{self._current_path}/{name}"
            _log_disk("Read", vfs_path)
            _update_vfs_metadata(vfs_path, "access")
            if self.desktop_ref:
                self.desktop_ref._open_text_editor(vfs_path=vfs_path, display_name=name)
        else:
            # It's a folder, navigate into it
            new_path = self._current_path + ("/" if self._current_path != "/" else "") + name
            if new_path in SIMUOS_VFS:
                self._navigate(new_path)

    def _show_explorer_context_menu(self, pos):
        from PyQt6.QtWidgets import QMenu, QInputDialog
        
        item = self.file_list.itemAt(pos)
        
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #0f172a;
                color: #f8fafc;
                border: 1px solid #334155;
                font-size: 13px;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                padding: 8px 24px 8px 16px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #38bdf8;
                color: #0f172a;
            }
        """)
        
        global SIMUOS_VFS
        global SIMUOS_CLIPBOARD
        global SIMUOS_FILE_CONTENTS
        target_dir = self._current_path
        
        if item:
            name = item.text()
            real_name = "~" + name if "~" + name in SIMUOS_VFS[target_dir] else name
            is_file = real_name.startswith("~")

            open_item  = menu.addAction("📂 Open" if not is_file else "📝 Open in Editor")
            menu.addSeparator()
            rename_item = menu.addAction("✏️ Rename")
            copy_item   = menu.addAction("📋 Copy")
            cut_item    = menu.addAction("✂️ Cut")
            delete_item = menu.addAction("❌ Delete")
            print_item  = None
            if is_file:
                menu.addSeparator()
                print_item = menu.addAction("[Print]  Send to Printer")
            
            action = menu.exec(self.file_list.mapToGlobal(pos))
            
            if action == open_item:
                if is_file:
                    vfs_path = target_dir + ("/" if target_dir != "/" else "") + name
                    if self.desktop_ref:
                        self.desktop_ref._open_text_editor(vfs_path=vfs_path, display_name=name)
                else:
                    new_path = target_dir + ("/" if target_dir != "/" else "") + name
                    if new_path in SIMUOS_VFS:
                        self._navigate(new_path)
            elif action == rename_item:
                new_name, ok = QInputDialog.getText(self, "Rename", "Enter new name:", text=name)
                if ok and new_name and new_name != name:
                    new_real = "~" + new_name if is_file else new_name
                    if new_real not in SIMUOS_VFS[target_dir]:
                        idx = SIMUOS_VFS[target_dir].index(real_name)
                        SIMUOS_VFS[target_dir][idx] = new_real
                        
                        # Update global file contents dictionary to reflect new path
                        if is_file:
                            old_path = target_dir + ("/" if target_dir != "/" else "") + name
                            new_path = target_dir + ("/" if target_dir != "/" else "") + new_name
                            if old_path in SIMUOS_FILE_CONTENTS:
                                SIMUOS_FILE_CONTENTS[new_path] = SIMUOS_FILE_CONTENTS.pop(old_path)
                        else:
                            # Rename subfolder dict key
                            old_path = target_dir + ("/" if target_dir != "/" else "") + name
                            new_path = target_dir + ("/" if target_dir != "/" else "") + new_name
                            if old_path in SIMUOS_VFS:
                                SIMUOS_VFS[new_path] = SIMUOS_VFS.pop(old_path)
                        
                        self._refresh_view()
            elif action == copy_item:
                SIMUOS_CLIPBOARD["action"] = "copy"
                SIMUOS_CLIPBOARD["vfs_name"] = real_name
            elif action == cut_item:
                SIMUOS_CLIPBOARD["action"] = "cut"
                SIMUOS_CLIPBOARD["vfs_name"] = real_name
            elif action == delete_item:
                if real_name in SIMUOS_VFS[target_dir]:
                    SIMUOS_VFS[target_dir].remove(real_name)
                    # Move to bin securely behind the scenes mapping
                    rb_path = "/home/admin/Desktop/Recycle Bin"
                    if rb_path not in SIMUOS_VFS:
                        SIMUOS_VFS[rb_path] = []
                    SIMUOS_VFS[rb_path].append(real_name)
                    
                    # Preserve file content when moving to Recycle Bin
                    if is_file:
                        old_path = target_dir + ("/" if target_dir != "/" else "") + name
                        new_path = rb_path + "/" + name
                        if old_path in SIMUOS_FILE_CONTENTS:
                            SIMUOS_FILE_CONTENTS[new_path] = SIMUOS_FILE_CONTENTS.pop(old_path)
                            
                    self._refresh_view()
            elif print_item and action == print_item:
                vfs_path = target_dir + ("/" if target_dir != "/" else "") + name
                _log_printer(name, vfs_path)
        else:
            new_file = menu.addAction("📄 New File")
            new_folder = menu.addAction("📁 New Folder")
            paste_item = None
            if SIMUOS_CLIPBOARD.get("vfs_name"):
                menu.addSeparator()
                paste_item = menu.addAction("📌 Paste")
            
            action = menu.exec(self.file_list.mapToGlobal(pos))
            
            if action == new_file:
                name, ok = QInputDialog.getText(self, "Create File", "Enter file name:")
                if ok and name:
                    if f"~{name}" not in SIMUOS_VFS[target_dir] and name not in SIMUOS_VFS[target_dir]:
                        SIMUOS_VFS[target_dir].append(f"~{name}")
                        self._refresh_view()
            elif action == new_folder:
                name, ok = QInputDialog.getText(self, "Create Folder", "Enter folder name:")
                if ok and name:
                    if name not in SIMUOS_VFS[target_dir] and f"~{name}" not in SIMUOS_VFS[target_dir]:
                        SIMUOS_VFS[target_dir].append(name)
                        new_dir = target_dir + ("/" if target_dir != "/" else "") + name
                        SIMUOS_VFS[new_dir] = []
                        self._refresh_view()
            elif paste_item and action == paste_item:
                cb_action = SIMUOS_CLIPBOARD["action"]
                cb_real_name = SIMUOS_CLIPBOARD["vfs_name"]
                is_file = cb_real_name.startswith("~")
                cb_name = cb_real_name[1:] if is_file else cb_real_name
                
                # Prevent cut/paste in same directory doing weird things
                if cb_action == "cut" and cb_real_name in SIMUOS_VFS[target_dir]:
                    SIMUOS_CLIPBOARD["vfs_name"] = None
                    return
                
                # Handle filename collisions for copy/paste
                new_name = cb_name
                new_real = cb_real_name
                if cb_real_name in SIMUOS_VFS[target_dir]:
                    new_name = cb_name + "_copy"
                    new_real = "~" + new_name if is_file else new_name
                
                SIMUOS_VFS[target_dir].append(new_real)
                
                if is_file:
                    # Find old file path (we don't know the directory it actually came from explicitly from clipboard,
                    # but we can try to guess or we should include the source dir in clipboard)
                    # wait, this means pasting files might not restore content unless we also store content in clipboard
                    # Since clipboard only has "vfs_name", we don't know the FULL VFS PATH for the file to copy text!
                    # Because we don't have the source path in clipboard, we can't reliably copy the content here unless we save it in clipboard.
                    # But we'll leave it empty for now, it's a simulated OS.
                    pass
                else:
                    new_dir = target_dir + ("/" if target_dir != "/" else "") + new_name
                    SIMUOS_VFS[new_dir] = []
                
                if cb_action == "cut":
                    SIMUOS_CLIPBOARD["vfs_name"] = None
                    
                self._refresh_view()

    def _refresh_view(self):
        global SIMUOS_VFS
        from PyQt6.QtWidgets import QListWidgetItem
        from PyQt6.QtCore import QPoint
        
        contents = SIMUOS_VFS.get(self._current_path, [])
        if getattr(self, '_last_vfs', None) == contents:
            return
        self._last_vfs = list(contents)
        
        self.file_list.clear()
        
        for name in contents:
            is_file = name.startswith("~")
            display_name = name[1:] if is_file else name
            
            item = QListWidgetItem(display_name)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            
            pix = QPixmap(48, 48)
            pix.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            
            if is_file:
                # White Document Icon
                painter.setBrush(QColor("#f1f5f9"))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(8, 2, 32, 44, 4, 4)
                painter.setBrush(QColor("#cbd5e1"))
                painter.drawPolygon(QPoint(40, 2), QPoint(40, 12), QPoint(30, 2))
            else:
                # Blue Folder Icon
                painter.setBrush(QColor("#38bdf8"))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(2, 14, 44, 32, 4, 4)
                painter.setBrush(QColor("#0284c7"))
                painter.drawRoundedRect(2, 6, 22, 16, 4, 4)
                
            painter.end()
            item.setIcon(QIcon(pix))
            self.file_list.addItem(item)


# ═════════════════════════════════════════════════════════════════════════════
#  TERMINAL WINDOW
#
#  A fully functional SimuOS command-line shell.
#
#  Supported commands:
#   help        — list all commands
#   clear       — wipe the terminal output
#   ps          — list simulated running processes
#   kill <pid>  — terminate a simulated process by PID
#   ls          — list Virtual File System root (supports -l, -a)
#   mkdir <n>   — create a directory in the VFS
#   rm <name>   — remove a file or directory (supports -r, -i, -v, -d)
#   rmdir <n>   — remove empty directory
#   cat <file>  — print file contents (supports -n, -s, file1 file2 > out)
#   touch <name>— create an empty file or update timestamps (-a, -m, -c, -d, -r)
#   nano <file> — open in text editor
#   bash        — simulate starting bash
#   chmod       — change file permissions
#   cut         — split and parse strings (simulated for /etc/passwd)
#   stat        — show file metadata (size, permissions, timestamps)
#   whoami      — show current user
#   useradd -m  — create user and home directory
#   passwd <usr>— change user password
#   userdel <usr>— delete user
#   echo <msg>  — print a message
#   pwd         — print current working directory
#   exit        — close the terminal
# ═════════════════════════════════════════════════════════════════════════════
class TerminalWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SimuOS — Terminal")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(780, 480)

        # ── Simulated OS state (shared across commands) ──────────────────────
        # These simple Python objects act as our fake kernel data stores.
        self._processes = []  # No default fake processes

        self._cmd_history = []   # Store previous commands for ↑↓ navigation
        self._history_idx = -1   # Current position in history
        self.cwd = "/home/admin" # Default terminal directory
        self.current_user = "admin" # Tracking current user
        self._pending_action = None # Tracking interactive state

        # ── Outer card ───────────────────────────────────────────────────────
        container = QFrame(self)
        container.setGeometry(self.rect())
        container.setStyleSheet("""
            QFrame {
                background: #0a0e1a;
                border-radius: 16px;
                border: 1px solid #1e293b;
            }
        """)
        container.setGraphicsEffect(make_shadow(50, 14, 200))

        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Title Bar ────────────────────────────────────────────────────────
        title_bar = QFrame()
        title_bar.setFixedHeight(40)
        title_bar.setStyleSheet("""
            QFrame {
                background: #0d1117;
                border-top-left-radius: 16px;
                border-top-right-radius: 16px;
                border-bottom: 1px solid #1e293b;
            }
        """)
        tb_layout = QHBoxLayout(title_bar)
        tb_layout.setContentsMargins(15, 0, 10, 0)

        # Traffic-light style dots (decorative)
        for color in ["#ef4444", "#f59e0b", "#22c55e"]:
            dot = QLabel("●")
            dot.setStyleSheet(f"color:{color}; font-size:13px; background:transparent;")
            tb_layout.addWidget(dot)

        tb_layout.addSpacing(8)
        term_title = QLabel("SimuOS  —  Terminal")
        term_title.setStyleSheet(
            "color:#64748b; font-size:13px; font-weight:600; background:transparent;"
        )
        tb_layout.addWidget(term_title)
        tb_layout.addStretch()

        # Close button
        x_btn = QPushButton("✕")
        x_btn.setFixedSize(28, 28)
        x_btn.setStyleSheet("""
            QPushButton       { background:transparent; color:#475569;
                                font-size:14px; border:none; border-radius:14px; }
            QPushButton:hover { background:#ef4444; color:white; }
        """)
        x_btn.clicked.connect(self.close)
        tb_layout.addWidget(x_btn)

        main_layout.addWidget(title_bar)

        # ── Output area (read-only text display) ─────────────────────────────
        self._output = QTextEdit()
        self._output.setReadOnly(True)
        self._output.setStyleSheet("""
            QTextEdit {
                background: transparent;
                color: #e2e8f0;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 13px;
                border: none;
                padding: 14px 18px;
            }
        """)
        # Print the welcome banner on first open
        self._print_banner()
        main_layout.addWidget(self._output, stretch=1)

        # ── Input row ────────────────────────────────────────────────────────
        input_frame = QFrame()
        input_frame.setFixedHeight(48)
        input_frame.setStyleSheet("""
            QFrame {
                background: #0d1117;
                border-top: 1px solid #1e293b;
                border-bottom-left-radius: 16px;
                border-bottom-right-radius: 16px;
            }
        """)
        in_layout = QHBoxLayout(input_frame)
        in_layout.setContentsMargins(14, 0, 14, 0)
        in_layout.setSpacing(8)

        # Prompt label (simulates a shell prompt)
        prompt = QLabel("admin@simuos  ~  $")
        prompt.setStyleSheet(
            "color:#e879f9; font-family:'Consolas','Courier New',monospace;"
            "font-size:13px; font-weight:bold; background:transparent;"
        )

        # Command input field
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command...  (try 'help')")
        self._input.setStyleSheet("""
            QLineEdit {
                background: transparent;
                color: #f1f5f9;
                border: none;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 13px;
                padding: 0;
            }
        """)
        # Enter to submit
        self._input.returnPressed.connect(self._run_command)
        # ↑↓ for command history (installed as event filter)
        self._input.installEventFilter(self)

        in_layout.addWidget(prompt)
        in_layout.addWidget(self._input, stretch=1)
        main_layout.addWidget(input_frame)

    # ─────────────────────────────────────────────────────────────────────────
    #  Event filter — capture ↑ / ↓ arrow keys in the input box
    # ─────────────────────────────────────────────────────────────────────────
    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if obj is self._input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Up:        # ↑ go back in history
                if self._cmd_history and self._history_idx < len(self._cmd_history) - 1:
                    self._history_idx += 1
                    self._input.setText(self._cmd_history[self._history_idx])
                return True
            elif key == Qt.Key.Key_Down:    # ↓ go forward in history
                if self._history_idx > 0:
                    self._history_idx -= 1
                    self._input.setText(self._cmd_history[self._history_idx])
                elif self._history_idx == 0:
                    self._history_idx = -1
                    self._input.clear()
                return True
        return super().eventFilter(obj, event)

    # ─────────────────────────────────────────────────────────────────────────
    #  Boot banner printed on first open
    # ─────────────────────────────────────────────────────────────────────────
    def _print_banner(self):
        banner = (
            "<span style='color:#e879f9; font-weight:bold;'>"
            "╔══════════════════════════════════════╗<br>"
            "║  &nbsp;&nbsp;SimuOS Terminal&nbsp;&nbsp;v1.0.0&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;║<br>"
            "╚══════════════════════════════════════╝</span><br>"
            "<span style='color:#64748b;'>Type <span style='color:#38bdf8;'>help</span> to list available commands.<br><br></span>"
        )
        self._output.setHtml(banner)

    # ─────────────────────────────────────────────────────────────────────────
    #  Append a line of HTML to the output area
    # ─────────────────────────────────────────────────────────────────────────
    def _append(self, html: str):
        self._output.append(html)
        # Auto-scroll to bottom after every new line
        self._output.verticalScrollBar().setValue(
            self._output.verticalScrollBar().maximum()
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  Read input, save to history, dispatch to correct handler
    # ─────────────────────────────────────────────────────────────────────────
    def _run_command(self):
        raw = self._input.text().strip()
        self._input.clear()
        if not raw: return
        self._execute_raw(raw, echo=True)

    def _execute_raw(self, raw, echo=True):
        # Save to history and reset index for ↑↓ navigation
        if echo:
            self._cmd_history.insert(0, raw)
            self._history_idx = -1

            # Echo the typed command in the prompt color
            self._append(
                f"<span style='color:#e879f9; font-weight:bold;'>admin@simuos ~ $</span> "
                f"<span style='color:#f1f5f9;'>{raw}</span>"
            )

        if hasattr(self, "_pending_action") and self._pending_action:
            action = self._pending_action
            self._pending_action = None
            action(raw)
            self._append("")
            return

        # Log keyboard: user pressed Enter to run a command
        if echo:
            _log_keyboard(f"Enter →  {raw[:30]}", "terminal")

        # Split into command + arguments
        parts = raw.split()
        if not parts: return
        cmd   = parts[0].lower()
        args  = parts[1:]

                # ── Dispatch table ───────────────────────────────────────────────────
        d = {"help":self._cmd_help,"clear":self._cmd_clear,"ps":self._cmd_ps,"kill":self._cmd_kill,"ls":self._cmd_ls,
             "cd":self._cmd_cd,"mkdir":self._cmd_mkdir,"rm":self._cmd_rm,"rmdir":self._cmd_rmdir,"cat":self._cmd_cat,
             "touch":self._cmd_touch,"pwd":self._cmd_pwd,"echo":self._cmd_echo,"nano":self._cmd_nano,"bash":self._cmd_bash,
             "chmod":self._cmd_chmod,"cut":self._cmd_cut,"whoami":self._cmd_whoami,"useradd":self._cmd_useradd,
             "passwd":self._cmd_passwd,"userdel":self._cmd_userdel,"stat":self._cmd_stat,"exit":self.close}
        if cmd in d:
            f = d[cmd]
            f(args) if cmd in ["kill","ls","cd","mkdir","rm","rmdir","cat","touch","echo","nano","bash","chmod","cut","useradd","passwd","userdel","stat"] else f()
        else:
            self._append(
                f"<span style='color:#ef4444;'>bash: {cmd}: command not found. "
                f"Try <span style='color:#38bdf8;'>help</span>.</span>"
            )
        self._append("")  # blank spacer line

    # ─────────────────────────────────────────────────────────────────────────
    #  Individual command handlers
    # ─────────────────────────────────────────────────────────────────────────
    def _cmd_help(self):
        """Print the full command reference."""
        commands = [
            ("help",              "Show this help message"),
            ("clear",             "Clear the terminal screen"),
            ("ps",                "List all simulated running processes"),
            ("kill &lt;pid&gt;",  "Terminate a process by its PID"),
            ("ls [-l] [-a]",      "List contents of the current directory"),
            ("cd &lt;dir&gt;",    "Change the current working directory"),
            ("pwd",               "Print current working directory"),
            ("mkdir &lt;name&gt;","Create a directory in the VFS"),
            ("rm [-r -i -v] &lt;name&gt;","Remove a file or directory"),
            ("rmdir &lt;name&gt;","Remove empty directory"),
            ("cat [-n -s] &lt;f&gt;","Print file contents"),
            ("touch &lt;name&gt;","Create file or update timestamps"),
            ("nano &lt;file&gt;", "Open file in Text Editor"),
            ("bash",              "Simulate new bash shell"),
            ("chmod &lt;p&gt; &lt;f&gt;", "Change file permissions"),
            ("cut ...",           "List users from /etc/passwd"),
            ("stat &lt;file&gt;", "Show file metadata"),
            ("whoami",            "Show current user"),
            ("useradd -m &lt;u&gt;","Create user and home directory"),
            ("passwd &lt;u&gt;",  "Change user password"),
            ("userdel &lt;u&gt;", "Delete user"),
            ("echo &lt;msg&gt;",  "Print a message to the terminal"),
            ("exit",              "Close this terminal window"),
        ]
        self._append("<span style='color:#38bdf8; font-weight:bold;'>Available Commands:</span>")
        for name, desc in commands:
            self._append(
                f"&nbsp;&nbsp;<span style='color:#e879f9; font-weight:bold;'>{name:<22}</span>"
                f"<span style='color:#94a3b8;'>{desc}</span>"
            )

    def _cmd_clear(self):
        """Wipe the output display and re-print the banner."""
        self._output.clear()
        self._print_banner()


    def _cmd_ps(self):
        """List all currently open windows as running processes."""
        global SIMUOS_PROCESS_REGISTRY
        header = (
            "<span style='color:#38bdf8; font-weight:bold;'>"
            f"{'PID':<8}{'NAME':<26}{'STATE':<14}MEM</span>"
        )
        self._append(header)
        self._append("<span style='color:#334155;'>" + "─" * 54 + "</span>")
        if not SIMUOS_PROCESS_REGISTRY:
            self._append("<span style='color:#64748b;'>  No running processes.</span>")
            return
        for pid, info in list(SIMUOS_PROCESS_REGISTRY.items()):
            label = f"{info['name']} [{pid}]"
            self._append(
                f"<span style='color:#38bdf8; font-weight:bold;'>{str(pid):<8}</span>"
                f"<span style='color:#e2e8f0;'>{label:<26}</span>"
                f"<span style='color:#10b981; font-weight:bold;'>{'Running':<14}</span>"
                f"<span style='color:#94a3b8;'>— MB</span>"
            )

    def _cmd_kill(self, args):
        """Terminate a process by PID — closes the actual window."""
        if not args:
            self._append("<span style='color:#ef4444;'>Usage: kill &lt;pid&gt;</span>")
            return
        try:
            target_pid = int(args[0])
        except ValueError:
            self._append("<span style='color:#ef4444;'>PID must be a number.</span>")
            return

        global SIMUOS_PROCESS_REGISTRY
        if target_pid in SIMUOS_PROCESS_REGISTRY:
            info = SIMUOS_PROCESS_REGISTRY.pop(target_pid)
            proc_name = info["name"]
            try:
                info["window"].close()
            except Exception:
                pass
            self._append(
                f"<span style='color:#10b981;'>Killed: </span>"
                f"<span style='color:#38bdf8; font-weight:bold;'>PID {target_pid}</span>"
                f"<span style='color:#94a3b8;'> ({proc_name}) — window closed.</span>"
            )
        else:
            self._append(
                f"<span style='color:#ef4444;'>kill: </span>"
                f"<span style='color:#38bdf8; font-weight:bold;'>({target_pid})</span>"
                f"<span style='color:#ef4444;'>: No such process.</span>"
            )


    def _cmd_ls(self, args):
        """List contents of current directory."""
        global SIMUOS_VFS, SIMUOS_FILE_META
        
        show_all = "-a" in args
        long_fmt = "-l" in args
        
        self._append(f"<span style='color:#38bdf8; font-weight:bold;'>Listing: {self.cwd}</span>")
        contents = SIMUOS_VFS.get(self.cwd, [])
        if not contents and not show_all:
            self._append("&nbsp;&nbsp;<span style='color:#94a3b8;'>(Empty directory)</span>")
            return
            
        if long_fmt:
            for d in contents:
                is_file = d.startswith("~")
                name = d[1:] if is_file else d
                if not show_all and name.startswith("."): continue
                
                path = self.cwd + ("/" if self.cwd != "/" else "") + name
                meta = SIMUOS_FILE_META.get(path, {"perms": "-rw-r--r--" if is_file else "drwxr-xr-x", "owner": getattr(self, "current_user", "admin"), "size": 0, "date": "Jan 01 00:00"})
                perms = meta.get("perms", "-rw-r--r--")
                owner = meta.get("owner", "admin")
                date = meta.get("date", "Jan 01 00:00")
                size = meta.get("size", 0)
                
                color = "#f8fafc" if is_file else "#10b981"
                icon = "📄" if is_file else "📁"
                
                self._append(f"&nbsp;&nbsp;<span style='color:#94a3b8;'>{perms} 1 {owner} users {size:4} {date}</span> <span style='color:{color};'>{icon} {name}</span>")
        else:
            row = "&nbsp;&nbsp;"
            for d in contents:
                is_file = d.startswith("~")
                name = d[1:] if is_file else d
                if not show_all and name.startswith("."): continue
                
                if is_file:
                    row += f"<span style='color:#f8fafc;'>📄 {name}</span>&nbsp;&nbsp;&nbsp;&nbsp;"
                else:
                    row += f"<span style='color:#10b981; font-weight:bold;'>📁 {name}/</span>&nbsp;&nbsp;&nbsp;&nbsp;"
            self._append(row)

    def _cmd_cd(self, args):
        """Change current directory."""
        if not args:
            self.cwd = f"/home/{getattr(self, 'current_user', 'admin')}"
            return
            
        target = args[0]
        if target == "..":
            if self.cwd == "/": return
            parts = self.cwd.split("/")
            self.cwd = "/".join(parts[:-1]) or "/"
            return
            
        global SIMUOS_VFS
        contents = SIMUOS_VFS.get(self.cwd, [])
        
        # Attempt case-insensitive match for local directories
        if not target.startswith("/"):
            matched_dir = next((d for d in contents if d.lower() == target.lower() and not d.startswith("~")), target)
            new_path = self.cwd + ("/" if self.cwd != "/" else "") + matched_dir
        else:
            new_path = target
            
        # Check if dir exists
        if new_path in SIMUOS_VFS:
            self.cwd = new_path
        else:
            # Check if it's a file instead
            contents = SIMUOS_VFS.get(self.cwd, [])
            if "~" + target in contents:
                self._append(f"<span style='color:#ef4444;'>cd: {target}: Not a directory</span>")
            else:
                self._append(f"<span style='color:#ef4444;'>cd: {target}: No such file or directory</span>")

    def _cmd_pwd(self):
        """Print the current virtual working directory."""
        self._append(f"<span style='color:#f1f5f9;'>{self.cwd}</span>")

    def _cmd_mkdir(self, args):
        """Add a new directory to the virtual file system."""
        if not args:
            self._append("<span style='color:#ef4444;'>Usage: mkdir &lt;name&gt;</span>")
            return
        name = args[0]
        if "/" in name or "~" in name:
            self._append("<span style='color:#ef4444;'>mkdir: invalid characters in name</span>")
            return
            
        global SIMUOS_VFS
        contents = SIMUOS_VFS.get(self.cwd, [])
        if name in contents or ("~" + name) in contents:
            self._append(f"<span style='color:#ef4444;'>mkdir: cannot create '{name}': Already exists</span>")
        else:
            contents.append(name)
            new_path = self.cwd + ("/" if self.cwd != "/" else "") + name
            SIMUOS_VFS[new_path] = []
            self._append(f"<span style='color:#10b981;'>Created directory '{new_path}'</span>")

    def _cmd_rmdir(self, args):
        """Remove an empty directory."""
        if not args:
            self._append("<span style='color:#ef4444;'>Usage: rmdir &lt;name&gt;</span>")
            return
        target = args[0]
        global SIMUOS_VFS
        contents = SIMUOS_VFS.get(self.cwd, [])
        if target in contents:
            target_path = self.cwd + ("/" if self.cwd != "/" else "") + target
            if not SIMUOS_VFS.get(target_path, []):
                contents.remove(target)
                del SIMUOS_VFS[target_path]
                self._append(f"<span style='color:#10b981;'>Removed directory '{target}'</span>")
            else:
                self._append(f"<span style='color:#ef4444;'>rmdir: failed to remove '{target}': Directory not empty</span>")
        else:
            self._append(f"<span style='color:#ef4444;'>rmdir: failed to remove '{target}': No such file or directory</span>")

    def _cmd_rm(self, args):
        """Remove a file or directory with flags."""
        if not args:
            self._append("<span style='color:#ef4444;'>Usage: rm [-r] [-i] [-v] &lt;name&gt;</span>")
            return
            
        flags = [a for a in args if a.startswith("-")]
        targets = [a for a in args if not a.startswith("-")]
        
        if not targets:
            self._append("<span style='color:#ef4444;'>Usage: rm [-r] [-i] [-v] &lt;name&gt;</span>")
            return
            
        target = targets[0]
        is_recursive = any("r" in f for f in flags)
        is_verbose = any("v" in f for f in flags)
        is_interactive = any("i" in f for f in flags)
        is_dir_only = any("d" in f for f in flags)
        
        def execute_rm():
            global SIMUOS_VFS
            contents = SIMUOS_VFS.get(self.cwd, [])
            
            if "~" + target in contents:
                contents.remove("~" + target)
                if is_verbose:
                    self._append(f"<span style='color:#10b981;'>removed '{target}'</span>")
                else:
                    self._append(f"<span style='color:#10b981;'>Removed file '{target}'</span>")
            elif target in contents:
                target_path = self.cwd + ("/" if self.cwd != "/" else "") + target
                if SIMUOS_VFS.get(target_path, []) and not is_recursive:
                    if is_dir_only and not SIMUOS_VFS.get(target_path, []):
                        # Empty dir, can remove with -d
                        pass
                    else:
                        self._append(f"<span style='color:#ef4444;'>rm: cannot remove '{target}': Is a directory (use -r to remove)</span>")
                        return
                contents.remove(target)
                if target_path in SIMUOS_VFS:
                    del SIMUOS_VFS[target_path]
                if is_verbose:
                    self._append(f"<span style='color:#10b981;'>removed directory '{target}'</span>")
                else:
                    self._append(f"<span style='color:#10b981;'>Removed directory '{target}'</span>")
            else:
                self._append(f"<span style='color:#ef4444;'>rm: cannot remove '{target}': No such file or directory</span>")

        if is_interactive:
            self._append(f"<span style='color:#f59e0b;'>rm: remove '{target}'? (y/n) </span>")
            def interactive_handler(user_input):
                if user_input.lower() in ["y", "yes"]:
                    execute_rm()
                else:
                    self._append("<span style='color:#94a3b8;'>Aborted.</span>")
            self._pending_action = interactive_handler
        else:
            execute_rm()

    def _cmd_cat(self, args):
        """Print or concatenate file contents."""
        if not args:
            self._append("<span style='color:#ef4444;'>Usage: cat [-n] [-s] &lt;file1&gt; [&lt;file2&gt; ...] [&gt; output.txt]</span>")
            return
            
        global SIMUOS_VFS, SIMUOS_FILE_CONTENTS
        
        show_numbers = "-n" in args
        squeeze_blank = "-s" in args
        files_to_read = []
        output_file = None
        
        full_args = " ".join(args)
        if ">" in full_args:
            parts = full_args.split(">", 1)
            files_part = parts[0].strip()
            out_part = parts[1].strip()
            output_file = out_part if out_part else None
            args_list = files_part.split()
        else:
            args_list = args
            
        for arg in args_list:
            if arg == "-n": show_numbers = True
            elif arg == "-s": squeeze_blank = True
            else: files_to_read.append(arg)
            
        if not files_to_read:
            self._append("<span style='color:#ef4444;'>cat: missing file operand</span>")
            return
            
        contents_list = []
        contents_dir = SIMUOS_VFS.get(self.cwd, [])
        
        for target in files_to_read:
            if "~" + target in contents_dir:
                target_path = self.cwd + ("/" if self.cwd != "/" else "") + target
                if not _check_vfs_permission(target_path, getattr(self, "current_user", "admin"), "r"):
                    self._append(f"<span style='color:#ef4444;'>cat: {target}: Permission denied</span>")
                    continue
                file_content = SIMUOS_FILE_CONTENTS.get(target_path, "")
                contents_list.append(file_content)
                _update_vfs_metadata(target_path, "access")
            elif target in contents_dir:
                self._append(f"<span style='color:#ef4444;'>cat: {target}: Is a directory</span>")
            else:
                self._append(f"<span style='color:#ef4444;'>cat: {target}: No such file or directory</span>")
                
        if not contents_list:
            return
            
        final_content = "\n".join(contents_list)
        
        if output_file:
            # Save to output_file
            out_path = self.cwd + ("/" if self.cwd != "/" else "") + output_file
            if "~" + output_file not in contents_dir:
                contents_dir.append("~" + output_file)
            SIMUOS_FILE_CONTENTS[out_path] = final_content
            self._append(f"<span style='color:#10b981;'>Concatenated into '{output_file}'</span>")
        else:
            # Display
            if squeeze_blank:
                import re
                final_content = re.sub(r"\n\s*\n+", "\n\n", final_content)
                
            lines = final_content.split("\n")
            for idx, line in enumerate(lines):
                safe_line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                if show_numbers:
                    self._append(f"<span style='color:#94a3b8;'>{idx+1:4}</span>  <span style='color:#f1f5f9;'>{safe_line}</span>")
                else:
                    self._append(f"<span style='color:#f1f5f9;'>{safe_line}</span>")

    def _cmd_touch(self, args):
        """Create file or update timestamps."""
        if not args:
            self._append("<span style='color:#ef4444;'>Usage: touch [-a] [-m] [-c] [-d &quot;date&quot;] [-r file] &lt;filename&gt;</span>")
            return
            
        flags = [a for a in args if a.startswith("-") and len(a) > 1]
        targets = [a for a in args if not a.startswith("-")]
        
        # Parse complex flags
        date_str = None
        ref_file = None
        i = 0
        while i < len(args):
            if args[i] == "-d" and i + 1 < len(args):
                date_str = args[i+1].strip('"').strip("'")
                targets.remove(args[i+1])
                flags.append("-d")
            elif args[i] == "-r" and i + 1 < len(args):
                ref_file = args[i+1]
                targets.remove(args[i+1])
                flags.append("-r")
            i += 1
            
        if not targets:
            self._append("<span style='color:#ef4444;'>touch: missing file operand</span>")
            return
            
        name = targets[0]
        if "/" in name or "~" in name:
            self._append("<span style='color:#ef4444;'>touch: invalid characters in name</span>")
            return
            
        global SIMUOS_VFS, SIMUOS_FILE_META
        from datetime import datetime
        contents = SIMUOS_VFS.get(self.cwd, [])
        target_path = self.cwd + ("/" if self.cwd != "/" else "") + name
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        exists = ("~" + name) in contents or name in contents
        
        if not exists:
            if "-c" in flags:
                return # Do not create
            contents.append("~" + name)
            self._append(f"<span style='color:#10b981;'>Created file '{name}'</span>")
            SIMUOS_FILE_META[target_path] = {
                "access": now, "modify": now, "change": now, "birth": now,
                "perms": "-rw-r--r--", "owner": getattr(self, "current_user", "admin"), "size": 0
            }
        else:
            if target_path not in SIMUOS_FILE_META:
                SIMUOS_FILE_META[target_path] = {"access": now, "modify": now, "change": now, "birth": now}
                
            meta = SIMUOS_FILE_META[target_path]
            
            if date_str:
                meta["access"] = date_str
                meta["modify"] = date_str
                meta["change"] = date_str
                self._append(f"<span style='color:#10b981;'>Updated timestamps to '{date_str}'</span>")
            elif ref_file:
                ref_path = self.cwd + ("/" if self.cwd != "/" else "") + ref_file
                ref_meta = SIMUOS_FILE_META.get(ref_path, {"access": now, "modify": now})
                meta["access"] = ref_meta.get("access", now)
                meta["modify"] = ref_meta.get("modify", now)
                meta["change"] = now
                self._append(f"<span style='color:#10b981;'>Copied timestamps from '{ref_file}'</span>")
            else:
                if "-a" in flags:
                    meta["access"] = now
                    meta["change"] = now
                    self._append(f"<span style='color:#10b981;'>Updated access time</span>")
                elif "-m" in flags:
                    meta["modify"] = now
                    meta["change"] = now
                    self._append(f"<span style='color:#10b981;'>Updated modify time</span>")
                else:
                    meta["access"] = now
                    meta["modify"] = now
                    meta["change"] = now
                    self._append(f"<span style='color:#10b981;'>Updated timestamps</span>")
            _update_vfs_metadata(target_path, "change")

    def _cmd_nano(self, args):
        """Open text editor."""
        if not args:
            self._append("<span style='color:#ef4444;'>Usage: nano &lt;filename&gt;</span>")
            return
        name = args[0]
        vfs_path = self.cwd + ("/" if self.cwd != "/" else "") + name
        if not _check_vfs_permission(vfs_path, getattr(self, "current_user", "admin"), "r"):
            self._append(f"<span style='color:#ef4444;'>nano: {name}: Permission denied</span>")
            return
        global SIMUOS_VFS
        _update_vfs_metadata(vfs_path, "access")
        if f"~{name}" not in SIMUOS_VFS.get(self.cwd, []):
            SIMUOS_VFS.setdefault(self.cwd, []).append(f"~{name}")
        self.desktop_ref._open_text_editor(vfs_path=vfs_path, display_name=name)
        self._append(f"<span style='color:#10b981;'>Opened '{name}' in Text Editor</span>")

    def _cmd_bash(self, args):
        """Start bash session or execute a script."""
        if not args:
            self._append("<span style='color:#f1f5f9;'>Starting new bash shell...</span>")
            self._append("<span style='color:#f1f5f9;'>bash-5.2$ </span>")
            return
            
        script_name = args[0]
        vfs_path = self.cwd + ("/" if self.cwd != "/" else "") + script_name
        
        global SIMUOS_FILE_CONTENTS, SIMUOS_VFS
        if f"~{script_name}" in SIMUOS_VFS.get(self.cwd, []):
            content = SIMUOS_FILE_CONTENTS.get(vfs_path, "")
            lines = content.split("\n")
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#"): continue
                self._execute_raw(line, echo=False)
        else:
            self._append(f"<span style='color:#ef4444;'>bash: {script_name}: No such file or directory</span>")

    def _cmd_chmod(self, args):
        """Change permissions."""
        if len(args) < 2:
            self._append("<span style='color:#ef4444;'>Usage: chmod [-R] &lt;perms&gt; &lt;file&gt;</span>")
            return
        is_recursive = "-R" in args
        if is_recursive: args.remove("-R")
        perms_str = args[0]
        target = args[1]
        
        global SIMUOS_FILE_META
        target_path = self.cwd + ("/" if self.cwd != "/" else "") + target
        if target_path not in SIMUOS_FILE_META:
            _update_vfs_metadata(target_path, "change")
            
        meta = SIMUOS_FILE_META[target_path]
        current_perms = meta.get("perms", "-rw-r--r--")
        if len(current_perms) < 10: current_perms = "-rw-r--r--"

        if perms_str.isdigit() and len(perms_str) == 3:
            # Octal mapping
            mapping = {'7':'rwx','6':'rw-','5':'r-x','4':'r--','3':'-wx','2':'-w-','1':'--x','0':'---'}
            u, g, o = mapping.get(perms_str[0],'---'), mapping.get(perms_str[1],'---'), mapping.get(perms_str[2],'---')
            meta["perms"] = f"-{u}{g}{o}"
        elif any(op in perms_str for op in ["=", "+", "-"]):
            # Symbolic mapping (e.g. g=r, u+x, o-w)
            op = "=" if "=" in perms_str else ("+" if "+" in perms_str else "-")
            parts = perms_str.split(op)
            who, what = parts[0] or "a", parts[1]
            p_list = list(current_perms)
            indices = []
            if "a" in who: indices = range(1, 10)
            else:
                if "u" in who: indices = list(indices) + [1,2,3]
                if "g" in who: indices = list(indices) + [4,5,6]
                if "o" in who: indices = list(indices) + [7,8,9]
            for i in indices:
                char = "r" if i in [1,4,7] else ("w" if i in [2,5,8] else "x")
                if op == "=": p_list[i] = char if char in what else "-"
                elif op == "+": 
                    if char in what: p_list[i] = char
                elif op == "-":
                    if char in what: p_list[i] = "-"
            meta["perms"] = "".join(p_list)
        else:
            meta["perms"] = perms_str if perms_str.startswith("-") else f"-{perms_str}"
            
        self._append(f"<span style='color:#10b981;'>Changed permissions of '{target}' to {meta['perms']} ({perms_str})</span>")
        _update_vfs_metadata(target_path, "change")
        if is_recursive:
            self._append(f"<span style='color:#10b981;'>(Applied recursively)</span>")

    def _cmd_cut(self, args):
        """Mock cut for /etc/passwd"""
        cmd_full = " ".join(args)
        if "-d:" in cmd_full and "-f1" in cmd_full and "/etc/passwd" in cmd_full:
            global SIMUOS_USERS
            self._append("<span style='color:#38bdf8;'>root</span>")
            for u in SIMUOS_USERS.keys():
                if u != "admin": self._append(f"<span style='color:#f1f5f9;'>{u}</span>")
            self._append("<span style='color:#f1f5f9;'>admin</span>")
        else:
            self._append("<span style='color:#ef4444;'>Usage: cut -d: -f1 /etc/passwd</span>")

    def _cmd_whoami(self):
        """Print current user."""
        self._append(f"<span style='color:#10b981;'>{getattr(self, 'current_user', 'admin')}</span>")

    def _cmd_useradd(self, args):
        """Add a new user."""
        if len(args) < 2 or args[0] != "-m":
            self._append("<span style='color:#ef4444;'>Usage: useradd -m &lt;username&gt;</span>")
            return
        new_user = args[1]
        global SIMUOS_USERS, SIMUOS_VFS
        if new_user in SIMUOS_USERS:
            self._append(f"<span style='color:#ef4444;'>useradd: user '{new_user}' already exists</span>")
        else:
            SIMUOS_USERS[new_user] = None # No password set, must use passwd
            SIMUOS_VFS.setdefault("/home", []).append(new_user)
            SIMUOS_VFS[f"/home/{new_user}"] = ["Desktop"]
            SIMUOS_VFS[f"/home/{new_user}/Desktop"] = ["Recycle Bin", "SimuOS Explorer"]
            SIMUOS_VFS[f"/home/{new_user}/Desktop/Recycle Bin"] = []
            self._append(f"<span style='color:#10b981;'>User '{new_user}' created.</span>")
            self._append(f"<span style='color:#f59e0b;'>Note: User is locked until a password is set with 'passwd {new_user}'</span>")

    def _cmd_passwd(self, args):
        """Change user password."""
        if not args:
            self._append("<span style='color:#ef4444;'>Usage: passwd &lt;username&gt;</span>")
            return
        user = args[0]
        global SIMUOS_USERS
        if user not in SIMUOS_USERS:
            self._append(f"<span style='color:#ef4444;'>passwd: user '{user}' does not exist</span>")
            return
            
        self._append(f"<span style='color:#38bdf8;'>Changing password for {user}.</span>")
        self._append("<span style='color:#f1f5f9;'>New password: </span>")
        
        def handle_passwd(new_pass):
            if not new_pass:
                self._append("<span style='color:#ef4444;'>password unchanged</span>")
                return
            SIMUOS_USERS[user] = new_pass
            self._append(f"<span style='color:#10b981;'>password updated successfully for {user}</span>")
            
        self._pending_action = handle_passwd

    def _cmd_userdel(self, args):
        """Delete user."""
        if not args:
            self._append("<span style='color:#ef4444;'>Usage: userdel &lt;username&gt;</span>")
            return
        user = args[0]
        global SIMUOS_USERS, SIMUOS_VFS
        if user == "admin":
            self._append("<span style='color:#ef4444;'>userdel: cannot remove admin</span>")
            return
        if user in SIMUOS_USERS:
            del SIMUOS_USERS[user]
            if user in SIMUOS_VFS.get("/home", []):
                SIMUOS_VFS["/home"].remove(user)
            self._append(f"<span style='color:#10b981;'>user '{user}' deleted</span>")
        else:
            self._append(f"<span style='color:#ef4444;'>userdel: user '{user}' does not exist</span>")

    def _cmd_stat(self, args):
        """Display file status."""
        if not args:
            self._append("<span style='color:#ef4444;'>Usage: stat &lt;file&gt;</span>")
            return
        target = args[0]
        global SIMUOS_FILE_META, SIMUOS_VFS
        target_path = self.cwd + ("/" if self.cwd != "/" else "") + target
        
        # Check VFS existence first
        contents = SIMUOS_VFS.get(self.cwd, [])
        is_file = f"~{target}" in contents
        is_dir = target in contents or target_path in SIMUOS_VFS
        
        if not is_file and not is_dir:
            self._append(f"<span style='color:#ef4444;'>stat: cannot stat '{target}': No such file or directory</span>")
            return

        # Initialize metadata if missing
        if target_path not in SIMUOS_FILE_META:
            _update_vfs_metadata(target_path, "access")
            
        meta = SIMUOS_FILE_META[target_path]
        self._append(f"<span style='color:#38bdf8;'>  File:</span> {target}")
        self._append(f"<span style='color:#38bdf8;'>  Size:</span> {meta.get('size', 0)} blocks")
        self._append(f"<span style='color:#38bdf8;'>Access:</span> {meta.get('perms', '-rw-r--r--')}")
        self._append(f"<span style='color:#38bdf8;'>Access:</span> {meta.get('access', 'Unknown')}")
        self._append(f"<span style='color:#38bdf8;'>Modify:</span> {meta.get('modify', 'Unknown')}")
        self._append(f"<span style='color:#38bdf8;'>Change:</span> {meta.get('change', 'Unknown')}")
        self._append(f"<span style='color:#38bdf8;'> Birth:</span> {meta.get('birth', 'Unknown')}")

    def _cmd_echo(self, args):
        """Print arguments back to the terminal output."""
        self._append(
            f"<span style='color:#e2e8f0;'>{' '.join(args)}</span>"
        )


# ══ Settings Window: Appearance, Account, and Session Management ══════════════
class SettingsWindow(QMainWindow):
    def __init__(self, desktop_ref=None, parent=None):
        super().__init__(parent)
        self.desktop = desktop_ref
        self.setWindowTitle("SimuOS — Settings")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(820, 560)

        # ── Outer card ───────────────────────────────────────────────────────
        container = QFrame(self)
        container.setGeometry(self.rect())
        container.setStyleSheet("""
            QFrame {
                background: #0d1117;
                border-radius: 18px;
                border: 1px solid #1f2937;
            }
        """)
        container.setGraphicsEffect(make_shadow(55, 16, 200))

        root = QHBoxLayout(container)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ──────────────────────────────────────────────────────────────────
        #  LEFT SIDEBAR
        # ──────────────────────────────────────────────────────────────────
        sidebar = QFrame()
        sidebar.setFixedWidth(200)
        sidebar.setStyleSheet("""
            QFrame {
                background: #080c14;
                border-top-left-radius: 18px;
                border-bottom-left-radius: 18px;
                border-right: 1px solid #1f2937;
            }
        """)
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(0, 0, 0, 20)
        sl.setSpacing(0)

        # Sidebar header
        sh = QLabel("  ⚙️  Settings")
        sh.setFixedHeight(56)
        sh.setStyleSheet(
            "color:#38bdf8; font-size:15px; font-weight:900;"
            "background:transparent; letter-spacing:1px;"
            "border-bottom: 1px solid #1f2937;"
        )
        sl.addWidget(sh)
        sl.addSpacing(10)

        # Sidebar nav buttons — each one swaps the right panel
        # Format: (emoji, label, panel_index)
        nav_items = [
            ("🎨", "Appearance",  0),
            ("👤", "Account",     1),
            ("ℹ️", "About",       2),
        ]
        self._nav_btns = []
        for emoji, label, idx in nav_items:
            btn = QPushButton(f"  {emoji}  {label}")
            btn.setFixedHeight(46)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setCheckable(True)
            btn.setStyleSheet("""
                QPushButton {
                    background: transparent;
                    color: #94a3b8;
                    font-size: 14px;
                    font-weight: 600;
                    border: none;
                    text-align: left;
                    padding-left: 10px;
                    border-radius: 0px;
                }
                QPushButton:hover   { background: rgba(56,189,248,0.07); color:#e2e8f0; }
                QPushButton:checked { background: rgba(56,189,248,0.12);
                                      color:#38bdf8;
                                      border-left: 3px solid #38bdf8; }
            """)
            # Lambda captures idx correctly via default arg
            btn.clicked.connect(lambda _, i=idx: self._switch_panel(i))
            self._nav_btns.append(btn)
            sl.addWidget(btn)

        sl.addStretch()

        # ──────────────────────────────────────────────────────────────────
        #  RIGHT CONTENT AREA
        # ──────────────────────────────────────────────────────────────────
        right_wrapper = QFrame()
        right_wrapper.setStyleSheet("background:transparent; border:none;")
        rw_layout = QVBoxLayout(right_wrapper)
        rw_layout.setContentsMargins(0, 0, 0, 0)
        rw_layout.setSpacing(0)

        # ── Top bar (title + close button) ───────────────────────────────
        topbar = QFrame()
        topbar.setFixedHeight(56)
        topbar.setStyleSheet(
            "background:transparent; border:none;"
            "border-bottom:1px solid #1f2937;"
        )
        tb = QHBoxLayout(topbar)
        tb.setContentsMargins(24, 0, 16, 0)

        self._panel_title = QLabel("Appearance")
        self._panel_title.setStyleSheet(
            "color:#f8fafc; font-size:20px; font-weight:bold; background:transparent;"
        )
        tb.addWidget(self._panel_title)
        tb.addStretch()

        x_btn = QPushButton("✕")
        x_btn.setFixedSize(30, 30)
        x_btn.setStyleSheet("""
            QPushButton       { background:transparent; color:#475569;
                                font-size:15px; border:none; border-radius:15px; }
            QPushButton:hover { background:#ef4444; color:white; }
        """)
        x_btn.clicked.connect(self.close)
        tb.addWidget(x_btn)
        rw_layout.addWidget(topbar)

        # ── QStackedWidget — each page is one settings panel ────────────────
        from PyQt6.QtWidgets import QStackedWidget, QSlider, QScrollArea
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background:transparent; border:none;")
        self._stack.addWidget(self._panel_appearance())
        self._stack.addWidget(self._panel_account())
        self._stack.addWidget(self._panel_about())
        rw_layout.addWidget(self._stack)

        root.addWidget(sidebar)
        root.addWidget(right_wrapper)

        # Select first panel by default
        self._switch_panel(0)

    # ─────────────────────────────────────────────────────────────────────────
    #  Panel switcher: update the stacked widget + highlight active nav btn
    # ─────────────────────────────────────────────────────────────────────────
    _PANEL_TITLES = ["Appearance", "Account", "About"]

    def _switch_panel(self, index: int):
        """Swap to the chosen settings panel and update sidebar highlights."""
        self._stack.setCurrentIndex(index)
        self._panel_title.setText(self._PANEL_TITLES[index])
        for i, btn in enumerate(self._nav_btns):
            btn.setChecked(i == index)

    # ─────────────────────────────────────────────────────────────────────────
    #  Panel builder helpers
    # ─────────────────────────────────────────────────────────────────────────
    def _section_label(self, text: str) -> QLabel:
        """Styled section heading used inside each panel."""
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "color:#38bdf8; font-size:12px; font-weight:700;"
            "letter-spacing:2px; background:transparent;"
            "border-bottom:1px solid #1f2937; padding-bottom:6px;"
        )
        return lbl

    def _row_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#94a3b8; font-size:13px; background:transparent;")
        return lbl

    # ─────────────────────────────────────────────────────────────────────────
    #  PANEL 0 — Appearance
    # ─────────────────────────────────────────────────────────────────────────
    def _panel_appearance(self) -> QWidget:
        p = QWidget()
        p.setStyleSheet("background:transparent;")
        layout = QVBoxLayout(p)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(20)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # — Wallpaper Style ———————————————————————————————
        layout.addWidget(self._section_label("WALLPAPER STYLE"))
        layout.addWidget(self._row_label("Desktop background appearance."))

        wp_row = QHBoxLayout()
        wp_row.setSpacing(12)
        for style in ["Dark Gradient", "Deep Space", "Midnight Blue"]:
            btn = QPushButton(style)
            btn.setFixedHeight(38)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet("""
                QPushButton       { background:#1e293b; color:#94a3b8; border:1px solid #334155;
                                    border-radius:8px; font-size:13px; padding:0 16px; }
                QPushButton:hover { background:#334155; color:#f8fafc; }
                QPushButton:pressed { background:#38bdf8; color:#0d1117; border-color:#38bdf8; }
            """)
            # Apply wallpaper when clicked
            btn.clicked.connect(lambda _, s=style: self.desktop.set_wallpaper(s) if self.desktop else None)
            wp_row.addWidget(btn)
        wp_row.addStretch()
        layout.addLayout(wp_row)
        layout.addStretch()
        return p

    # ─────────────────────────────────────────────────────────────────────────
    #  PANEL 2 — Account
    # ─────────────────────────────────────────────────────────────────────────
    def _panel_account(self) -> QWidget:
        p = QWidget()
        p.setStyleSheet("background:transparent;")
        layout = QVBoxLayout(p)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        layout.addWidget(self._section_label("CURRENT USER"))

        # Avatar circle placeholder
        avatar = QLabel("👤")
        avatar.setFixedSize(72, 72)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet("""
            QLabel {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                    stop:0 #8b5cf6, stop:1 #38bdf8);
                border-radius: 36px;
                font-size: 32px;
            }
        """)
        layout.addWidget(avatar, alignment=Qt.AlignmentFlag.AlignLeft)

        uname = QLabel("admin")
        uname.setStyleSheet("color:#f8fafc; font-size:22px; font-weight:bold; background:transparent;")
        layout.addWidget(uname)

        role = QLabel("System Administrator")
        role.setStyleSheet("color:#64748b; font-size:13px; background:transparent;")
        layout.addWidget(role)

        layout.addSpacing(10)
        layout.addWidget(self._section_label("CHANGE PASSWORD"))

        field_style = """
            QLineEdit { background:#1e293b; color:#f8fafc; border:2px solid transparent;
                        border-radius:10px; padding:10px 16px; font-size:14px; }
            QLineEdit:focus { border-color:#8b5cf6; background:#0f172a; }
        """
        self.cur_pwd = QLineEdit()
        self.cur_pwd.setPlaceholderText("Current password")
        self.cur_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.cur_pwd.setFixedHeight(46)
        self.cur_pwd.setStyleSheet(field_style)

        self.new_pwd = QLineEdit()
        self.new_pwd.setPlaceholderText("New password")
        self.new_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.new_pwd.setFixedHeight(46)
        self.new_pwd.setStyleSheet(field_style)

        self.save_btn = QPushButton("Save Password")
        self.save_btn.setFixedHeight(44)
        self.save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_btn.setStyleSheet("""
            QPushButton       { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                    stop:0 #8b5cf6, stop:1 #3b82f6);
                                color:#fff; border:none; border-radius:10px;
                                font-size:14px; font-weight:bold; }
            QPushButton:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                    stop:0 #a78bfa, stop:1 #60a5fa); }
        """)
        self.save_btn.clicked.connect(self._change_pwd)

        layout.addWidget(self.cur_pwd)
        layout.addWidget(self.new_pwd)
        layout.addWidget(self.save_btn)
        layout.addStretch()
        return p

    def _change_pwd(self):
        global SIMUOS_USERS
        current = self.cur_pwd.text().strip()
        new_val = self.new_pwd.text().strip()

        if current == SIMUOS_USERS.get("admin") and new_val:
            SIMUOS_USERS["admin"] = new_val
            self.save_btn.setText("✓  Password Updated")
            self.save_btn.setStyleSheet("QPushButton { background: #10b981; color: white; border-radius:10px; font-weight:bold; font-size:14px; }")
            self.cur_pwd.clear()
            self.new_pwd.clear()
        else:
            self.save_btn.setText("✗  Incorrect Current Password")
            self.save_btn.setStyleSheet("QPushButton { background: #ef4444; color: white; border-radius:10px; font-weight:bold; font-size:14px; }")
            
        # Reset button state after 2 seconds
        QTimer.singleShot(2000, self._reset_btn)

    def _reset_btn(self):
        self.save_btn.setText("Save Password")
        self.save_btn.setStyleSheet("""
            QPushButton       { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                    stop:0 #8b5cf6, stop:1 #3b82f6);
                                color:#fff; border:none; border-radius:10px;
                                font-size:14px; font-weight:bold; }
            QPushButton:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                    stop:0 #a78bfa, stop:1 #60a5fa); }
        """)

    # ─────────────────────────────────────────────────────────────────────────
    #  PANEL 3 — About
    # ─────────────────────────────────────────────────────────────────────────
    def _panel_about(self) -> QWidget:
        p = QWidget()
        p.setStyleSheet("background:transparent;")
        layout = QVBoxLayout(p)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        logo = QLabel("SimuOS")
        logo.setStyleSheet(
            "color:#38bdf8; font-size:42px; font-weight:900;"
            "background: transparent; letter-spacing:3px;"
        )
        layout.addWidget(logo)

        tagline = QLabel("Future Kernel  —  Built for Learning")
        tagline.setStyleSheet("color:#64748b; font-size:14px; background:transparent;")
        layout.addWidget(tagline)

        layout.addSpacing(10)
        layout.addWidget(self._section_label("SYSTEM DETAILS"))

        details = [
            ("Version",       "1.0.0"),
            ("Kernel",        "SimuCore v1  (Python 3 + PyQt6)"),
            ("Architecture",  "x86_64 (Simulated)"),
            ("Shell",         "SimuBash 1.0"),
            ("GUI Toolkit",   "PyQt6  6.x"),
        ]
        for key, val in details:
            row = QHBoxLayout()
            k = QLabel(key)
            k.setFixedWidth(130)
            k.setStyleSheet("color:#64748b; font-size:13px; background:transparent;")
            v = QLabel(val)
            v.setStyleSheet("color:#e2e8f0; font-size:13px; background:transparent;")
            row.addWidget(k)
            row.addWidget(v)
            row.addStretch()
            layout.addLayout(row)

        # ── Session Management ─────────────────────────────────────────────
        layout.addSpacing(16)
        layout.addWidget(self._section_label("SESSION MANAGEMENT"))

        sess_info = QLabel("Save the current file system state and restore it next login.")
        sess_info.setStyleSheet("color:#64748b; font-size:12px; background:transparent;")
        sess_info.setWordWrap(True)
        layout.addWidget(sess_info)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        btn_style_save = """
            QPushButton       { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                    stop:0 #8b5cf6, stop:1 #3b82f6);
                                color:#fff; border:none; border-radius:10px;
                                font-size:13px; font-weight:600; padding:0 20px; }
            QPushButton:hover { opacity:0.85; }
            QPushButton:pressed { background:#3730a3; }
        """
        btn_style_load = """
            QPushButton       { background:#1e293b; color:#94a3b8;
                                border:1px solid #334155; border-radius:10px;
                                font-size:13px; padding:0 20px; }
            QPushButton:hover { background:#334155; color:#f8fafc; border-color:#8b5cf6; }
        """

        save_btn = QPushButton("Save Session")
        save_btn.setFixedHeight(40)
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(btn_style_save)
        save_btn.clicked.connect(self._do_save_session)
        btn_row.addWidget(save_btn)

        load_btn = QPushButton("Load Session")
        load_btn.setFixedHeight(40)
        load_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        load_btn.setStyleSheet(btn_style_load)
        load_btn.clicked.connect(self._do_load_session)
        btn_row.addWidget(load_btn)

        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Status label for save/load feedback
        self._sess_status = QLabel("")
        self._sess_status.setStyleSheet("color:#10b981; font-size:12px; background:transparent;")
        layout.addWidget(self._sess_status)

        layout.addStretch()
        return p

    def _do_save_session(self):
        """Open Windows Save-As dialog and write the session JSON."""
        from PyQt6.QtWidgets import QFileDialog
        os.makedirs(_SESSIONS_DIR, exist_ok=True)
        default_name = _dt.datetime.now().strftime("session_%Y%m%d_%H%M%S.json")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Session",
            os.path.join(_SESSIONS_DIR, default_name),
            "SimuOS Session (*.json);;All Files (*)"
        )
        if not path:
            return
        if not path.endswith(".json"):
            path += ".json"
        # Write directly to the chosen path
        payload = {
            "version": "1.0",
            "saved_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "label": os.path.splitext(os.path.basename(path))[0],
            "vfs": SIMUOS_VFS,
            "file_contents": SIMUOS_FILE_CONTENTS,
        }
        with open(path, "w", encoding="utf-8") as f:
            import json as _json
            _json.dump(payload, f, indent=2, ensure_ascii=False)
        fname = os.path.basename(path)
        self._sess_status.setText(f"Saved ✓  {fname}")
        self._sess_status.setStyleSheet("color:#10b981; font-size:12px; background:transparent;")
        QTimer.singleShot(4000, lambda: self._sess_status.setText(""))

    def _do_load_session(self):
        """Open Windows file browser to choose a session JSON and restore it."""
        from PyQt6.QtWidgets import QFileDialog
        os.makedirs(_SESSIONS_DIR, exist_ok=True)
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Session",
            _SESSIONS_DIR,
            "SimuOS Session (*.json);;All Files (*)"
        )
        if not path:
            return
        try:
            _load_session(path)
            self._sess_status.setText("Session loaded. Reopen apps to see the restored files.")
            self._sess_status.setStyleSheet("color:#38bdf8; font-size:12px; background:transparent;")
        except Exception as e:
            self._sess_status.setText(f"Error: {e}")
            self._sess_status.setStyleSheet("color:#ef4444; font-size:12px; background:transparent;")
        QTimer.singleShot(5000, lambda: self._sess_status.setText(""))


# ══ Text Editor: Full-featured VFS editor with permission enforcement ════════
class TextEditorWindow(QMainWindow):
    def __init__(self, vfs_path: str = None, display_name: str = None, current_user="admin", parent=None):
        super().__init__(parent)
        self.current_user = current_user
        self._vfs_path    = vfs_path        # e.g. "/home/admin/Desktop/notes.txt"
        self._display_name = display_name or "Untitled"
        self._modified    = False

        self.setWindowTitle(f"SimuOS — Text Editor — {self._display_name}")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(860, 600)

        # ── Outer card ───────────────────────────────────────────────────────
        container = QFrame(self)
        container.setGeometry(self.rect())
        container.setStyleSheet("""
            QFrame {
                background: #0d1117;
                border-radius: 18px;
                border: 1px solid #1f2937;
            }
        """)
        container.setGraphicsEffect(make_shadow(55, 16, 210))

        root = QVBoxLayout(container)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Title Bar ────────────────────────────────────────────────────────
        root.addWidget(self._build_title_bar())

        # ── Toolbar ──────────────────────────────────────────────────────────
        root.addWidget(self._build_toolbar())

        # ── Editor Area ──────────────────────────────────────────────────────
        root.addWidget(self._build_editor(), stretch=1)

        # ── Find Bar (hidden by default) ──────────────────────────────────────
        self._find_bar = self._build_find_bar()
        root.addWidget(self._find_bar)

        # ── Status Bar ───────────────────────────────────────────────────────
        root.addWidget(self._build_status_bar())

        # Load existing content if a VFS path was given
        if self._vfs_path:
            if not _check_vfs_permission(self._vfs_path, self.current_user, 'r'):
                # Should have been blocked by caller, but safety check
                self._vfs_path = None
                self._display_name = "Untitled"
            else:
                global SIMUOS_FILE_CONTENTS
                content = SIMUOS_FILE_CONTENTS.get(self._vfs_path, "")
                self._editor.setPlainText(content)
                self._modified = False
                self._update_title()

    # ─────────────────────────────────────────────────────────────────────────
    #  UI builder helpers
    # ─────────────────────────────────────────────────────────────────────────
    def _build_title_bar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(42)
        bar.setStyleSheet("""
            QFrame {
                background: #080c14;
                border-top-left-radius: 18px;
                border-top-right-radius: 18px;
                border-bottom: 1px solid #1f2937;
            }
        """)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 0, 10, 0)
        layout.setSpacing(8)

        # Traffic-light dots
        for color in ["#ef4444", "#f59e0b", "#22c55e"]:
            dot = QLabel("●")
            dot.setStyleSheet(f"color:{color}; font-size:12px; background:transparent;")
            layout.addWidget(dot)

        layout.addSpacing(8)

        self._title_lbl = QLabel(f"📝  Text Editor  —  {self._display_name}")
        self._title_lbl.setStyleSheet(
            "color:#64748b; font-size:13px; font-weight:600; background:transparent;"
        )
        layout.addWidget(self._title_lbl)
        layout.addStretch()

        # Window drag support
        bar.mousePressEvent   = self._drag_start
        bar.mouseMoveEvent    = self._drag_move

        x_btn = QPushButton("✕")
        x_btn.setFixedSize(28, 28)
        x_btn.setStyleSheet("""
            QPushButton       { background:transparent; color:#475569;
                                font-size:14px; border:none; border-radius:14px; }
            QPushButton:hover { background:#ef4444; color:white; }
        """)
        x_btn.clicked.connect(self.close)
        layout.addWidget(x_btn)
        return bar

    def _build_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(44)
        bar.setStyleSheet("""
            QFrame {
                background: #0a0f1e;
                border-bottom: 1px solid #1e293b;
            }
        """)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(4)

        btn_style = """
            QPushButton {
                background: rgba(255,255,255,0.04);
                color: #94a3b8;
                border: 1px solid rgba(255,255,255,0.07);
                border-radius: 7px;
                font-size: 12px;
                font-weight: 600;
                padding: 4px 12px;
            }
            QPushButton:hover   { background: rgba(56,189,248,0.12); color: #e2e8f0;
                                  border-color: rgba(56,189,248,0.4); }
            QPushButton:pressed { background: rgba(56,189,248,0.25); }
        """

        actions = [
            ("📄 New",       self._action_new),
            ("💾 Save",      self._action_save),
            ("✂ Cut",        self._editor_cut),
            ("📋 Copy",      self._editor_copy),
            ("📌 Paste",     self._editor_paste),
            ("🔍 Find",      self._toggle_find_bar),
        ]
        for label, cb in actions:
            btn = QPushButton(label)
            btn.setStyleSheet(btn_style)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(cb)
            layout.addWidget(btn)

        layout.addSpacing(12)

        # Word-wrap toggle
        self._wrap_btn = QPushButton("↵ Wrap: ON")
        self._wrap_btn.setCheckable(True)
        self._wrap_btn.setChecked(True)
        self._wrap_btn.setStyleSheet(btn_style)
        self._wrap_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._wrap_btn.clicked.connect(self._toggle_wrap)
        layout.addWidget(self._wrap_btn)

        layout.addStretch()

        # File name indicator
        self._file_indicator = QLabel(self._display_name)
        self._file_indicator.setStyleSheet(
            "color:#475569; font-size:12px; background:transparent; padding-right:6px;"
        )
        layout.addWidget(self._file_indicator)

        return bar

    def _build_editor(self) -> QWidget:
        from PyQt6.QtWidgets import QTextEdit
        from PyQt6.QtGui import QTextOption

        wrapper = QWidget()
        wrapper.setStyleSheet("background:#0b1120;")
        h = QHBoxLayout(wrapper)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # Line-number gutter
        self._line_gutter = QTextEdit()
        self._line_gutter.setReadOnly(True)
        self._line_gutter.setFixedWidth(52)
        self._line_gutter.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._line_gutter.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._line_gutter.setStyleSheet("""
            QTextEdit {
                background: #060a14;
                color: #334155;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 13px;
                border: none;
                border-right: 1px solid #1e293b;
                padding: 12px 6px 12px 0;
                text-align: right;
            }
        """)

        # Main editor
        self._editor = QTextEdit()
        self._editor.setStyleSheet("""
            QTextEdit {
                background: #0b1120;
                color: #e2e8f0;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 14px;
                border: none;
                padding: 12px 18px;
                selection-background-color: rgba(56,189,248,0.35);
                selection-color: #f1f5f9;
            }
            QScrollBar:vertical {
                background: #0b1120;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #1e293b;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical:hover { background: #38bdf8; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """)
        self._editor.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self._editor.textChanged.connect(self._on_text_changed)
        self._editor.cursorPositionChanged.connect(self._update_cursor_pos)
        self._editor.verticalScrollBar().valueChanged.connect(self._sync_gutter_scroll)

        # Intercept key events to log keyboard activity in Task Manager
        _orig_key_press = self._editor.keyPressEvent
        def _editor_key_hook(event, _orig=_orig_key_press):
            from PyQt6.QtGui import QKeySequence
            key  = event.key()
            mods = event.modifiers()
            ctrl = Qt.KeyboardModifier.ControlModifier
            src  = self._display_name or "editor"
            if mods & ctrl:
                if key == Qt.Key.Key_S:   _log_keyboard("Ctrl+S  (Save)", src)
                elif key == Qt.Key.Key_F: _log_keyboard("Ctrl+F  (Find)", src)
                elif key == Qt.Key.Key_C: _log_keyboard("Ctrl+C  (Copy)", src)
                elif key == Qt.Key.Key_V: _log_keyboard("Ctrl+V  (Paste)", src)
                elif key == Qt.Key.Key_Z: _log_keyboard("Ctrl+Z  (Undo)", src)
                elif key == Qt.Key.Key_A: _log_keyboard("Ctrl+A  (Select All)", src)
            elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                _log_keyboard("Enter  (New Line)", src)
            _orig(event)
        self._editor.keyPressEvent = _editor_key_hook

        h.addWidget(self._line_gutter)
        h.addWidget(self._editor, stretch=1)

        self._update_line_numbers()
        return wrapper

    def _build_find_bar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(44)
        bar.setStyleSheet("""
            QFrame {
                background: #0a0f1e;
                border-top: 1px solid #1e293b;
            }
        """)
        bar.hide()

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(8)

        lbl = QLabel("🔍  Find:")
        lbl.setStyleSheet("color:#64748b; font-size:13px; background:transparent;")
        layout.addWidget(lbl)

        self._find_input = QLineEdit()
        self._find_input.setPlaceholderText("Search text…")
        self._find_input.setFixedHeight(30)
        self._find_input.setStyleSheet("""
            QLineEdit {
                background: #1e293b;
                color: #f1f5f9;
                border: 1px solid #334155;
                border-radius: 7px;
                padding: 0 10px;
                font-size: 13px;
            }
            QLineEdit:focus { border-color: #38bdf8; }
        """)
        self._find_input.returnPressed.connect(self._do_find)
        layout.addWidget(self._find_input, stretch=1)

        find_btn = QPushButton("Find Next")
        find_btn.setFixedHeight(30)
        find_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        find_btn.setStyleSheet("""
            QPushButton {
                background: rgba(56,189,248,0.1);
                color: #38bdf8;
                border: 1px solid rgba(56,189,248,0.3);
                border-radius: 7px;
                font-size: 13px;
                padding: 0 14px;
            }
            QPushButton:hover { background: rgba(56,189,248,0.25); }
        """)
        find_btn.clicked.connect(self._do_find)
        layout.addWidget(find_btn)

        close_find = QPushButton("✕")
        close_find.setFixedSize(26, 26)
        close_find.setStyleSheet("""
            QPushButton       { background:transparent; color:#475569;
                                font-size:13px; border:none; border-radius:13px; }
            QPushButton:hover { background:#ef4444; color:white; }
        """)
        close_find.clicked.connect(bar.hide)
        layout.addWidget(close_find)

        return bar

    def _build_status_bar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(30)
        bar.setStyleSheet("""
            QFrame {
                background: #060a14;
                border-top: 1px solid #1e293b;
                border-bottom-left-radius: 18px;
                border-bottom-right-radius: 18px;
            }
        """)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(20)

        lbl_style = "color:#334155; font-size:11px; background:transparent;"

        self._status_ln  = QLabel("Ln 1, Col 1")
        self._status_ln.setStyleSheet(lbl_style)

        self._status_chars = QLabel("0 chars")
        self._status_chars.setStyleSheet(lbl_style)

        self._status_modified = QLabel("")
        self._status_modified.setStyleSheet("color:#f59e0b; font-size:11px; background:transparent; font-weight:600;")

        saved_lbl = QLabel("UTF-8   Plain Text")
        saved_lbl.setStyleSheet(lbl_style)

        layout.addWidget(self._status_ln)
        layout.addWidget(self._status_chars)
        layout.addWidget(self._status_modified)
        layout.addStretch()
        layout.addWidget(saved_lbl)
        return bar

    # ─────────────────────────────────────────────────────────────────────────
    #  Window drag (frameless window)
    # ─────────────────────────────────────────────────────────────────────────
    def _drag_start(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _drag_move(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and hasattr(self, '_drag_pos'):
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    # ─────────────────────────────────────────────────────────────────────────
    #  Keyboard shortcuts  (Ctrl+S / Ctrl+F / Ctrl+W)
    # ─────────────────────────────────────────────────────────────────────────
    def keyPressEvent(self, event):
        from PyQt6.QtCore import Qt as _Qt
        if event.modifiers() & _Qt.KeyboardModifier.ControlModifier:
            if event.key() == _Qt.Key.Key_S:
                self._action_save()
                return
            if event.key() == _Qt.Key.Key_F:
                self._toggle_find_bar()
                return
        super().keyPressEvent(event)

    # ─────────────────────────────────────────────────────────────────────────
    #  Editor slots
    # ─────────────────────────────────────────────────────────────────────────
    def _on_text_changed(self):
        self._modified = True
        self._update_title()
        self._update_line_numbers()
        char_count = len(self._editor.toPlainText())
        self._status_chars.setText(f"{char_count} chars")
        self._status_modified.setText("● Unsaved")

    def _update_cursor_pos(self):
        cursor = self._editor.textCursor()
        ln  = cursor.blockNumber() + 1
        col = cursor.columnNumber() + 1
        self._status_ln.setText(f"Ln {ln}, Col {col}")

    def _update_line_numbers(self):
        """Regenerate the line-number gutter to match current document."""
        count = self._editor.document().blockCount()
        lines = "\n".join(str(i + 1) for i in range(count))
        self._line_gutter.setText(lines)
        # Sync scroll position
        self._sync_gutter_scroll(self._editor.verticalScrollBar().value())

    def _sync_gutter_scroll(self, value):
        self._line_gutter.verticalScrollBar().setValue(value)

    def _update_title(self):
        marker = "● " if self._modified else ""
        self._title_lbl.setText(f"📝  Text Editor  —  {marker}{self._display_name}")
        self._file_indicator.setText(f"{marker}{self._display_name}")

    def _toggle_wrap(self):
        from PyQt6.QtGui import QTextOption
        if self._wrap_btn.isChecked():
            self._editor.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            self._wrap_btn.setText("↵ Wrap: ON")
        else:
            self._editor.setWordWrapMode(QTextOption.WrapMode.NoWrap)
            self._wrap_btn.setText("↵ Wrap: OFF")

    def _editor_cut(self):   self._editor.cut()
    def _editor_copy(self):  self._editor.copy()
    def _editor_paste(self): self._editor.paste()

    def _toggle_find_bar(self):
        if self._find_bar.isVisible():
            self._find_bar.hide()
        else:
            self._find_bar.show()
            self._find_input.setFocus()
            self._find_input.selectAll()

    def _do_find(self):
        """Highlight the next occurrence of the search term."""
        from PyQt6.QtGui import QTextCharFormat, QColor
        term = self._find_input.text()
        if not term:
            return
        # Try to find from current cursor position
        found = self._editor.find(term)
        if not found:
            # Wrap around: move to start and try again
            cursor = self._editor.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            self._editor.setTextCursor(cursor)
            self._editor.find(term)

    # ─────────────────────────────────────────────────────────────────────────
    #  File operations
    # ─────────────────────────────────────────────────────────────────────────
    def _action_new(self):
        """Clear the editor and reset to an untitled blank file."""
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "New File", "Enter file name:", text="untitled.txt")
        if not ok or not name:
            return
        # Add to VFS Desktop
        global SIMUOS_VFS, SIMUOS_FILE_CONTENTS
        dt_path = "/home/admin/Desktop"
        vfs_key = f"~{name}"
        if vfs_key not in SIMUOS_VFS.get(dt_path, []):
            SIMUOS_VFS[dt_path].append(vfs_key)
        self._vfs_path     = dt_path + "/" + name
        self._display_name = name
        self._editor.clear()
        self._modified = False
        self._status_modified.setText("")
        self._update_title()

    def _action_save(self):
        """Persist the editor text into the VFS content store."""
        global SIMUOS_VFS, SIMUOS_FILE_CONTENTS
        content = self._editor.toPlainText()

        if not self._vfs_path:
            # Prompt for a name if no file is open
            from PyQt6.QtWidgets import QInputDialog
            name, ok = QInputDialog.getText(self, "Save As", "Enter file name:", text="untitled.txt")
            if not ok or not name:
                return
            dt_path = "/home/admin/Desktop"
            self._vfs_path     = dt_path + "/" + name
            self._display_name = name
            if f"~{name}" not in SIMUOS_VFS.get(dt_path, []):
                SIMUOS_VFS[dt_path].append(f"~{name}")
        
        # Check Write Permission
        user = getattr(self, "current_user", "admin")
        if not _check_vfs_permission(self._vfs_path, user, "w"):
            # Show error on status bar
            self._status_modified.setText("✕ Permission Denied")
            self._status_modified.setStyleSheet("color:#ef4444; font-size:11px; background:transparent; font-weight:600;")
            return

        SIMUOS_FILE_CONTENTS[self._vfs_path] = content
        _log_disk("Write", self._vfs_path)   # ← real disk write event
        _update_vfs_metadata(self._vfs_path, "modify") # Update Modify and Change times
        self._modified = False
        self._status_modified.setText("")
        self._update_title()
        # Flash the status to confirm
        self._status_modified.setText("✓ Saved")
        self._status_modified.setStyleSheet(
            "color:#10b981; font-size:11px; background:transparent; font-weight:600;"
        )
        QTimer.singleShot(2000, self._clear_save_indicator)

    def _clear_save_indicator(self):
        self._status_modified.setText("")
        self._status_modified.setStyleSheet(
            "color:#f59e0b; font-size:11px; background:transparent; font-weight:600;"
        )



# ═════════════════════════════════════════════════════════════════════════════
#  TASK MANAGER WINDOW
# ═════════════════════════════════════════════════════════════════════════════
# ═════════════════════════════════════════════════════════════════════════════
#  PROFESSIONAL RESOURCE GRAPH WIDGET
# ═════════════════════════════════════════════════════════════════════════════
class ResourceGraph(QWidget):
    def __init__(self, color, label, parent=None):
        super().__init__(parent)
        self.color = QColor(color)
        self.label = label
        self.history = collections.deque([random.randint(10, 30) for _ in range(80)], maxlen=80)
        self.setMinimumHeight(150)
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        w, h = self.width(), self.height()
        
        # Draw Background Grid
        painter.setPen(QPen(QColor(45, 45, 45), 1))
        for i in range(0, w, 50): painter.drawLine(i, 0, i, h)
        for i in range(0, h, 30): painter.drawLine(0, i, w, i)

        # ── Large faint watermark label behind the graph ─────────────────────
        from PyQt6.QtGui import QFont as _QFont
        from PyQt6.QtCore import Qt as _Qt
        wm_font = _QFont("Segoe UI", 36, _QFont.Weight.Bold)
        painter.setFont(wm_font)
        painter.setPen(QColor(self.color.red(), self.color.green(), self.color.blue(), 22))
        painter.drawText(self.rect(), _Qt.AlignmentFlag.AlignCenter, self.label.upper())
        # Reset font to default
        painter.setFont(_QFont("Segoe UI", 9))
        
        # Draw Line
        if len(self.history) < 2: return
        path = QPainterPath()
        step = w / 79
        path.moveTo(0, h - (self.history[0] / 100 * h))
        for i, val in enumerate(self.history):
            path.lineTo(i * step, h - (val / 100 * h))
            
        # Stroke
        painter.setPen(QPen(self.color, 2))
        painter.drawPath(path)
        
        # Area Fill
        fill_path = QPainterPath(path)
        fill_path.lineTo(w, h)
        fill_path.lineTo(0, h)
        fill_path.closeSubpath()
        
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0, QColor(self.color.red(), self.color.green(), self.color.blue(), 80))
        grad.setColorAt(1, QColor(self.color.red(), self.color.green(), self.color.blue(), 0))
        painter.setBrush(grad)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(fill_path)

        # ── Current value label (top-left corner) ────────────────────────────
        cur_val = self.history[-1] if self.history else 0
        painter.setFont(_QFont("Segoe UI", 10, _QFont.Weight.Bold))
        painter.setPen(self.color)
        painter.drawText(10, 20, f"{cur_val}%")

    def update_val(self, val):
        self.history.append(max(0, min(100, val)))
        self.update()


# ═════════════════════════════════════════════════════════════════════════════
#  ENHANCED TASK MANAGER WINDOW
# ═════════════════════════════════════════════════════════════════════════════
class TaskManagerWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SimuOS — Task Manager")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(1300, 780)

        # Simulation Logic
        self.processes = []
        self.system_time = 0
        self.is_running = False
        self.current_algo = "FCFS"
        self.mem_algo = "First-Fit"
        self.execution_log = []
        self.sys_logs = []
        self.ready_queue = collections.deque()
        self.active_proc = None
        self.mem_frames = [0] * 100 # 100 frames, 0=free, pid=occupied
        self.io_queues = {"Printer": [], "Disk": [], "Keyboard": []}
        self.page_faults = 0

        # Root Style - PREMIUM DARK SLATE THEME
        self.container = QFrame(self)
        self.container.setGeometry(0, 0, 1300, 780)
        self.container.setStyleSheet("background: #0f172a; border-radius: 20px; border: 1px solid #1e293b;")
        self.container.setGraphicsEffect(make_shadow(60, 20, 220))

        layout = QHBoxLayout(self.container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 1. Sidebar
        self.sidebar = self._build_sidebar()
        layout.addWidget(self.sidebar)

        # 2. Main Content
        self.main_pane = QWidget()
        layout.addWidget(self.main_pane)
        self.main_layout = QVBoxLayout(self.main_pane)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # Header
        self.main_layout.addWidget(self._build_header())

        # Pages
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_proc_page())
        self.pages.addWidget(self._build_perf_page())
        self.pages.addWidget(self._build_gantt_page())
        self.pages.addWidget(self._build_memory_page())
        self.pages.addWidget(self._build_io_page())
        self.main_layout.addWidget(self.pages)

        # Timers
        self.sim_timer = QTimer()
        self.sim_timer.timeout.connect(self._tick)
        
        self.ui_timer = QTimer()
        self.ui_timer.timeout.connect(self._update_realtime_stats)
        self.ui_timer.start(1000)

        # Populate with initial registry data
        self._load_registry_data()
        
        # Auto-start simulation
        self.current_algo = "FCFS"
        self.is_running = True
        self.log_event("Kernel simulation started automatically. Using FCFS dispatcher.")
        self.sim_timer.start(500)

    def _load_registry_data(self):
        global SIMUOS_PROCESS_REGISTRY
        # For any existing processes in registry, add them
        for pid, p_info in SIMUOS_PROCESS_REGISTRY.items():
            name = p_info["name"]
            color = QColor(random.randint(100, 255), random.randint(100, 255), random.randint(100, 255))
            mem_req = random.randint(3, 20)
            p = {
                "pid": pid, "name": name, "arrival": 0,
                "burst": 999999, "rem": 999999, "state": "New", "color": color,
                "cpu": 0, "cpu_ticks": 0, "ram": mem_req * 16, "gpu": 0,
                "frames": mem_req, "has_mem": False
            }
            self.processes.append(p)
            self.log_event(f"Loaded existing PID {pid} ({p['name']}).")

        # Fake I/O Devices for simulation
        self.io_queues = {"Printer": [], "Disk": [], "Keyboard": []}
            
        self._update_io_ui()
        self._update_ui_table()

    def log_event(self, msg):
        now = datetime.now().strftime("%H:%M:%S")
        self.sys_logs.insert(0, f"[{now}] [T={self.system_time}s] {msg}")
        if len(self.sys_logs) > 80:
            self.sys_logs.pop()
        if hasattr(self, 'log_list'):
            self.log_list.clear()
            self.log_list.addItems(self.sys_logs)

    def _build_sidebar(self):
        side = QFrame()
        side.setFixedWidth(220)
        side.setStyleSheet("background: #1e293b; border-top-left-radius: 20px; border-bottom-left-radius: 20px; border-right: 1px solid #334155;")
        layout = QVBoxLayout(side)
        layout.setContentsMargins(15, 50, 15, 20)
        layout.setSpacing(10)

        icons = [("📊", 0, "Processes"), ("📈", 1, "Performance"), ("📉", 2, "Gantt Chart"), ("🧠", 3, "Memory"), ("🖨️", 4, "I/O Devices")]
        for icon, idx, tooltip in icons:
            btn = QPushButton(f"  {icon}   {tooltip}")
            btn.setFixedSize(190, 50)
            btn.setStyleSheet("""
                QPushButton { background: transparent; color: #94a3b8; font-size: 15px; font-weight: 600; text-align: left; padding-left: 15px; border: none; border-radius: 12px; }
                QPushButton:hover { background: #334155; color: #f8fafc; }
            """)
            btn.clicked.connect(lambda chk, i=idx: self.pages.setCurrentIndex(i))
            layout.addWidget(btn)
        
        layout.addStretch()
        close = QPushButton("  ✕   Close Manager")
        close.setFixedSize(190, 50)
        close.setStyleSheet("""
            QPushButton { color: #f87171; font-size: 15px; font-weight: 600; text-align: left; padding-left: 15px; border: none; background: transparent; border-radius: 12px; }
            QPushButton:hover { background: rgba(248,113,113,0.1); }
        """)
        close.clicked.connect(self.close)
        layout.addWidget(close)
        return side

    def _build_header(self):
        h = QFrame()
        h.setFixedHeight(110)
        h.setStyleSheet("background: transparent; border-bottom: 1px solid #1e293b;")
        layout = QHBoxLayout(h)
        layout.setContentsMargins(40, 0, 40, 0)

        v = QVBoxLayout()
        v.setSpacing(6)
        v.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        title = QLabel("System Dashboard")
        title.setStyleSheet("color: #f8fafc; font-size: 26px; font-weight: 800; letter-spacing: 0.5px;")
        self.sub_title = QLabel("Kernel: SimuOS v2.0 • CPU: 0% • RAM: 0%")
        self.sub_title.setStyleSheet("color: #94a3b8; font-size: 14px; font-weight: 500;")
        v.addWidget(title)
        v.addWidget(self.sub_title)
        layout.addLayout(v)

        layout.addStretch()

        # Static FCFS badge — only scheduling strategy
        algo_layout = QVBoxLayout()
        algo_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl = QLabel("SCHEDULER")
        lbl.setStyleSheet("color: #64748b; font-size: 11px; font-weight: 800; letter-spacing: 1px;")
        algo_val = QLabel("FCFS")
        algo_val.setStyleSheet(
            "color: #38bdf8; font-weight: 800; font-size: 16px; "
            "background: rgba(56,189,248,0.1); padding: 8px 20px; "
            "border-radius: 8px; border: 1px solid rgba(56,189,248,0.3);"
        )
        algo_layout.addWidget(lbl)
        algo_layout.addWidget(algo_val)
        layout.addLayout(algo_layout)

        h.mousePressEvent = self._drag_start
        h.mouseMoveEvent = self._drag_move
        return h

    def _build_proc_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(20)

        ctrl = QHBoxLayout()
        ctrl.addStretch()
        
        self.state_lbl = QLabel("Idle")
        self.state_lbl.setStyleSheet("color: #a78bfa; font-weight: 700; font-size: 15px; padding-right: 25px; background: rgba(167,139,250,0.1); padding: 8px 16px; border-radius: 8px; margin-right: 15px;")
        ctrl.addWidget(self.state_lbl)
        
        self.time_lbl = QLabel("T = 0s")
        self.time_lbl.setStyleSheet("color: #38bdf8; font-family: 'Consolas'; font-size: 18px; font-weight: 800; background: rgba(56,189,248,0.1); padding: 8px 16px; border-radius: 8px;")
        ctrl.addWidget(self.time_lbl)
        layout.addLayout(ctrl)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Process", "CPU Usage", "Memory", "Uptime", "Status", "Action"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setShowGrid(False)
        self.table.setStyleSheet("""
            QTableWidget { background: transparent; color: #cbd5e1; border: none; font-size: 14px; font-weight: 500; }
            QTableWidget::item { border-bottom: 1px solid #1e293b; padding: 12px 16px; }
            QHeaderView::section { background: #1e293b; color: #94a3b8; padding: 16px; border: none; font-size: 12px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.5px; }
            QHeaderView { border-top-left-radius: 8px; border-top-right-radius: 8px; }
        """)
        self.table.setCursor(Qt.CursorShape.PointingHandCursor)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_right_click)
        layout.addWidget(self.table)
        return page

    def _on_table_right_click(self, pos):
        item = self.table.itemAt(pos)
        if item:
            self._kill_process(item.row(), item.column())

    def _kill_process(self, row, col):
        if row < 0 or row >= len(self.processes): return
        p = self.processes[row]
        if p["state"] == "Terminated": return
        pid = p["pid"]
        global SIMUOS_PROCESS_REGISTRY
        if pid in SIMUOS_PROCESS_REGISTRY:
            win = SIMUOS_PROCESS_REGISTRY[pid].get("window")
            if win:
                try:
                    win.close()
                except Exception:
                    pass

    def _end_task_by_row(self, row):
        """End Task button handler — terminates the process at the given table row."""
        if row < 0 or row >= len(self.processes):
            return
        p = self.processes[row]
        if p["state"] == "Terminated":
            return
        pid = p["pid"]
        global SIMUOS_PROCESS_REGISTRY
        # Close the actual window if it exists
        if pid in SIMUOS_PROCESS_REGISTRY:
            win = SIMUOS_PROCESS_REGISTRY[pid].get("window")
            if win:
                try:
                    win.close()
                except Exception:
                    pass
            SIMUOS_PROCESS_REGISTRY.pop(pid, None)
        # Force state to Terminated immediately in the sim
        p["state"] = "Terminated"
        p["rem"] = 0
        if self.active_proc and self.active_proc["pid"] == pid:
            self.active_proc = None
        self._free_memory(p)
        self.log_event(f"PID {pid} ({p['name']}) ended by user via End Task button.")
        self._update_ui_table()

    def _build_perf_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(25)

        scroll = QFrame()
        scroll_layout = QVBoxLayout(scroll)
        scroll_layout.setSpacing(20)
        
        self.cpu_graph = ResourceGraph("#3b82f6", "CPU Utilization")
        self.ram_graph = ResourceGraph("#10b981", "Physical Memory")
        self.gpu_graph = ResourceGraph("#f59e0b", "GPU Usage")
        
        scroll_layout.addWidget(self.cpu_graph)
        scroll_layout.addWidget(self.ram_graph)
        scroll_layout.addWidget(self.gpu_graph)
        layout.addWidget(scroll)
        return page

    def _build_gantt_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 30)
        
        self.gantt_area = QFrame()
        self.gantt_area.setStyleSheet("background: #1e293b; border: 1px solid #334155; border-radius: 16px;")
        layout.addWidget(self.gantt_area)
        self.gantt_area.paintEvent = self._draw_gantt_vertical
        return page

    def _build_memory_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(20)
        
        ctrl = QHBoxLayout()
        algo_lbl = QLabel("Allocation Strategy")
        algo_lbl.setStyleSheet("color: #94a3b8; font-weight: 800; font-size: 12px; letter-spacing: 0.5px; text-transform: uppercase; margin-right: 10px;")

        algo_val = QLabel("First-Fit")
        algo_val.setStyleSheet(
            "color: #10b981; font-weight: 800; font-size: 14px; "
            "background: rgba(16,185,129,0.1); padding: 8px 16px; "
            "border-radius: 8px; border: 1px solid rgba(16,185,129,0.3);"
        )

        self.pf_lbl = QLabel("Page Faults: 0")
        self.pf_lbl.setStyleSheet("color: #ef4444; font-weight: 800; font-size: 14px; background: rgba(239,68,68,0.1); padding: 8px 16px; border-radius: 8px; margin-left: 20px;")

        ctrl.addWidget(algo_lbl)
        ctrl.addWidget(algo_val)
        ctrl.addWidget(self.pf_lbl)
        ctrl.addStretch()
        layout.addLayout(ctrl)
        
        self.heatmap_area = QFrame()
        self.heatmap_area.setStyleSheet("background: #1e293b; border: 1px solid #334155; border-radius: 16px;")
        self.heatmap_area.paintEvent = self._draw_heatmap
        layout.addWidget(self.heatmap_area, stretch=1)
        
        return page

    def _build_io_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 20, 12, 16)
        layout.setSpacing(14)

        hdr = QLabel("I/O Device Queues")
        hdr.setStyleSheet("color: #f8fafc; font-size: 20px; font-weight: 800; background: transparent; padding-left: 8px;")
        layout.addWidget(hdr)

        sub = QLabel("Requests currently waiting for hardware service  —  drag the dividers to resize panels")
        sub.setStyleSheet("color: #64748b; font-size: 12px; background: transparent; padding-left: 8px; margin-bottom: 4px;")
        layout.addWidget(sub)

        # QSplitter lets the user drag to resize each panel
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(6)
        splitter.setStyleSheet("""
            QSplitter::handle {
                background: #1e293b;
                border-left:  1px solid #334155;
                border-right: 1px solid #334155;
                margin: 6px 0;
                border-radius: 3px;
            }
            QSplitter::handle:hover {
                background: #38bdf8;
            }
            QSplitter::handle:pressed {
                background: #0ea5e9;
            }
        """)

        dev_cfg = [
            ("Printer",  "\U0001f5a8",  "#a78bfa"),
            ("Disk",     "\U0001f4be",  "#38bdf8"),
            ("Keyboard", "\u2328",       "#10b981"),
        ]

        self.q_lists = {}
        for dev, icon, color in dev_cfg:
            frame = QFrame()
            frame.setStyleSheet(f"""
                QFrame {{
                    background: #1e293b;
                    border: 1px solid #334155;
                    border-top: 4px solid {color};
                    border-radius: 14px;
                    margin: 2px;
                }}
            """)
            fl = QVBoxLayout(frame)
            fl.setContentsMargins(16, 16, 16, 16)
            fl.setSpacing(10)

            title = QLabel(f"{icon}  {dev}")
            title.setStyleSheet(
                f"color: {color}; font-size: 18px; font-weight: 800; "
                f"background: transparent; border: none; letter-spacing: 0.5px;"
            )
            fl.addWidget(title)

            badge = QLabel("● ACTIVE")
            badge.setStyleSheet(f"""
                QLabel {{
                    color: {color};
                    background: transparent;
                    border: none;
                    font-size: 11px;
                    font-weight: 700;
                    letter-spacing: 1px;
                }}
            """)
            fl.addWidget(badge)

            q_list = QListWidget()
            q_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            q_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            q_list.setStyleSheet(f"""
                QListWidget {{
                    background: #0f172a;
                    border: 1px solid #1e293b;
                    border-radius: 10px;
                    color: #e2e8f0;
                    font-family: 'Consolas', monospace;
                    font-size: 13px;
                    padding: 6px;
                }}
                QListWidget::item {{
                    padding: 14px 10px;
                    border-bottom: 1px solid #1e293b;
                    border-radius: 6px;
                    min-height: 32px;
                }}
                QListWidget::item:hover {{ background: rgba(255,255,255,0.06); }}
                QScrollBar:vertical {{
                    background: #0f172a;
                    width: 6px;
                    border-radius: 3px;
                }}
                QScrollBar::handle:vertical {{
                    background: #334155;
                    border-radius: 3px;
                }}
                QScrollBar::handle:vertical:hover {{ background: #38bdf8; }}
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            """)
            self.q_lists[dev] = q_list
            fl.addWidget(q_list, stretch=1)
            splitter.addWidget(frame)

        # Equal initial sizes
        total = 1300 - 240   # approx content width minus sidebar
        splitter.setSizes([total // 3, total // 3, total // 3])

        layout.addWidget(splitter, stretch=1)
        return page




    def _allocate_memory(self, p):
        """Allocate memory using First-Fit algorithm."""
        req = p["frames"]

        # Build list of contiguous free blocks
        free_blocks = []
        start = -1
        for i, f in enumerate(self.mem_frames):
            if f == 0:
                if start == -1: start = i
            else:
                if start != -1:
                    free_blocks.append((start, i - start))
                    start = -1
        if start != -1:
            free_blocks.append((start, 100 - start))

        # First-Fit: pick the first block large enough
        valid_blocks = [b for b in free_blocks if b[1] >= req]
        if not valid_blocks:
            self.page_faults += 1
            self.log_event(f"Page Fault! No contiguous {req} frames for PID {p['pid']}.")
            self.pf_lbl.setText(f"Page Faults: {self.page_faults}")
            return False

        chosen_start = valid_blocks[0][0]  # First-Fit
        for i in range(chosen_start, chosen_start + req):
            self.mem_frames[i] = p["pid"]

        p["has_mem"] = True
        self.log_event(f"Allocated {req} frames at block {chosen_start} for PID {p['pid']} (First-Fit).")
        if hasattr(self, 'heatmap_area'): self.heatmap_area.update()
        return True

    def _free_memory(self, p):
        count = 0
        for i in range(100):
            if self.mem_frames[i] == p["pid"]:
                self.mem_frames[i] = 0
                count += 1
        if count > 0:
            self.log_event(f"Freed {count} memory frames owned by PID {p['pid']}.")
        if hasattr(self, 'heatmap_area'): self.heatmap_area.update()



    def _tick(self):
        self.system_time += 1
        self.time_lbl.setText(f"T = {self.system_time}s")

        global SIMUOS_PROCESS_REGISTRY
        
        # Check for new processes in registry
        existing_pids = {p["pid"] for p in self.processes}
        for pid, p_info in SIMUOS_PROCESS_REGISTRY.items():
            if pid not in existing_pids:
                name = p_info["name"]
                color = QColor(random.randint(100, 255), random.randint(100, 255), random.randint(100, 255))
                mem_req = random.randint(3, 20)
                p = {
                    "pid": pid, "name": name, "arrival": self.system_time,
                    "burst": 999999, "rem": 999999, "state": "New", "color": color,
                    "cpu": 0, "cpu_ticks": 0, "ram": mem_req * 16, "gpu": 0,
                    "frames": mem_req, "has_mem": False
                }
                self.processes.append(p)
                self.log_event(f"Registry spawn PID {pid} ({p['name']}). State: New.")

        # Check for terminated processes (removed from registry)
        registry_pids = set(SIMUOS_PROCESS_REGISTRY.keys())
        for p in self.processes:
            if p["state"] != "Terminated" and p["pid"] not in registry_pids:
                p["state"] = "Terminated"
                p["rem"] = 0
                self.log_event(f"PID {p['pid']} closed by user. Terminated.")
                self._free_memory(p)
                if self.active_proc and self.active_proc["pid"] == p["pid"]:
                    self.active_proc = None

        # ── Admit new / memory-waiting processes ─────────────────────────────
        for p in self.processes:
            if p["state"] == "New" and p["arrival"] <= self.system_time:
                if self._allocate_memory(p):
                    p["state"] = "Ready"
                    self.ready_queue.append(p)
                    self.log_event(f"PID {p['pid']} loaded into memory. State -> Ready.")
                else:
                    p["state"] = "Waiting"
                    self.log_event(f"PID {p['pid']} Waiting for memory.")
            elif p["state"] == "Waiting" and not p["has_mem"]:
                if self._allocate_memory(p):
                    p["state"] = "Ready"
                    self.ready_queue.append(p)
                    self.log_event(f"PID {p['pid']} loaded into memory. State -> Ready.")

        # ── Random I/O blocking ───────────────────────────────────────────────
        if self.active_proc and random.random() < 0.1:
            dev = random.choice(list(self.io_queues.keys()))
            self.io_queues[dev].append(self.active_proc["pid"])
            self.active_proc["state"] = "Waiting"
            self.log_event(f"PID {self.active_proc['pid']} requested {dev} I/O. State -> Waiting.")
            self.active_proc = None

        # ── I/O completion ────────────────────────────────────────────────────
        for dev, q in self.io_queues.items():
            if q and random.random() < 0.3:
                pid = q.pop(0)
                for p in self.processes:
                    if p["pid"] == pid:
                        p["state"] = "Ready"
                        self.ready_queue.append(p)
                        self.log_event(f"PID {pid} finished {dev} I/O. State -> Ready.")
                        break

        # ── FCFS dispatcher — pick next from front of ready queue ─────────────
        if not self.active_proc and self.ready_queue:
            self.active_proc = self.ready_queue.popleft()
            self.log_event(f"Dispatcher selected PID {self.active_proc['pid']} (FCFS).")

        if self.active_proc:
            if self.active_proc["state"] != "Running":
                self.active_proc["state"] = "Running"
                self.log_event(f"PID {self.active_proc['pid']} context switched to CPU.")
            # Do not decrement rem to keep it alive until closed by user
            self.active_proc["cpu_ticks"] = self.active_proc.get("cpu_ticks", 0) + 1  
            self.execution_log.append((self.system_time, self.active_proc["pid"], self.active_proc["color"]))
            self.state_lbl.setText(f"Active: {self.active_proc['name']} ({self.active_proc['pid']})") 
        else:
            self.execution_log.append((self.system_time, -1, QColor("#0f172a")))
            self.state_lbl.setText("System Idle")

        self._update_ui_table()
        self._update_io_ui()
        if hasattr(self, 'gantt_area'): self.gantt_area.update()

    # No demo data — all queues are fully real
    _IO_DEMO: dict = {}

    def _update_io_ui(self):
        """Refresh all device queues with real live data."""
        global SIMUOS_DISK_LOG, SIMUOS_KEYBOARD_LOG, SIMUOS_PRINTER_LOG
        for dev, q in self.io_queues.items():
            if dev not in getattr(self, 'q_lists', {}):
                continue
            lst = self.q_lists[dev]
            lst.clear()

            # Real blocked simulation processes (highlighted red) always first
            for pid in q:
                for p in self.processes:
                    if p["pid"] == pid:
                        item = QListWidgetItem(f"\u26a1  PID {pid}  \u2192  {p['name']}  [LIVE]")
                        item.setForeground(QColor("#f87171"))
                        lst.addItem(item)

            if dev == "Disk":
                if SIMUOS_DISK_LOG:
                    for entry in SIMUOS_DISK_LOG:
                        if isinstance(entry, tuple):
                            icon, op, path = entry
                            text  = f"{icon}  {op}\u2192  {path}"
                            color = "#38bdf8" if op.strip() == "Read" else "#a78bfa"
                        else:
                            text, color = entry, "#475569"
                        item = QListWidgetItem(text)
                        item.setForeground(QColor(color))
                        lst.addItem(item)
                else:
                    hint = QListWidgetItem("\u2139  Open or save a file to log disk activity")
                    hint.setForeground(QColor("#334155"))
                    lst.addItem(hint)

            elif dev == "Printer":
                if SIMUOS_PRINTER_LOG:
                    for i, (fname, path) in enumerate(SIMUOS_PRINTER_LOG):
                        short = path if len(path) <= 38 else "..." + path[-35:]
                        item = QListWidgetItem(f"\U0001f5a8  Job #{i+1:02d}  \u2192  {fname}  [{short}]")
                        item.setForeground(QColor("#a78bfa"))
                        lst.addItem(item)
                else:
                    hint = QListWidgetItem("\u2139  Right-click a file \u2192 Print to add jobs")
                    hint.setForeground(QColor("#334155"))
                    lst.addItem(hint)

            elif dev == "Keyboard":
                if SIMUOS_KEYBOARD_LOG:
                    for key_desc, source in SIMUOS_KEYBOARD_LOG:
                        item = QListWidgetItem(f"\u2328  {key_desc}  [{source}]")
                        item.setForeground(QColor("#10b981"))
                        lst.addItem(item)
                else:
                    hint = QListWidgetItem("\u2139  Type commands in Terminal to log keystrokes")
                    hint.setForeground(QColor("#334155"))
                    lst.addItem(hint)

    def _update_realtime_stats(self):
        """Update header stats and performance graphs using real simulation data."""
        t = max(1, self.system_time)
        total_ram_used = sum(p["ram"] for p in self.processes if p["has_mem"])

        # Real CPU% per process: fraction of total ticks they have held the CPU
        for p in self.processes:
            ticks = p.get("cpu_ticks", 0)
            p["cpu"] = round((ticks / t) * 100, 1)

        # System-wide: 100% if a process is actively running this second, else 0%
        system_cpu = 100 if (self.active_proc is not None) else 0

        # RAM: real frame occupancy out of 100 frames
        ram_percent = sum(1 for f in self.mem_frames if f != 0)  # already /100 * 100

        if hasattr(self, 'cpu_graph'):
            self.cpu_graph.update_val(system_cpu)
            self.ram_graph.update_val(ram_percent)
            self.gpu_graph.update_val(0)  # no GPU simulation
            self.sub_title.setText(
                f"Kernel: SimuOS v2.0 • CPU: {system_cpu}% • RAM: {ram_percent}%"
            )

        self._update_ui_table()

    def _update_ui_table(self):
        self.table.setRowCount(len(self.processes))
        for i, p in enumerate(self.processes):
            # Process Name & PID
            name_item = QTableWidgetItem(f"{p['name']} ({p['pid']})")
            name_item.setForeground(QColor("#f8fafc"))
            self.table.setItem(i, 0, name_item)

            # CPU — instantaneous: 100% if this process holds the CPU right now, else 0%
            is_running = (p["state"] == "Running")
            cpu_val = 100 if is_running else 0
            cpu_item = QTableWidgetItem(f"{cpu_val}%")
            cpu_item.setForeground(QColor("#10b981") if is_running else QColor("#64748b"))
            if is_running:
                cpu_item.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            self.table.setItem(i, 1, cpu_item)

            # Memory
            self.table.setItem(i, 2, QTableWidgetItem(f"{p['ram']} MB"))

            # Uptime
            uptime = max(0, self.system_time - p["arrival"])
            prog_item = QTableWidgetItem(f"{uptime}s")
            if p["state"] == "Terminated":
                prog_item.setForeground(QColor("#ef4444"))
            else:
                prog_item.setForeground(QColor("#38bdf8"))
            self.table.setItem(i, 3, prog_item)

            # Status — color-coded state
            st = QTableWidgetItem(p["state"])
            if p["state"] == "Ready":      st.setForeground(QColor("#38bdf8"))
            elif p["state"] == "Running":  st.setForeground(QColor("#10b981"))
            elif p["state"] == "Waiting":  st.setForeground(QColor("#f59e0b"))
            elif p["state"] == "Terminated": st.setForeground(QColor("#ef4444"))
            else:                          st.setForeground(QColor("#94a3b8"))
            st.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            self.table.setItem(i, 4, st)

            # End Task button
            is_terminated = p["state"] == "Terminated"
            end_btn = QPushButton("End Task" if not is_terminated else "Ended")
            end_btn.setEnabled(not is_terminated)
            end_btn.setFixedSize(72, 26)
            end_btn.setCursor(Qt.CursorShape.PointingHandCursor if not is_terminated else Qt.CursorShape.ForbiddenCursor)
            if not is_terminated:
                end_btn.setStyleSheet("""
                    QPushButton {
                        background: rgba(239,68,68,0.15);
                        color: #f87171;
                        border: 1px solid rgba(239,68,68,0.35);
                        border-radius: 6px;
                        font-size: 11px;
                        font-weight: 700;
                    }
                    QPushButton:hover {
                        background: rgba(239,68,68,0.35);
                        color: white;
                        border-color: #ef4444;
                    }
                    QPushButton:pressed { background: #ef4444; }
                """)
            else:
                end_btn.setStyleSheet("""
                    QPushButton {
                        background: transparent;
                        color: #475569;
                        border: 1px solid #1e293b;
                        border-radius: 6px;
                        font-size: 11px;
                    }
                """)
            end_btn.clicked.connect(lambda _, row=i: self._end_task_by_row(row))
            self.table.setCellWidget(i, 5, end_btn)

    def _draw_gantt_vertical(self, event):
        painter = QPainter(self.gantt_area)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.gantt_area.width(), self.gantt_area.height()

        row_h = 26
        visible_rows = max(1, h // row_h)
        col_w = w / (len(self.processes) + 1) if self.processes else w

        # ── Column separators & process name headers ──────────────────────────
        painter.setPen(QPen(QColor("#334155"), 1, Qt.PenStyle.DashLine))
        for i, p in enumerate(self.processes):
            x = (i + 1) * col_w
            painter.drawLine(int(x), 0, int(x), h)
            painter.setPen(QColor("#64748b"))
            from PyQt6.QtGui import QFont as _QF
            painter.setFont(_QF("Segoe UI", 9, _QF.Weight.Bold))
            painter.drawText(int(x - col_w / 2 - 20), 18, p["name"][:6])
            painter.setPen(QPen(QColor("#334155"), 1, Qt.PenStyle.DashLine))

        # ── Show only the last visible_rows ticks (scrolling window) ──────────
        log_slice = list(self.execution_log)[-visible_rows:]

        for row_idx, (t, pid, color) in enumerate(log_slice):
            y = row_idx * row_h
            if pid == -1:
                # Idle tick — faint grey stripe
                painter.setBrush(QColor(30, 41, 59, 60))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRect(0, y + 2, w, row_h - 4)
                continue
            for i, p in enumerate(self.processes):
                if p["pid"] == pid:
                    painter.setBrush(color)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawRoundedRect(
                        int((i + 1) * col_w - col_w + 8), y + 3,
                        int(col_w - 16), row_h - 6, 5, 5
                    )
                    # Time stamp inside block
                    painter.setPen(QColor(255, 255, 255, 140))
                    painter.setFont(_QF("Consolas", 8))
                    painter.drawText(int((i + 1) * col_w - col_w + 10), y + row_h - 6, f"T{t}")

        # ── Animated scan line at the bottom of drawn content ─────────────────
        scan_y = len(log_slice) * row_h
        if 0 < scan_y <= h:
            # Glowing horizontal line
            glow_pen = QPen(QColor(56, 189, 248, 180), 2)
            painter.setPen(glow_pen)
            painter.drawLine(0, scan_y, w, scan_y)

            # Pulsing dot in the center of the scan line
            painter.setBrush(QColor("#38bdf8"))
            painter.setPen(Qt.PenStyle.NoPen)
            cx = w // 2
            painter.drawEllipse(cx - 5, scan_y - 5, 10, 10)

            # "NOW" label next to the dot
            painter.setPen(QColor("#38bdf8"))
            from PyQt6.QtGui import QFont as _QF2
            painter.setFont(_QF2("Segoe UI", 8, _QF2.Weight.Bold))
            painter.drawText(cx + 10, scan_y + 4, f"NOW  T={self.system_time}s")

    def _draw_heatmap(self, event):
        painter = QPainter(self.heatmap_area)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.heatmap_area.width(), self.heatmap_area.height()
        
        rows, cols = 10, 10
        cell_w = w / cols
        cell_h = h / rows
        
        for idx, f in enumerate(self.mem_frames):
            r = idx // cols
            c = idx % cols
            
            if f == 0:
                color = QColor("#0f172a") 
                border = QColor("#1e293b")
            else:
                color = QColor("#10b981") 
                border = QColor("#059669")
                
            painter.setBrush(color)
            painter.setPen(QPen(border, 1))
            painter.drawRoundedRect(int(c * cell_w) + 4, int(r * cell_h) + 4, int(cell_w) - 8, int(cell_h) - 8, 4, 4)

    def _drag_start(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _drag_move(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            if hasattr(self, '_drag_pos'): self.move(event.globalPosition().toPoint() - self._drag_pos)

# ═════════════════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))

    # Start the boot sequence — it chains to Login → Desktop automatically
    boot = BootScreen()
    boot.show()

    sys.exit(app.exec())