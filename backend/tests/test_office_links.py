"""OnlyOffice signed links carry the file version for stale-save protection."""

from __future__ import annotations

from app.services.office import sign_link, verify_link


def test_callback_link_carries_file_version() -> None:
    token = sign_link("node-1", "cb", 600, user_id="user-1", version="abc123")
    payload = verify_link(token, "cb")
    assert payload["n"] == "node-1"
    assert payload["u"] == "user-1"
    assert payload["v"] == "abc123"


def test_download_link_does_not_need_a_version() -> None:
    token = sign_link("node-1", "dl", 600)
    payload = verify_link(token, "dl")
    assert "v" not in payload
