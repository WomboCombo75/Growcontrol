"""Webcam stream URL validation for built-in mjpg-streamer and external sources."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

_BLOCKED_PATH_EXTENSIONS = frozenset(
    {
        ".exe",
        ".msi",
        ".msp",
        ".msu",
        ".zip",
        ".rar",
        ".7z",
        ".tar",
        ".gz",
        ".bz2",
        ".apk",
        ".deb",
        ".rpm",
        ".dmg",
        ".pkg",
        ".sh",
        ".bash",
        ".bat",
        ".cmd",
        ".ps1",
        ".py",
        ".pl",
        ".jar",
        ".dll",
        ".so",
        ".bin",
        ".iso",
        ".img",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".ppt",
    }
)

_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "multipart/x-mixed-replace",
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
        "image/gif",
        "video/x-motion-jpeg",
        "video/mjpeg",
        "text/html",
    }
)

_BLOCKED_CONTENT_TYPE_PREFIXES = (
    "application/",
    "text/javascript",
    "application/javascript",
    "application/x-",
)

_MAGIC_JPEG = b"\xff\xd8\xff"
_MAGIC_HTML = (b"<!doctype", b"<!DOCTYPE", b"<html", b"<HTML")


def normalize_stream_source(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in ("external", "url", "remote"):
        return "external"
    return "builtin"


def parse_webcam_stream_url(url: str) -> Tuple[str, Any]:
    """Structural checks shared by builtin + external streams. Returns (normalized_url, parsed)."""
    raw = str(url or "").strip()
    if not raw:
        return "", None
    if len(raw) > 2048:
        raise ValueError("webcam stream URL is too long (max 2048 characters)")
    try:
        parsed = urlparse(raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid stream URL: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ValueError("stream URL must use http or https")
    if parsed.username or parsed.password:
        raise ValueError("stream URL must not contain username or password")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError("stream URL is missing a host")
    path_lower = (parsed.path or "").lower()
    for ext in _BLOCKED_PATH_EXTENSIONS:
        if path_lower.endswith(ext):
            raise ValueError(f"stream URL path extension {ext!r} is not allowed for webcam streams")
    query_lower = (parsed.query or "").lower()
    if "download=" in query_lower or "attachment=" in query_lower:
        raise ValueError("stream URL must not look like a file download link")
    return raw, parsed


def _content_type_allowed(content_type: str) -> bool:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if not ct:
        return False
    if ct in _ALLOWED_CONTENT_TYPES:
        return True
    for prefix in _BLOCKED_CONTENT_TYPE_PREFIXES:
        if ct.startswith(prefix):
            return False
    return ct.startswith("image/") or ct.startswith("video/")


def _disposition_blocks_attachment(value: str) -> bool:
    disp = (value or "").strip().lower()
    if not disp:
        return False
    return "attachment" in disp


def _sniff_kind(body: bytes) -> Optional[str]:
    if not body:
        return None
    head = body[:512].lstrip()
    if head.startswith(_MAGIC_JPEG):
        return "mjpeg"
    lower = head.lower()
    for marker in _MAGIC_HTML:
        if lower.startswith(marker.lower()):
            return "html"
    if b"multipart/x-mixed-replace" in lower or b"Content-Type: image/jpeg" in head:
        return "mjpeg"
    return None


def _path_suggests_viewer(parsed: Any) -> bool:
    path = (parsed.path or "").lower()
    if re.search(r"\.html?$", path):
        return True
    query = (parsed.query or "").lower()
    return "action=stream" in query or "mjpeg" in path or "/stream" in path


def probe_external_webcam_url(url: str, timeout_seconds: float = 8.0) -> Dict[str, Any]:
    """
    Probe a remote URL from the Pi to reject obvious downloads / non-stream content.
    The Dashboard still loads the URL in the browser (<img> or sandboxed iframe).
    """
    normalized, parsed = parse_webcam_stream_url(url)
    if not normalized:
        raise ValueError("stream URL is required for external webcam sources")

    headers = {
        "User-Agent": "Growcontrol-Webcam-Validator/1.0",
        "Accept": "multipart/x-mixed-replace, image/*, text/html;q=0.8, */*;q=0.1",
    }
    warnings: List[str] = []
    last_error = ""

    def evaluate_response(resp: requests.Response, body_prefix: bytes) -> Dict[str, Any]:
        ctype = resp.headers.get("Content-Type", "")
        if _disposition_blocks_attachment(resp.headers.get("Content-Disposition", "")):
            raise ValueError("URL responds with Content-Disposition: attachment (file download blocked)")
        if _disposition_blocks_attachment(resp.headers.get("Content-disposition", "")):
            raise ValueError("URL responds with Content-Disposition: attachment (file download blocked)")

        kind: Optional[str] = None
        if _content_type_allowed(ctype):
            ct_main = ctype.split(";", 1)[0].strip().lower()
            if ct_main == "text/html":
                kind = "html"
            elif ct_main.startswith("image/") or "mjpeg" in ct_main:
                kind = "image"
            elif "mixed-replace" in ct_main:
                kind = "mjpeg"
            else:
                kind = "stream"
        else:
            sniffed = _sniff_kind(body_prefix)
            if sniffed:
                kind = sniffed
                warnings.append(f"Content-Type was {ctype or 'missing'}; allowed based on response body")
            elif _path_suggests_viewer(parsed):
                kind = "html" if re.search(r"\.html?$", (parsed.path or ""), re.I) else "stream"
                warnings.append(f"Content-Type was {ctype or 'missing'}; allowed based on URL pattern")
            else:
                raise ValueError(
                    f"URL does not look like a webcam stream (Content-Type: {ctype or 'missing'})"
                )

        return {
            "ok": True,
            "stream_url": normalized,
            "content_type": ctype or None,
            "content_kind": kind,
            "warnings": warnings,
        }

    session = requests.Session()
    try:
        head = session.head(normalized, allow_redirects=True, timeout=timeout_seconds, headers=headers)
        if head.status_code < 400 and head.headers.get("Content-Type"):
            try:
                return evaluate_response(head, b"")
            except ValueError as exc:
                last_error = str(exc)
        get = session.get(
            normalized,
            allow_redirects=True,
            timeout=timeout_seconds,
            headers={**headers, "Range": "bytes=0-2047"},
            stream=True,
        )
        chunk = b""
        try:
            for part in get.iter_content(256):
                chunk += part
                if len(chunk) >= 512:
                    break
        finally:
            get.close()
        if get.status_code >= 400:
            raise ValueError(f"URL returned HTTP {get.status_code}")
        return evaluate_response(get, chunk)
    except ValueError:
        raise
    except requests.RequestException as exc:
        hint = f" ({last_error})" if last_error else ""
        raise ValueError(f"Could not reach stream URL: {exc}{hint}") from exc
    finally:
        session.close()


def validate_webcam_stream_entry(
    item: Dict[str, Any],
    *,
    index: int,
    probe_external: bool = True,
) -> Tuple[Dict[str, Any], List[str]]:
    """Normalize one webcam_streams entry; probe external URLs when requested."""
    warnings: List[str] = []
    source = normalize_stream_source(item.get("source"))
    url = str(item.get("stream_url", "")).strip()
    out_kind: Optional[str] = None

    if source == "external":
        if not url:
            raise ValueError(f"webcam_streams[{index}].stream_url is required when source is external")
        parse_webcam_stream_url(url)
        if probe_external:
            probe = probe_external_webcam_url(url)
            warnings.extend(probe.get("warnings") or [])
            out_kind = str(probe.get("content_kind") or "stream")
    elif url:
        parse_webcam_stream_url(url)

    out = dict(item)
    out["source"] = source
    out["stream_url"] = url
    if source == "external" and out_kind:
        out["content_kind"] = out_kind
    else:
        out.pop("content_kind", None)
    return out, warnings
