"""Read/write Growcontrol secrets in the project .env file (not settings.json)."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Tuple

REPO_ROOT = Path(__file__).resolve().parent
ENV_FILE = REPO_ROOT / ".env"
_ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def read_env_file(path: Path | None = None) -> Dict[str, str]:
    p = path or ENV_FILE
    out: Dict[str, str] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ENV_LINE_RE.match(stripped)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def write_env_var(name: str, value: str, path: Path | None = None) -> None:
    """Set or replace one KEY=value line in .env (creates file if missing)."""
    p = path or ENV_FILE
    key = name.strip()
    if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError(f"invalid env var name: {name!r}")
    val = value.strip()
    if re.search(r"[\r\n]", val):
        raise ValueError("env value must not contain newlines")
    rendered = val if re.fullmatch(r"[A-Za-z0-9_./:-]+", val) else repr(val)
    new_line = f"{key}={rendered}"

    lines: list[str] = []
    if p.is_file():
        lines = p.read_text(encoding="utf-8").splitlines()
    replaced = False
    out_lines: list[str] = []
    for line in lines:
        if _ENV_LINE_RE.match(line.strip()) and line.strip().split("=", 1)[0] == key:
            if not replaced:
                out_lines.append(new_line)
                replaced = True
            continue
        out_lines.append(line)
    if not replaced:
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append(new_line)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(p, 0o600)
    os.environ[key] = val


def openweather_api_key_from_env() -> str:
    return os.getenv("OPENWEATHER_API_KEY", "").strip()


def openweather_api_key_configured() -> bool:
    return bool(openweather_api_key_from_env())


def set_openweather_api_key(key: str) -> None:
    write_env_var("OPENWEATHER_API_KEY", key.strip())


def migrate_openweather_key_from_settings(settings: Dict[str, object]) -> Tuple[Dict[str, object], bool]:
    """
    Move legacy openweather_api_key from settings.json into .env and remove from settings.
    Returns (settings, changed).
    """
    if "openweather_api_key" not in settings:
        return settings, False
    legacy = str(settings.pop("openweather_api_key") or "").strip()
    if not legacy:
        return settings, True
    if not openweather_api_key_configured():
        set_openweather_api_key(legacy)
    return settings, True
