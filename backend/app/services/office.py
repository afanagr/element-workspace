"""OnlyOffice Document Server integration helpers.

The Document Server never sees our session tokens: every document open gets
short-lived signed links for downloading the file and for the save callback.
Editor configs and callbacks are additionally signed with the shared JWT secret
that the Document Server is configured to require.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from app.config import settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models import Node, User

# Extensions that can be edited in the Document Server. Legacy formats (.doc,
# .xls, .ppt) are intentionally excluded: OnlyOffice would save them back as
# OOXML and silently change the file format behind the user's back.
OFFICE_EXTENSIONS: dict[str, str] = {
    ".docx": "word",
    ".odt": "word",
    ".rtf": "word",
    ".xlsx": "cell",
    ".xlsm": "cell",
    ".ods": "cell",
    ".csv": "cell",
    ".pptx": "slide",
    ".odp": "slide",
    ".pdf": "pdf",
}


class OfficeError(Exception):
    """Raised when an OnlyOffice link or editor request is invalid."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# --------------------------------------------------------------------- links
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _link_key() -> bytes:
    return f"office-link:{settings.app_secret_key}".encode()


def sign_link(
    node_id: uuid.UUID | str, kind: str, ttl: int, *, user_id: uuid.UUID | str | None = None
) -> str:
    """Create a compact HMAC-signed token for a download or callback URL."""
    payload: dict[str, Any] = {
        "n": str(node_id),
        "k": kind,
        "e": int(time.time()) + max(ttl, 60),
    }
    if user_id is not None:
        payload["u"] = str(user_id)
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(_link_key(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64url(signature)}"


def verify_link(token: str, kind: str) -> dict[str, Any]:
    body, _, signature = token.partition(".")
    if not body or not signature:
        raise OfficeError("Malformed document link", status_code=403)
    expected = hmac.new(_link_key(), body.encode(), hashlib.sha256).digest()
    try:
        provided = _b64url_decode(signature)
    except Exception as exc:  # noqa: BLE001
        raise OfficeError("Malformed document link", status_code=403) from exc
    if not hmac.compare_digest(expected, provided):
        raise OfficeError("Invalid document link", status_code=403)
    try:
        payload = json.loads(_b64url_decode(body))
    except Exception as exc:  # noqa: BLE001
        raise OfficeError("Malformed document link", status_code=403) from exc
    if payload.get("k") != kind:
        raise OfficeError("Wrong document link type", status_code=403)
    if int(payload.get("e") or 0) < int(time.time()):
        raise OfficeError("Document link has expired, reopen the file", status_code=403)
    return payload


# ----------------------------------------------------------------------- jwt
def _secret() -> bytes:
    return settings.onlyoffice_jwt_secret.encode()


def encode_jwt(payload: dict[str, Any]) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(_secret(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(signature)}"


def decode_jwt(token: str) -> dict[str, Any]:
    header, _, rest = token.partition(".")
    body, _, signature = rest.partition(".")
    if not header or not body or not signature:
        raise OfficeError("Malformed callback token", status_code=403)
    expected = hmac.new(_secret(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    try:
        provided = _b64url_decode(signature)
    except Exception as exc:  # noqa: BLE001
        raise OfficeError("Malformed callback token", status_code=403) from exc
    if not hmac.compare_digest(expected, provided):
        raise OfficeError("Invalid callback token", status_code=403)
    try:
        payload = json.loads(_b64url_decode(body))
    except Exception as exc:  # noqa: BLE001
        raise OfficeError("Malformed callback token", status_code=403) from exc
    expires = payload.get("exp")
    if expires is not None and int(expires) < int(time.time()):
        raise OfficeError("Callback token has expired", status_code=403)
    return payload


# ------------------------------------------------------------------- config
def build_editor_config(node: "Node", user: "User") -> dict[str, Any]:
    """Return ``{config, documentServerUrl}`` for ``DocsAPI.DocEditor``."""
    from app.services.parsing import extension_of

    if not settings.office_configured:
        raise OfficeError("OnlyOffice is not configured", status_code=503)
    ext = extension_of(node.name)
    document_type = OFFICE_EXTENSIONS.get(ext)
    if document_type is None:
        raise OfficeError("This file type is not supported by OnlyOffice", status_code=415)

    download = sign_link(
        node.id,
        "dl",
        settings.onlyoffice_download_ttl,
        user_id=user.id,
    )
    callback = sign_link(
        node.id,
        "cb",
        settings.onlyoffice_callback_ttl,
        user_id=user.id,
    )
    api_base = settings.onlyoffice_api_url.rstrip("/")
    stamp = 0
    if node.updated_at or node.created_at:
        stamp = int((node.updated_at or node.created_at).timestamp())
    config: dict[str, Any] = {
        "document": {
            "fileType": ext.lstrip("."),
            "key": f"{node.id}-{stamp}",
            "title": node.name,
            "url": f"{api_base}/api/office/download/{download}",
            "permissions": {
                "edit": True,
                "download": True,
                "print": True,
                "review": True,
                "comment": True,
            },
        },
        "documentType": document_type,
        "type": "desktop",
        "width": "100%",
        "height": "100%",
        "editorConfig": {
            "callbackUrl": f"{api_base}/api/office/callback/{callback}",
            "lang": settings.onlyoffice_lang,
            "mode": "edit",
            "user": {
                "id": str(user.id),
                "name": user.display_name or user.username,
            },
            "customization": {
                "autosave": True,
                "forcesave": True,
                "compactHeader": False,
                "hideRightMenu": True,
                "uiTheme": "theme-light",
            },
        },
    }
    config["token"] = encode_jwt(config)
    return {"config": config, "documentServerUrl": settings.onlyoffice_url.rstrip("/")}


# ------------------------------------------------------------------ payloads
def is_zip_bytes(data: bytes) -> bool:
    return data[:4] == b"PK\x03\x04"


def spreadsheet_to_csv(data: bytes) -> bytes:
    """Convert an XLSX returned by OnlyOffice back into the original CSV file.

    Values are exported, not formulas: a CSV cell cannot hold a formula, so the
    saved document keeps the computed result (same as opening it in Excel and
    choosing "Save as CSV").
    """
    import csv
    import io

    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        for row in sheet.iter_rows(values_only=True):
            writer.writerow(["" if cell is None else cell for cell in row])
        return buffer.getvalue().encode("utf-8")
    finally:
        workbook.close()


def safe_source_url(url: str) -> str:
    """Validate the file URL a callback asks us to fetch (basic SSRF guard)."""
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise OfficeError("Unsupported document URL", status_code=400)
    host = parsed.hostname.lower()
    if host in settings.onlyoffice_hosts:
        return url
    if host in {"169.254.169.254", "metadata.google.internal"}:
        raise OfficeError("Unsupported document URL", status_code=400)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Docker service names have no dot and are internal by construction.
        if "." not in host:
            return url
        if settings.onlyoffice_hosts:
            raise OfficeError("Document host is not allowed", status_code=400) from None
        return url
    if address.is_private or address.is_loopback:
        return url
    raise OfficeError("Document host is not allowed", status_code=400)
