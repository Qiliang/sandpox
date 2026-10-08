"""Start and replace the published Gradio subprocess."""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parent.parent
APP_ID = "demo001"
PREVIEW_PATH = f"/app/{APP_ID}/"
DATA_DIR = ROOT / "data" / APP_ID
APP_PATH = DATA_DIR / "app.py"
DRAFT_PATH = DATA_DIR / "draft.py"
LOG_PATH = DATA_DIR / "server.log"
RUNNER_PATH = Path(__file__).resolve().parent / "runner.py"
READY_TIMEOUT_SECONDS = 45
MAX_CODE_CHARS = 1_000_000

SAMPLE_CODE = """\
import gradio as gr

def fn(x):
    return x.upper()

demo = gr.Interface(
    fn,
    gr.Textbox(label="输入"),
    gr.Textbox(label="输出"),
)
"""

_URL_RE = re.compile(r"Running on local URL:\s+https?://[^:\s]+:(\d+)")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _http_ready(port: int) -> bool:
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}/", timeout=1) as response:
            return response.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except Exception:
        return False


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


class AppManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._log_fp: TextIO | None = None
        self._generation = 0
        self.port: int | None = None
        self.last_error = ""

    def read_code(self) -> str:
        if DRAFT_PATH.exists():
            return DRAFT_PATH.read_text(encoding="utf-8")
        if APP_PATH.exists():
            return APP_PATH.read_text(encoding="utf-8")
        return SAMPLE_CODE

    def read_log(self, limit: int = 12_000) -> str:
        if not LOG_PATH.exists():
            return ""
        text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
        return text[-limit:]

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running_unlocked()

    def endpoint(self) -> tuple[str, int] | None:
        with self._lock:
            if self.port is None or not self._is_running_unlocked():
                return None
            return ("127.0.0.1", self.port)

    def status(self) -> dict[str, object]:
        running = self.is_running()
        return {
            "running": running,
            "preview": PREVIEW_PATH if running else "",
            "error": self.last_error,
            "log": self.read_log(),
        }

    def save(self, code: str) -> dict[str, object]:
        error = self._validate(code)
        if error:
            return {"ok": False, "error": error}
        _write(DRAFT_PATH, code)
        return {"ok": True}

    def publish(self, code: str) -> dict[str, object]:
        error = self._validate(code)
        if error:
            return {
                "ok": False,
                "error": error,
                "log": self.read_log(),
                "running": self.is_running(),
                "preview": PREVIEW_PATH if self.is_running() else "",
            }

        _write(DRAFT_PATH, code)
        _write(APP_PATH, code)

        with self._lock:
            self._generation += 1
            generation = self._generation
            self._stop_unlocked()
            port = _free_port()
            try:
                proc = self._spawn(port)
            except OSError as exc:
                self.last_error = str(exc)
                return self._failure(f"无法启动进程：{exc}")
            self._proc = proc

        actual = self._wait_until_ready(generation, port, proc)
        with self._lock:
            if self._generation != generation:
                return self._failure("发布被新的请求取代", running=self._is_running_unlocked())
            if actual is None:
                log = self.read_log()
                self._stop_unlocked()
                message = "Gradio 没有启动起来"
                if proc.poll() not in (None, 0):
                    message = f"应用进程退出了（code {proc.returncode}）"
                self.last_error = message
                return self._failure(message, log=log)
            self.port = actual
            self.last_error = ""
            return {
                "ok": True,
                "preview": PREVIEW_PATH,
                "log": self.read_log(),
                "running": True,
            }

    def restore(self) -> None:
        if not APP_PATH.exists():
            return
        code = APP_PATH.read_text(encoding="utf-8")
        self.publish(code)

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            self._stop_unlocked()

    def _validate(self, code: str) -> str:
        if len(code) > MAX_CODE_CHARS:
            return "代码太长了"
        if not code.strip():
            return "代码是空的"
        try:
            compile(code, "app.py", "exec")
        except SyntaxError as exc:
            location = f"第 {exc.lineno} 行" if exc.lineno else "未知位置"
            return f"语法错误：{location}，{exc.msg}"
        return ""

    def _failure(
        self,
        error: str,
        log: str | None = None,
        running: bool | None = None,
    ) -> dict[str, object]:
        alive = self._is_running_unlocked() if running is None else running
        return {
            "ok": False,
            "error": error,
            "log": self.read_log() if log is None else log,
            "running": alive,
            "preview": PREVIEW_PATH if alive else "",
        }

    def _is_running_unlocked(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self.port is not None

    def _spawn(self, port: int) -> subprocess.Popen[str]:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        log_fp = open(LOG_PATH, "w", encoding="utf-8", buffering=1)
        self._log_fp = log_fp
        log_fp.write(f"sandpox: starting on 127.0.0.1:{port}\n")
        log_fp.flush()
        env = os.environ.copy()
        env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "GRADIO_SERVER_NAME": "127.0.0.1",
                "GRADIO_SERVER_PORT": str(port),
                "GRADIO_ROOT_PATH": PREVIEW_PATH.rstrip("/"),
                "GRADIO_SSR_MODE": "False",
                "GRADIO_SHARE": "False",
                "GRADIO_ANALYTICS_ENABLED": "False",
            }
        )
        popen_kwargs: dict[str, object] = {}
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        try:
            return subprocess.Popen(
                [sys.executable, str(RUNNER_PATH), str(APP_PATH)],
                cwd=DATA_DIR,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                text=True,
                **popen_kwargs,
            )
        except Exception:
            self._close_log()
            raise

    def _wait_until_ready(
        self,
        generation: int,
        port: int,
        proc: subprocess.Popen[str],
    ) -> int | None:
        deadline = time.time() + READY_TIMEOUT_SECONDS
        while time.time() < deadline:
            if generation != self._generation or proc.poll() is not None:
                return None
            actual = self._port_from_log() or port
            if _port_open(actual) and _http_ready(actual):
                return actual
            time.sleep(0.15)
        return None

    def _port_from_log(self) -> int | None:
        match = _URL_RE.search(self.read_log())
        if not match:
            return None
        return int(match.group(1))

    def _stop_unlocked(self) -> None:
        proc = self._proc
        self._proc = None
        self.port = None
        if proc is not None and proc.poll() is None:
            self._terminate(proc)
        self._close_log()

    def _close_log(self) -> None:
        log_fp = self._log_fp
        self._log_fp = None
        if log_fp is not None:
            log_fp.close()

    def _terminate(self, proc: subprocess.Popen[str]) -> None:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


manager = AppManager()
