# coding: utf-8
"""抠图台：本地网页工具，调用火山引擎 AI MediaKit 智能抠图移除图片背景。"""

from __future__ import annotations

import io
import re
import socket
import threading
import unicodedata
import uuid
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import uvicorn
from PIL import Image
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from config import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_PORT,
    OUTPUT_DIR,
    STATIC_DIR,
    settings,
)
from image_utils import (
    MAX_INPUT_BYTES,
    SCENE_LIMITS,
    ImageInputError,
    normalize_result_image,
    prepare_upload,
)
from mediakit_client import MediaKitError, VALID_SCENES, segment_image

LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
INVALID_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


class ApiError(Exception):
    def __init__(self, status_code: int, message: str, code: str = "BAD_REQUEST"):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


class ApiKeyPayload(BaseModel):
    api_key: str = ""


class ZipItem(BaseModel):
    id: str
    name: str = ""


class ZipPayload(BaseModel):
    items: list[ZipItem]


def _error_response(status_code: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )


@app.exception_handler(ApiError)
async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return _error_response(exc.status_code, exc.message, exc.code)


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return _error_response(400, "请求参数不正确，请刷新页面后再试", "BAD_REQUEST")


@app.exception_handler(MediaKitError)
async def _mediakit_error_handler(_: Request, exc: MediaKitError) -> JSONResponse:
    if exc.code == "NOT_CONFIGURED":
        status = 400
    elif exc.code == "MEDIAKIT_AUTH":
        status = 401
    elif exc.code == "MEDIAKIT_PERMISSION":
        status = 403
    elif exc.code == "MEDIAKIT_BILLING":
        status = 402
    elif exc.code == "MEDIAKIT_RATE_LIMIT" or exc.status_code == 429:
        status = 429
    elif exc.code == "MEDIAKIT_BAD_IMAGE":
        status = 422
    elif exc.status_code == 504:
        status = 504
    elif exc.status_code in (401, 403):
        status = 401
    else:
        status = 502
    return _error_response(status, exc.message, exc.code)


@app.middleware("http")
async def _local_only_guard(request: Request, call_next):
    if request.method == "POST" and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin:
            hostname = urlparse(origin).hostname
            if hostname not in LOCAL_HOSTNAMES:
                return _error_response(403, "只接受本机页面的请求", "FORBIDDEN")
        if request.url.path == "/api/cutout":
            raw_length = request.headers.get("content-length")
            if raw_length:
                try:
                    if int(raw_length) > MAX_INPUT_BYTES + 4 * 1024 * 1024:
                        return _error_response(413, "上传内容太大，请换一张小一点的图片", "FILE_TOO_LARGE")
                except ValueError:
                    pass
            if request.headers.get("x-cutout-client") != "1":
                return _error_response(403, "请求来源校验失败，请刷新页面", "FORBIDDEN")
    return await call_next(request)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


def sanitize_filename(name: str, fallback: str = "cutout") -> str:
    name = INVALID_FILENAME.sub("", (name or "").strip())
    # 去掉从右到左等方向控制符，避免 ZIP 里显示成别的扩展名
    name = "".join(ch for ch in name if unicodedata.category(ch) != "Cf")
    name = re.sub(r"\.png$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return fallback
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        name = "_" + name
    return name[:80]


def _status_payload() -> dict:
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "provider": "mediakit",
        "configured": settings.configured,
        "masked_key": settings.masked_key,
        "endpoint": settings.endpoint,
        "limits": {
            "max_input_mb": MAX_INPUT_BYTES // (1024 * 1024),
            "scenes": ["general", "human", "product"],
            "scene_limits": {
                scene: {"long_side": long_side, "short_side": short_side}
                for scene, (long_side, short_side, _) in SCENE_LIMITS.items()
            },
        },
    }


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse(_status_payload(), headers={"Cache-Control": "no-store"})


@app.post("/api/settings")
async def api_save_settings(payload: ApiKeyPayload) -> JSONResponse:
    api_key = payload.api_key.strip()
    if not api_key:
        raise ApiError(400, "请填写 MediaKit API Key", "INVALID_CREDENTIALS")
    try:
        settings.save_api_key(api_key)
    except ValueError as exc:
        raise ApiError(400, str(exc), "INVALID_CREDENTIALS")
    except OSError:
        raise ApiError(500, "凭证保存失败，请检查目录写入权限", "SAVE_FAILED")
    return JSONResponse(_status_payload(), headers={"Cache-Control": "no-store"})


@app.post("/api/cutout")
async def api_cutout(
    file: UploadFile = File(...),
    scene: str = Form("general"),
    output_mode: str = Form("original"),
    name: str = Form(""),
) -> JSONResponse:
    if not settings.configured:
        raise ApiError(
            400,
            "还没有配置 MediaKit API Key，请点击右上角「凭证设置」填写",
            "NOT_CONFIGURED",
        )
    if scene not in VALID_SCENES:
        raise ApiError(400, "不支持的抠图场景", "BAD_SCENE")

    raw = await file.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ApiError(
            413,
            f"图片超过 {MAX_INPUT_BYTES // (1024 * 1024)} MB，请压缩后再试",
            "FILE_TOO_LARGE",
        )

    crop_subject = output_mode == "crop"

    try:
        prepared = await run_in_threadpool(prepare_upload, raw, scene)
    except ImageInputError as exc:
        raise ApiError(400, str(exc), "IMAGE_INVALID")

    result = await segment_image(
        prepared,
        scene=scene,
        crop_subject=crop_subject,
        settings=settings,
    )

    try:
        png_bytes, width, height = await run_in_threadpool(
            normalize_result_image, result.image_bytes, crop_subject
        )
    except ImageInputError as exc:
        raise ApiError(502, str(exc), "UPSTREAM_BAD_IMAGE")

    file_id = uuid.uuid4().hex
    try:
        await run_in_threadpool(_write_output, file_id, png_bytes)
    except OSError:
        raise ApiError(
            500,
            "抠图已经成功，但结果保存失败。请检查 outputs 文件夹是否可写、磁盘是否已满",
            "SAVE_FAILED",
        )

    display_name = sanitize_filename(name) if name else sanitize_filename(
        Path(file.filename or "").stem or "cutout"
    )

    return JSONResponse(
        {
            "id": file_id,
            "url": f"/files/{file_id}",
            "name": display_name,
            "width": width,
            "height": height,
            "size": len(png_bytes),
            "request_id": result.request_id,
            "task_id": result.task_id,
            "elapsed": round(result.elapsed, 2),
            "mode": "crop" if crop_subject else "original",
            "scene": scene,
            "input": {
                "width": prepared.original_width,
                "height": prepared.original_height,
                "sent_width": prepared.width,
                "sent_height": prepared.height,
                "resized": prepared.resized,
                "transcoded": prepared.transcoded,
            },
        },
        headers={"Cache-Control": "no-store"},
    )


def _write_output(file_id: str, data: bytes) -> Path:
    # outputs 可能在运行期间被清理掉，写文件前确保它存在
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{file_id}.png"
    path.write_bytes(data)
    return path


def _image_size(path: Path) -> tuple[int, int] | None:
    """读取成品 PNG 的实际宽高；读不出来时返回 None。"""
    try:
        with Image.open(path) as image:
            return image.size
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _build_zip(entries: list[tuple[ZipItem, Path]]) -> bytes:
    # 按分辨率分文件夹，同名去重只在各自文件夹内进行
    used_names: dict[str, set[str]] = {}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item, path in entries:
            size = _image_size(path)
            folder = f"{size[0]}x{size[1]}" if size else "未知尺寸"
            base = sanitize_filename(item.name or path.stem)
            filename = f"{base}.png"
            used = used_names.setdefault(folder, set())
            counter = 2
            while filename.casefold() in used:
                filename = f"{base} ({counter}).png"
                counter += 1
            used.add(filename.casefold())
            archive.write(path, arcname=f"{folder}/{filename}")
    return buffer.getvalue()


def _output_path(file_id: str) -> Path:
    if not ID_PATTERN.match(file_id):
        raise ApiError(400, "文件标识不合法", "BAD_REQUEST")
    path = OUTPUT_DIR / f"{file_id}.png"
    if not path.is_file():
        raise ApiError(404, "找不到这个抠图结果，请重新处理", "NOT_FOUND")
    return path


@app.get("/files/{file_id}")
async def get_file(file_id: str, download: int = 0, name: str = "") -> FileResponse:
    path = _output_path(file_id)
    headers = {"Cache-Control": "private, max-age=3600"}
    if download:
        filename = sanitize_filename(name or path.stem)
        ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "cutout"
        headers["Content-Disposition"] = (
            f'attachment; filename="{ascii_fallback}.png"; '
            f"filename*=UTF-8''{quote(filename + '.png')}"
        )
    return FileResponse(path, media_type="image/png", headers=headers)


@app.post("/api/zip")
async def api_zip(payload: ZipPayload) -> Response:
    if not payload.items:
        raise ApiError(400, "没有可打包的文件", "EMPTY")
    if len(payload.items) > 100:
        raise ApiError(400, "一次最多打包 100 张", "TOO_MANY")

    entries: list[tuple[ZipItem, Path]] = []
    total_bytes = 0
    for item in payload.items:
        path = _output_path(item.id)
        total_bytes += path.stat().st_size
        if total_bytes > 500 * 1024 * 1024:
            raise ApiError(413, "打包内容超过 500 MB，请分批下载", "ZIP_TOO_LARGE")
        entries.append((item, path))

    content = await run_in_threadpool(_build_zip, entries)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="cutouts-{stamp}.zip"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/health")
async def api_health() -> dict:
    return {"ok": True, "version": APP_VERSION}


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _find_port() -> int:
    import os

    wanted = os.environ.get("PORT")
    if wanted:
        try:
            return int(wanted)
        except ValueError:
            pass
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("找不到可用端口")


def main() -> None:
    port = _find_port()
    url = f"http://127.0.0.1:{port}"
    print()
    print("  ┌────────────────────────────────────────────┐")
    print(f"  │  抠图台已启动  {url:<28}│")
    print("  └────────────────────────────────────────────┘")
    if not settings.configured:
        print("  凭证未配置：打开页面右上角「凭证设置」，填入 MediaKit API Key。")
    else:
        print(f"  凭证已就绪：{settings.masked_key}")
    print("  关闭这个窗口即可退出工具。")
    print()

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
