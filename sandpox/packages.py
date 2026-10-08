"""Install and remove packages in the same environment that runs published apps."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import threading

_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_VERSION = r"[A-Za-z0-9*](?:[A-Za-z0-9*._+-]*[A-Za-z0-9*])?"
_OPERATOR = r"(?:===|==|!=|<=|>=|~=|<|>)"
_REQUIREMENT = re.compile(
    rf"^{_NAME}(?:\[{_NAME}(?:,{_NAME})*\])?(?:{_OPERATOR}{_VERSION}(?:,{_OPERATOR}{_VERSION})*)?$"
)
_PACKAGE_NAME = re.compile(rf"^{_NAME}$")
_OPERATORS = ("===", "==", "!=", "<=", ">=", "~=", "<", ">")
_PROTECTED = {
    "fastapi",
    "gradio",
    "uvicorn",
    "starlette",
    "pydantic",
    "httpx",
    "anyio",
}
_MAX_REQUIREMENTS = 30
_MAX_TEXT = 4_000
_LOG_LIMIT = 200_000


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(text: str) -> tuple[list[str] | None, str]:
    """Accept package names and version specifiers. Reject flags, URLs, and paths."""
    raw = text.strip()
    if not raw:
        return None, "请填写要安装的包"
    if len(raw) > _MAX_TEXT:
        return None, "内容太长了"

    specs: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            return None, "不能带命令行参数"
        if any(operator in line for operator in _OPERATORS):
            pieces = [line]
        else:
            pieces = line.split()
        for piece in pieces:
            if piece.startswith("-"):
                return None, "不能带命令行参数"
            normalized = re.sub(r"\s+", "", piece)
            if not _REQUIREMENT.fullmatch(normalized):
                return None, f"包名不合法：{piece}"
            specs.append(normalized)

    if not specs:
        return None, "请填写要安装的包"
    if len(specs) > _MAX_REQUIREMENTS:
        return None, f"一次最多安装 {_MAX_REQUIREMENTS} 个包"
    return specs, ""


def parse_package_name(name: str) -> tuple[str | None, str]:
    cleaned = name.strip()
    if not _PACKAGE_NAME.fullmatch(cleaned):
        return None, "包名不合法"
    if canonical_name(cleaned) in _PROTECTED:
        return None, "这个包是运行环境本身，不能卸载"
    return cleaned, ""


def _uv_bin() -> str | None:
    return shutil.which("uv")


def list_installed() -> tuple[list[dict[str, object]] | None, str]:
    uv = _uv_bin()
    if uv is None:
        return None, "找不到 uv，请确认它在 PATH 里"
    try:
        result = subprocess.run(
            [
                uv,
                "pip",
                "list",
                "--python",
                sys.executable,
                "--format",
                "json",
                "--color",
                "never",
                "--no-progress",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return None, f"无法列出已安装的包：{exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return None, detail or "无法列出已安装的包"
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None, "uv 返回的包列表无法解析"
    if not isinstance(payload, list):
        return None, "uv 返回的包列表无法解析"

    packages: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        version = str(item.get("version", "")).strip()
        if not name:
            continue
        packages.append(
            {
                "name": name,
                "version": version,
                "protected": canonical_name(name) in _PROTECTED,
            }
        )
    packages.sort(key=lambda item: str(item["name"]).casefold())
    return packages, ""


class Installer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._ok: bool | None = None
        self._error = ""
        self._log = ""

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "running": self._running,
                "ok": self._ok,
                "error": self._error,
                "log": self._log,
            }

    def start(self, packages: list[str], *, action: str) -> str | None:
        if action not in {"install", "uninstall"}:
            return "未知操作"
        uv = _uv_bin()
        if uv is None:
            return "找不到 uv，请确认它在 PATH 里"
        with self._lock:
            if self._running:
                return "已有安装任务在跑"
            self._running = True
            self._ok = None
            self._error = ""
            self._log = ""
        thread = threading.Thread(
            target=self._run,
            args=(uv, action, list(packages)),
            daemon=True,
        )
        thread.start()
        return None

    def _append(self, text: str) -> None:
        with self._lock:
            self._log = (self._log + text)[-_LOG_LIMIT:]

    def _finish(self, ok: bool, error: str) -> None:
        with self._lock:
            self._running = False
            self._ok = ok
            self._error = error

    def _run(self, uv: str, action: str, packages: list[str]) -> None:
        command = [
            uv,
            "pip",
            action,
            "--python",
            sys.executable,
            "--color",
            "never",
            "--no-progress",
            *packages,
        ]
        self._append("$ " + " ".join(shlex.quote(part) for part in command) + "\n")
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self._append(f"{exc}\n")
            self._finish(False, "无法启动 uv")
            return

        assert proc.stdout is not None
        for line in proc.stdout:
            self._append(line)
        code = proc.wait()
        if code == 0:
            self._append("完成\n")
            self._finish(True, "")
            return
        self._append(f"失败，退出码 {code}\n")
        self._finish(False, "安装失败" if action == "install" else "卸载失败")


installer = Installer()
