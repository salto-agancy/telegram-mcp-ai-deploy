"""File download and handling utilities."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from io import BytesIO
from urllib.parse import unquote

import httpx

from src.config.server_config import cfg
from src.tools.messages.security import _validate_url_security

logger = logging.getLogger(__name__)

# MIME type → default filename extension for data: URI payloads
_MIME_TO_EXT: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/html": ".html",
    "text/xml": ".xml",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    # Office document types
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
}


def _parse_data_uri(uri: str) -> tuple[str, bytes, str]:
    """
    Parse a data: URI and return (mime_type, decoded_bytes, filename).

    Only base64-encoded data URIs are supported (data:<mime>;base64,<payload>).
    Raises ValueError for invalid URIs, non-base64 encoding, or empty payloads.
    """
    if not uri.startswith("data:"):
        raise ValueError(f"Not a data URI: expected 'data:' scheme, got {uri[:20]}")

    # Format: data:[<mime>][;base64],<payload>
    # Split only on first comma to get header and payload
    comma_idx = uri.find(",", 5)  # skip "data:"
    if comma_idx == -1:
        raise ValueError("Invalid data URI: missing comma separator")

    header = uri[5:comma_idx]  # e.g. "image/png;base64" or ";base64"
    payload = uri[comma_idx + 1:]

    if not payload:
        raise ValueError("Invalid data URI: empty payload")

    # Parse header parts
    parts = header.split(";")
    is_base64 = False
    mime_type = ""
    filename = ""

    for part in parts:
        if part == "base64":
            is_base64 = True
        elif part.startswith("filename="):
            filename = unquote(part[len("filename="):])
        elif part:
            mime_type = part

    if not is_base64:
        raise ValueError(
            "Only base64-encoded data URIs are supported "
            "(use data:<mime>;base64,<payload>)"
        )

    if not mime_type:
        mime_type = "application/octet-stream"

    # Decode base64 payload
    try:
        decoded = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise ValueError(f"Invalid base64 in data URI: {exc}") from exc

    # Enforce size limit
    config = cfg()
    max_bytes = config.max_file_size_mb * 1024 * 1024
    if len(decoded) > max_bytes:
        raise ValueError(
            f"Data URI payload too large: {len(decoded)} bytes "
            f"(max: {max_bytes} bytes)"
        )

    # Derive filename: use explicit filename param if provided, else generate from MIME type
    if not filename:
        ext = _MIME_TO_EXT.get(mime_type, ".bin")
        filename = f"upload{ext}"

    return mime_type, decoded, filename


def is_own_attachment_url(url: str) -> bool:
    config = cfg()
    return bool(
        config.public_base_url_normalized
        and url.startswith(config.public_base_url_normalized)
    )


# Filename suffixes Telethon can reliably treat as photos when force_document=False.
_IMAGE_SUFFIXES = frozenset(
    (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff")
)


def _basename_from_url_or_path(url_or_path: str) -> str:
    """
    Basename of a URL or filesystem path, without a query string.

    Uses os.path.basename so POSIX and Windows path separators both work.
    """
    path_without_query = url_or_path.split("?", 1)[0]
    return os.path.basename(path_without_query)


def _is_likely_image_filename(url_or_path: str) -> bool:
    """Whether the path/URL/data: URI looks like a raster image (for send_file hint)."""
    if url_or_path.startswith("data:"):
        # Parse MIME type from data URI: data:image/png;filename=...;base64,...
        header_end = url_or_path.find(",", 5)
        header = url_or_path[5:header_end] if header_end != -1 else url_or_path[5:]
        mime = ""
        for part in header.split(";"):
            if part and part != "base64" and not part.startswith("filename="):
                mime = part
        return mime.startswith("image/")
    base = _basename_from_url_or_path(url_or_path)
    if not base:
        return False
    lower = base.lower()
    return any(lower.endswith(s) for s in _IMAGE_SUFFIXES)


def force_document_for_file_list(file_list: list[str]) -> bool:
    """
    If False, Telethon may upload as photo(s). If True, send as generic file/document.

    Use True unless every entry looks like an image filename — avoids MediaInvalidError
    when a URL returns HTML or non-photo bytes but Telethon tries photo upload.
    """
    if not file_list:
        return True
    return not all(_is_likely_image_filename(f) for f in file_list)


async def prepare_files_for_send(file_list: list[str]) -> list[BytesIO | str]:
    """
    Resolve files for sending: data: URIs → BytesIO, http(s) URLs → BytesIO, local paths as-is.

    - data: URIs are decoded inline (base64 payload → BytesIO with filename)
    - http(s) URLs are downloaded with security validation
    - Local paths are kept as-is (stdio mode only, validated elsewhere)
    """
    if not any(
        f.startswith(("http://", "https://", "data:")) for f in file_list
    ):
        return file_list

    url_entries = [f for f in file_list if f.startswith(("http://", "https://"))]
    downloaded = await _download_urls_to_bytes(url_entries) if url_entries else []
    url_to_content: dict[str, bytes | str] = dict(
        zip(url_entries, downloaded, strict=True)
    )
    out: list[BytesIO | str] = []
    for f in file_list:
        if f.startswith("data:"):
            _mime_type, data, filename = _parse_data_uri(f)
            file_obj = BytesIO(data)
            file_obj.name = filename
            out.append(file_obj)
        elif f.startswith(("http://", "https://")):
            content = url_to_content[f]
            if isinstance(content, bytes):
                filename = _basename_from_url_or_path(f) or "file"
                file_obj = BytesIO(content)
                file_obj.name = filename
                out.append(file_obj)
            else:
                out.append(content)
        else:
            out.append(f)
    return out


async def _download_single_file(
    http_client: httpx.AsyncClient, url: str
) -> bytes | str:
    """Download a single file from URL with security validation."""

    is_safe, error_msg = _validate_url_security(url)
    if not is_safe:
        raise ValueError(f"Unsafe URL blocked: {error_msg}")

    if url.startswith(("http://", "https://")):
        logger.debug(f"Downloading file from {url}")
        try:
            response = await http_client.get(url, follow_redirects=False)
            content_length = response.headers.get("content-length")
            config = cfg()
            max_size_bytes = config.max_file_size_mb * 1024 * 1024

            if content_length and int(content_length) > max_size_bytes:
                raise ValueError(
                    f"File too large: {content_length} bytes (max: {max_size_bytes} bytes)"
                )

            response.raise_for_status()
            content = response.content

            if len(content) > max_size_bytes:
                raise ValueError(
                    f"Downloaded file too large: {len(content)} bytes (max: {max_size_bytes} bytes)"
                )

            return content

        except Exception as e:
            raise ValueError(f"Failed to download {url}: {e!s}") from e

    return url


async def _download_urls_to_bytes(file_list: list[str]) -> list[bytes | str]:
    """
    Download files from URLs as bytes in parallel with enhanced security.

    Returns list of file contents as bytes or local paths.
    Raises ValueError with specific URL if download fails.
    """
    timeout = httpx.Timeout(30.0, connect=10.0)
    limits = httpx.Limits(
        max_connections=10,
        max_keepalive_connections=2,
    )

    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=False,
        headers={
            "User-Agent": "fast-mcp-telegram/1.0",
            "Accept": "*/*",
        },
    ) as http_client:
        tasks = [_download_single_file(http_client, url) for url in file_list]
        return await asyncio.gather(*tasks)


# ---------- Large files (video): disk spool + background send ----------
# Small files keep the old in-memory path (max_file_size_mb). Anything bigger
# (up to TG_LARGE_FILE_MAX_MB, Telegram's 2 GB) is streamed to disk — the
# container has only ~320 MB RAM — and sent in a background task, because a
# 1 GB upload outlives any MCP request (Cloudflare cuts at ~100 s).

LARGE_SPOOL_DIR = "/tmp/tgmcp-large"
_VIDEO_SUFFIXES = frozenset((".mp4", ".m4v", ".mov"))


def large_file_max_bytes() -> int:
    return int(os.getenv("TG_LARGE_FILE_MAX_MB", "2000")) * 1024 * 1024


def is_video_filename(url_or_path: str) -> bool:
    base = _basename_from_url_or_path(url_or_path).lower()
    return any(base.endswith(s) for s in _VIDEO_SUFFIXES)


async def find_large_urls(file_list: list[str]) -> list[str]:
    """http(s) URLs whose Content-Length exceeds max_file_size_mb (HEAD request)."""
    small_max = cfg().max_file_size_mb * 1024 * 1024
    large: list[str] = []
    urls = [
        f for f in file_list
        if f.startswith(("http://", "https://")) and not is_own_attachment_url(f)
    ]
    if not urls:
        return large
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False,
        headers={"User-Agent": "fast-mcp-telegram/1.0"},
    ) as http_client:
        for url in urls:
            is_safe, error_msg = _validate_url_security(url)
            if not is_safe:
                raise ValueError(f"Unsafe URL blocked: {error_msg}")
            try:
                resp = await http_client.head(url)
                size = int(resp.headers.get("content-length") or 0)
            except Exception:  # HEAD unsupported → fall back to the normal path
                continue
            if size > small_max:
                if size > large_file_max_bytes():
                    raise ValueError(
                        f"File too large for Telegram: {size} bytes "
                        f"(max: {large_file_max_bytes()} bytes)"
                    )
                large.append(url)
    return large


async def download_url_to_disk(url: str) -> str:
    """Stream a URL to a temp file (no RAM spike). Returns the path."""
    import tempfile

    is_safe, error_msg = _validate_url_security(url)
    if not is_safe:
        raise ValueError(f"Unsafe URL blocked: {error_msg}")
    os.makedirs(LARGE_SPOOL_DIR, exist_ok=True)
    name = _basename_from_url_or_path(url) or "file"
    fd, path = tempfile.mkstemp(prefix="dl-", suffix=f"-{unquote(name)}", dir=LARGE_SPOOL_DIR)
    limit = large_file_max_bytes()
    written = 0
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=False,
            headers={"User-Agent": "fast-mcp-telegram/1.0"},
        ) as http_client, http_client.stream("GET", url) as resp:
            resp.raise_for_status()
            with os.fdopen(fd, "wb") as out:
                async for chunk in resp.aiter_bytes(4 * 1024 * 1024):
                    written += len(chunk)
                    if written > limit:
                        raise ValueError(f"File too large: > {limit} bytes")
                    out.write(chunk)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def mp4_video_info(path: str) -> tuple[int, int, int] | None:
    """(duration_sec, width, height) from MP4 boxes (mvhd + video tkhd).

    No ffprobe/hachoir in the image, and without these Telegram shows the video
    as 0:00 with no proper player. Reads only box headers, never the media data.
    """
    import struct

    def boxes(f, start: int, end: int):
        pos = start
        while pos + 8 <= end:
            f.seek(pos)
            hdr = f.read(8)
            if len(hdr) < 8:
                return
            size, typ = struct.unpack(">I4s", hdr)
            hlen = 8
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0]
                hlen = 16
            elif size == 0:
                size = end - pos
            if size < hlen:
                return
            yield typ, pos + hlen, pos + size
            pos += size

    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            total = f.tell()
            moov = next(((s, e) for t, s, e in boxes(f, 0, total) if t == b"moov"), None)
            if not moov:
                return None
            duration = 0
            width = height = 0
            for typ, s, e in boxes(f, *moov):
                if typ == b"mvhd":
                    f.seek(s)
                    ver = f.read(1)[0]
                    if ver == 1:
                        f.seek(s + 4 + 16)
                        timescale, dur = struct.unpack(">IQ", f.read(12))
                    else:
                        f.seek(s + 4 + 8)
                        timescale, dur = struct.unpack(">II", f.read(8))
                    duration = int(dur / timescale) if timescale else 0
                elif typ == b"trak" and not width:
                    for t2, s2, e2 in boxes(f, s, e):
                        if t2 == b"tkhd":
                            f.seek(e2 - 8)
                            w, h = struct.unpack(">II", f.read(8))
                            if w >> 16 and h >> 16:
                                width, height = w >> 16, h >> 16
            if duration and width and height:
                return duration, width, height
    except Exception:
        logger.debug("mp4_video_info failed for %s", path, exc_info=True)
    return None


def _calculate_file_count(files: str | list[str] | None) -> int:
    """Calculate the number of files in the files parameter."""
    if not files:
        return 0
    return len(files) if isinstance(files, list) else 1


def _wrap_bytes_in_file_objects(
    file_list: list[str], downloaded_files: list[bytes | str]
) -> list:
    """
    Wrap downloaded bytes in BytesIO objects with proper filenames.

    Extracts original filenames from URLs for proper file type detection.
    """
    file_objects = []
    for i, content in enumerate(downloaded_files):
        if isinstance(content, bytes):
            filename = _basename_from_url_or_path(file_list[i]) or "file"
            file_obj = BytesIO(content)
            file_obj.name = filename
            file_objects.append(file_obj)
        else:
            file_objects.append(content)
    return file_objects
