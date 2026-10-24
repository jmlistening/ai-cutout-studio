# coding: utf-8
"""本地配置：MediaKit API Key 只保存在本机的 .env 文件里，绝不返回给浏览器。"""

from __future__ import annotations

import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
OUTPUT_DIR = BASE_DIR / "outputs"
ENV_PATH = BASE_DIR / ".env"

APP_NAME = "抠图台"
APP_VERSION = "1.2.0"

DEFAULT_ENDPOINT = "https://mediakit.cn-beijing.volces.com"
DEFAULT_PORT = 8765

API_KEY_ENV = "MEDIAKIT_API_KEY"
ENDPOINT_ENV = "MEDIAKIT_ENDPOINT"


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return values

    # 记事本可能把 .env 存成 ANSI/GBK，Key 本身是 ASCII，兼容读取更稳
    text = ""
    for encoding in ("utf-8-sig", "gbk"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


_KEY_PATTERN = re.compile(r"^\S{8,}$")


def _clean_api_key(value: str) -> str:
    value = (value or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


class Settings:
    """每次保存后立即 reload，不需要重启服务。"""

    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        file_env = _parse_env_file(ENV_PATH)

        def pick(name: str, default: str = "") -> str:
            # 真实环境变量优先，方便高级用户临时覆盖 .env
            return (os.environ.get(name) or file_env.get(name) or default).strip()

        self.api_key = _clean_api_key(pick(API_KEY_ENV))
        self.endpoint = (pick(ENDPOINT_ENV, DEFAULT_ENDPOINT) or DEFAULT_ENDPOINT).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def masked_key(self) -> str:
        key = self.api_key
        if not key:
            return ""
        if len(key) <= 10:
            return key[:2] + "…" + key[-2:]
        return key[:6] + "…" + key[-4:]

    def save_api_key(self, api_key: str) -> None:
        api_key = _clean_api_key(api_key)
        if not _KEY_PATTERN.match(api_key):
            raise ValueError("API Key 格式不正确（不应包含空格）")

        lines = [
            "# 由「抠图台」保存，请勿把此文件分享给别人。",
            "# 在火山引擎 AI MediaKit 控制台的 API Key 页面创建。",
            f"{API_KEY_ENV}={api_key}",
            f"{ENDPOINT_ENV}={self.endpoint or DEFAULT_ENDPOINT}",
        ]

        tmp_path = ENV_PATH.with_name(".env.tmp")
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp_path, ENV_PATH)

        # 避免旧的进程环境变量继续覆盖新值
        os.environ.pop(API_KEY_ENV, None)
        self.reload()


settings = Settings()
