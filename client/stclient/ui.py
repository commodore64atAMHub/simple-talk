"""Terminal UI for the simple-talk client.

Two front ends:
  * ``ChatUI``  - a tiny ANSI TUI (status bar, activity line, log,
                  single-line editor) that works on any ANSI terminal;
                  key handling is cross-platform (termios/msvcrt).
  * ``PlainUI`` - dumb line mode for non-tty output / scripts.
"""

import os
import shutil
import sys
import threading
import time
from collections import deque

CSI = "\x1b["


def enable_windows_vt():
    """Opt into ANSI/VT processing on Windows 10+ consoles."""
    if os.name != "nt":
        return
    try:
        import ctypes

        k = ctypes.windll.kernel32
        out = k.GetStdHandle(-11)
        mode = k.GetConsoleMode(out, ctypes.byref(ctypes.c_uint32()))
        if mode:
            k.SetConsoleMode(out, mode.value | 0x0001 | 0x0002 | 0x0004)
    except Exception:
        pass


def wrap(text: str, width: int) -> list:
    """Word wrap returning lines no longer than ``width``."""
    if width <= 1:
        return text.splitlines() or [""]
    out = []
    for para in text.split("\n"):
        if not para:
            out.append("")
            continue
        words = para.split(" ")
        line = ""
        for w in words:
            if not w:
                continue
            while len(w) > width:
                if line:
                    out.append(line)
                    line = ""
                out.append(w[:width])
                w = w[width:]
            if len(line) + len(w) + (1 if line else 0) > width:
                out.append(line)
                line = w
            else:
                line = (line + " " + w).strip()
        out.append(line)
    return out


def term_size(fallback=(100, 26)) -> tuple:
    try:
        size = shutil.get_terminal_size(fallback)
        return size.columns, size.lines
    except OSError:
        return fallback


def centro(seq: str) -> str:
    """Underscore-out any control sequence so it can't escape the UI."""
    return seq.replace("\x1b", "?")


class ChatUI:
    """TUI: status(1) + activity(2) + log + single-line editor."""

    PROMPT = "> "

    def __init__(self, submit, on_quit, loop):
        self.log: deque = deque(maxlen=600)
        self.buf = ""
        self.cur = 0
        self.status = "connecting to mesh..."
        self.activity = ""
        self.submit = submit          # async-safe callable(text)
        self.on_quit = on_quit        # async-safe callable()
        self.loop = loop

    # -- public API -----------------------------------------------------
    def set_status(self, text: str) -> None:
        self.status = text
        self._schedule_redraw()

    def set_activity(self, text: str) -> None:
        self.activity = text
        self._schedule_redraw()

    def add_raw(self, text: str, kind: str = "*") -> None:
        self.log.append((time.strftime("%H:%M:%S"), kind, text))
        self._schedule_redraw()

    # -- key events (from background thread, via call_soon_threadsafe) --
    def on_key(self, key) -> None:
        if key == "enter":
            text = self.buf
            self.buf = ""
            self.cur = 0
            self.redraw()
            if text.strip():
                self.submit(text)
        elif key == "backspace":
            if self.cur > 0:
                self.buf = self.buf[: self.cur - 1] + self.buf[self.cur:]
                self.cur -= 1
        elif key == "ctrl-u":
            self.buf = ""
            self.cur = 0
        elif key == "left":
            self.cur = max(0, self.cur - 1)
        elif key == "right":
            self.cur = min(len(self.buf), self.cur + 1)
        elif key == "home":
            self.cur = 0
        elif key == "end":
            self.cur = len(self.buf)
        elif key in ("ctrl-c",):
            self.on_quit()
            return
        elif key in ("ctrl-d", "escape"):
            if not self.buf:
                self.on_quit()
                return
        elif isinstance(key, str) and len(key) == 1:
            self.buf = self.buf[: self.cur] + key + self.buf[self.cur:]
            self.cur += 1
        self.redraw()

    # -- rendering ------------------------------------------------------
    def _schedule_redraw(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.redraw)
        except Exception:
            pass

    def redraw(self, *_a) -> None:
        try:
            self._render()
        except Exception:
            # never let a rendering glitch take down the client/input loop
            pass

    def _render(self) -> None:
        cols, rows = term_size()
        rows = max(rows, 4)
        out = [CSI + "2J"]  # home + clear

        # status bar (row 1)
        st = self._fit(self.status, cols - 4)
        out.append(CSI + "1;1H" + "\x1b[7m " + st + self._pad("", cols - len(st) - 2) + " \x1b[0m")

        # activity line (row 2)
        ac = self._fit(self.activity, cols - 4)
        out.append(CSI + "2;1H" + " " + ac + self._pad("", cols - len(ac) - 1))

        # log area (rows 3..rows-1)
        log_rows = rows - 3
        rendered = []
        for ts, kind, text in self.log:
            for line in wrap(text, cols - 10):
                rendered.append((ts, kind, line))
        tail = rendered[-log_rows:]
        for i, (ts, kind, line) in enumerate(tail):
            row = 3 + i
            if len(rendered) > log_rows:
                ts = ""
            entry = self._pad(self._fit(f"{ts}  {line}", cols), cols)
            out.append(CSI + f"{row};1H" + entry)

        # input line (row rows)
        prompt = self.PROMPT
        inp = prompt + self.buf
        inp_disp = self._pad(self._fit(inp, cols, cut_keep_end=False), cols)
        out.append(CSI + f"{rows};1H" + inp_disp)

        # cursor position
        cx = min(len(prompt.encode("utf-8", "replace")) + len(self.buf[: self.cur].encode("utf-8", "replace")) + 1, cols)
        out.append(CSI + f"{rows};{cx}H")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    @staticmethod
    def _fit(text: str, width: int, cut_keep_end=True) -> str:
        """Truncate a single line to ``width`` columns (approx)."""
        if width <= 0:
            return ""
        while len(text) > width:
            text = text[:-1]
        return text

    @staticmethod
    def _pad(text: str, width: int) -> str:
        need = width - len(text)
        return text + (" " * need if need > 0 else "")


class PlainUI:
    """Line-oriented fallback for non-tty stdin/stdout."""

    def __init__(self):
        self.status = "connecting to mesh..."
        self.activity = ""

    def set_status(self, text: str) -> None:
        self.status = text

    def set_activity(self, text: str) -> None:
        self.activity = text

    def add_raw(self, text: str, kind: str = "*") -> None:
        self._print(f"{time.strftime('%H:%M:%S')}  {text}")

    @staticmethod
    def _print(text: str) -> None:
        sys.stdout.write(centro(text) + "\r\n")
        sys.stdout.flush()


class Terminal:
    """Owns raw/cbreak mode for the input tty on POSIX; no-op on Windows."""

    def __init__(self):
        self._old = None
        self._raw = False

    def enter_raw(self):
        if os.name == "nt" or not sys.stdin.isatty():
            return
        import termios
        import tty

        fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(fd)
        tty.setraw(fd)
        self._raw = True

    def restore(self):
        if not self._raw or os.name == "nt":
            return
        import termios

        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old)
        except OSError:
            pass
        self._raw = False


class KeyReader:
    """Reads terminal key events on a background thread.

    Produces tokens: 'enter','backspace','left','right','home','end',
    'ctrl-c','ctrl-d','ctrl-u','escape', or single printable characters.
    """

    def __init__(self, on_key, into_loop):
        self.on_key = on_key
        self.into_loop = into_loop
        self._t = None
        self._stop = threading.Event()

    def start(self):
        self._t = threading.Thread(target=self._run, name="keys", daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=1.0)

    def _emit(self, token):
        try:
            self.into_loop.call_soon_threadsafe(self.on_key, token)
        except RuntimeError:
            pass

    def _run(self):
        if os.name == "nt":
            self._run_windows()
        else:
            self._run_posix()

    def _run_posix(self):
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = None
        try:
            old = termios.tcgetattr(fd)
            tty.setraw(fd)
        except (OSError, termios.error):
            old = None
        try:
            while not self._stop.is_set():
                if not select.select([fd], [], [], 0.05)[0]:
                    continue
                raw = os.read(fd, 1)
                if not raw:
                    break
                c = raw.decode("utf-8", "replace")
                if c in "\r\n":
                    self._emit("enter")
                elif c in "\x7f\x08":
                    self._emit("backspace")
                elif c == "\x03":
                    self._emit("ctrl-c")
                elif c == "\x04":
                    self._emit("ctrl-d")
                elif c == "\x15":
                    self._emit("ctrl-u")
                elif c == "\x1b":
                    seq = b""
                    if select.select([fd], [], [], 0.02)[0]:
                        seq = os.read(fd, 8)
                    if seq == b"[D":
                        self._emit("left")
                    elif seq == b"[C":
                        self._emit("right")
                    elif seq == b"[H":
                        self._emit("home")
                    elif seq == b"[F":
                        self._emit("end")
                    else:
                        self._emit("escape")
                elif c.isprintable() or c.isalnum():
                    self._emit(c)
        finally:
            if old is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                except OSError:
                    pass

    def _run_windows(self):
        import msvcrt

        pending = ""
        while not self._stop.is_set():
            ch = msvcrt.getwch()
            if pending:
                token = {
                    "K": "left", "M": "right", "H": "up", "P": "down",
                    "G": "home", "O": "end",
                }.get(ch)
                self._emit(token or "escape")
                pending = ""
                continue
            if ch in ("\x00", "\xe0"):
                pending = ch
                continue
            if ch in "\r\n":
                self._emit("enter")
            elif ch in ("\x7f", "\x08"):
                self._emit("backspace")
            elif ch == "\x03":
                self._emit("ctrl-c")
            elif ch == "\x04":
                self._emit("ctrl-d")
            elif ch == "\x15":
                self._emit("ctrl-u")
            elif ch.isprintable():
                self._emit(ch)