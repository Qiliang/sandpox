"""HTTP Basic Auth loaded from the environment or a local .env file."""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
USER_ENV = "SANDPOX_BASIC_USER"
PASSWORD_ENV = "SANDPOX_BASIC_PASSWORD"


def load_env_file(path: Path = ENV_PATH) -> None:
    """Fill missing environment variables from .env. Existing values win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def basic_auth_credentials() -> tuple[str, str]:
    load_env_file()
    username = os.environ.get(USER_ENV, "")
    password = os.environ.get(PASSWORD_ENV, "")
    if not username or not password:
        raise RuntimeError(
            f"缺少 {USER_ENV} 或 {PASSWORD_ENV}。在 .env 或环境变量里同时设置它们之后才能启动。"
        )
    return username, password


class BasicAuthMiddleware:
    def __init__(self, app: ASGIApp, username: str, password: str) -> None:
        self.app = app
        self.username = username
        self.password = password

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if not self._authorized(Headers(scope=scope).get("authorization")):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            response = PlainTextResponse(
                "Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="sandpox"'},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _authorized(self, header: str | None) -> bool:
        if not header:
            return False
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return False
        try:
            decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        username, separator, password = decoded.partition(":")
        if not separator:
            return False
        return secrets.compare_digest(username, self.username) and secrets.compare_digest(
            password, self.password
        )
