"""Upload hardening: filename sanitization, size cap, clean format errors."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import settings
from app.main import _safe_filename


def test_safe_filename_strips_traversal():
    assert _safe_filename("../../app/main.py") == "main.py"
    assert _safe_filename("/etc/passwd") == "passwd"
    assert _safe_filename("..") == "upload"
    assert _safe_filename(None) == "upload"
    assert _safe_filename("statement (Jan).pdf") == "statement _Jan_.pdf"
    assert len(_safe_filename("x" * 500 + ".pdf")) <= 128


def test_traversal_filename_cannot_escape(client: TestClient, tmp_path):
    r = client.post("/api/ingest",
                    files={"file": ("../../pwned.txt", b"not a statement", "text/plain")})
    # Rejected as an unsupported format, and nothing written outside statements/.
    assert r.status_code == 415
    statements = settings.DATA_DIR / "statements"
    assert (statements / "pwned.txt").exists()
    assert not (settings.DATA_DIR.parent / "pwned.txt").exists()


def test_oversize_upload_rejected(client: TestClient):
    settings.MAX_UPLOAD_MB = 1
    try:
        blob = b"0" * (2 * 1024 * 1024)
        r = client.post("/api/ingest", files={"file": ("big.csv", blob, "text/csv")})
        assert r.status_code == 413
    finally:
        settings.MAX_UPLOAD_MB = 30


def test_unknown_format_is_clean_415(client: TestClient):
    r = client.post("/api/ingest",
                    files={"file": ("mystery.txt", b"hello world", "text/plain")})
    assert r.status_code == 415
    assert r.json()["error"] == "unsupported_format"
