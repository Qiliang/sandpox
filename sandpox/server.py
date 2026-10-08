"""Editor, save/publish API, and reverse proxy onto the Gradio process."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from sandpox.auth import BasicAuthMiddleware, basic_auth_credentials
from sandpox.manager import APP_ID, PREVIEW_PATH, manager
from sandpox.packages import installer, list_installed, parse_package_name, parse_requirements

STATIC_DIR = Path(__file__).resolve().parent / "static"
PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
_DROPPED_REQUEST_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "accept-encoding",
    "authorization",
}
_DROPPED_RESPONSE_HEADERS = _DROPPED_REQUEST_HEADERS | {
    "content-encoding",
    "x-frame-options",
}


class CodeIn(BaseModel):
    code: str = Field(max_length=1_000_000)


class RequirementsIn(BaseModel):
    requirements: str = Field(max_length=4_000)


class PackageNameIn(BaseModel):
    name: str = Field(max_length=200)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, read=None, write=None, pool=None),
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    restore = asyncio.create_task(asyncio.to_thread(manager.restore))
    try:
        yield
    finally:
        restore.cancel()
        await app.state.http.aclose()
        await asyncio.to_thread(manager.stop)


app = FastAPI(title="sandpox", lifespan=_lifespan)
_basic_user, _basic_password = basic_auth_credentials()
app.add_middleware(BasicAuthMiddleware, username=_basic_user, password=_basic_password)


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "view.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/editor")
async def editor() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "editor.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/packages")
async def packages_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "packages.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/code")
async def code() -> dict[str, str]:
    return {"code": manager.read_code()}


@app.get("/api/status")
async def status() -> dict[str, object]:
    return manager.status()


@app.get("/api/logs")
async def logs() -> dict[str, str]:
    return {"log": manager.read_log()}


@app.get("/api/packages")
async def packages() -> JSONResponse:
    installed, error = await asyncio.to_thread(list_installed)
    if error:
        return JSONResponse({"ok": False, "error": error, "packages": []}, status_code=500)
    return JSONResponse({"ok": True, "packages": installed})


@app.get("/api/packages/job")
async def package_job() -> dict[str, object]:
    return installer.snapshot()


@app.post("/api/packages/install")
async def install_packages(body: RequirementsIn) -> JSONResponse:
    specs, error = parse_requirements(body.requirements)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    error = installer.start(specs or [], action="install")
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=409)
    return JSONResponse({"ok": True})


@app.post("/api/packages/uninstall")
async def uninstall_package(body: PackageNameIn) -> JSONResponse:
    name, error = parse_package_name(body.name)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    error = installer.start([name or ""], action="uninstall")
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=409)
    return JSONResponse({"ok": True})


@app.post("/save")
async def save(body: CodeIn) -> JSONResponse:
    result = manager.save(body.code)
    status_code = 200 if result["ok"] else 400
    return JSONResponse(result, status_code=status_code)


@app.post("/publish")
async def publish(body: CodeIn) -> JSONResponse:
    result = await asyncio.to_thread(manager.publish, body.code)
    status_code = 200 if result["ok"] else 400
    return JSONResponse(result, status_code=status_code)


@app.api_route("/app/{app_id}", methods=PROXY_METHODS, response_model=None)
@app.api_route("/app/{app_id}/{path:path}", methods=PROXY_METHODS, response_model=None)
async def proxy(app_id: str, request: Request, path: str = "") -> StreamingResponse | HTMLResponse:
    if app_id != APP_ID:
        return HTMLResponse("未知应用", status_code=404)
    endpoint = manager.endpoint()
    if endpoint is None:
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>Preview</title>"
            "<p style='font:14px sans-serif;color:#444'>还没有发布。先在左侧写好代码，再点发布。</p>",
            status_code=503,
        )

    host, port = endpoint
    target = f"http://{host}:{port}/{path.lstrip('/')}"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    headers = [
        (key, value)
        for key, value in request.headers.items()
        if key.lower() not in _DROPPED_REQUEST_HEADERS
    ]
    public_host = request.headers.get("host", "127.0.0.1")
    headers.append(("accept-encoding", "identity"))
    headers.append(("host", public_host))
    headers.append(("x-forwarded-host", public_host))
    headers.append(("x-forwarded-proto", request.url.scheme))
    headers.append(("x-forwarded-prefix", PREVIEW_PATH.rstrip("/")))

    client: httpx.AsyncClient = request.app.state.http
    upstream_request = client.build_request(
        request.method,
        target,
        headers=headers,
        content=request.stream(),
    )
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.RequestError:
        return HTMLResponse("预览进程没有响应", status_code=502)

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    response = StreamingResponse(relay(), status_code=upstream.status_code)
    for key, value in upstream.headers.multi_items():
        if key.lower() in _DROPPED_RESPONSE_HEADERS:
            continue
        response.headers.append(key, value)
    response.headers.setdefault("cache-control", "no-store")
    return response
