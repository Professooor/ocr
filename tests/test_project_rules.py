"""Project-wide rules: document-only terminology, storage safety, Oracle safety."""

import logging
import re

import pytest

from app import config
from app.errors import ServiceError
from app.services import oracle_db_service, storage_service

SOURCE_FOLDERS = ["app", "scripts", "tests"]
SOURCE_SUFFIXES = {".py", ".html", ".css", ".js"}


def source_files():
    files = [config.PROJECT_DIR / "main.py"]
    for folder in SOURCE_FOLDERS:
        files.extend(p for p in (config.PROJECT_DIR / folder).rglob("*") if p.suffix in SOURCE_SUFFIXES)
    return files


# Only the files below may use the word "invoice". Generic parts (upload, PDF, OCR,
# storage, Oracle, shared files) must stay generic so they can be reused for any document.
INVOICE_WORDING_ALLOWED = {
    "invoice_data_api.py",
    "invoice_posting_api.py",
    "test_document_delete.py",
    "invoice_posting_service.py",
    "test_invoice_posting.py",
    "invoice_data_service.py",
    "invoice_extraction_service.py",
    "review.html",
    "review.js",
    "review.css",
    "sample_invoice.py",
    "test_invoice_data.py",
    "test_invoice_extraction.py",
    "test_project_rules.py",
    "test_api_errors.py",
    "test_frontend.py",
    "main.py",
    "config.py",
    "document_metadata_service.py",
    "base.js",
    "base.css",
    "base.html",
    "frontend_api.py",
}


def test_generic_modules_stay_free_of_invoice_wording():
    word = "in" + "voice"
    offenders = [
        p.name for p in source_files()
        if p.name not in INVOICE_WORDING_ALLOWED
        and (word in p.read_text(encoding="utf-8").lower() or word in p.name.lower())
    ]
    assert offenders == []


def test_no_destructive_sql_in_source():
    pattern = re.compile(r"\b(CREATE|ALTER|DROP|TRUNCATE|RENAME)\s+(TABLE|INDEX|VIEW|SEQUENCE|SYNONYM)\b", re.I)
    for path in source_files():
        if path.name == "test_project_rules.py":
            continue
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("report.pdf", "report.pdf"),
        ("C:\\Users\\someone\\scan.png", "scan.png"),
        ("../../etc/passwd", "passwd"),
        ("bad<>:\"|?*name.pdf", "badname.pdf"),
        ("", "document"),
        ("....", "document"),
        ("a" * 300 + ".pdf", "a" * 196 + ".pdf"),
    ],
)
def test_safe_display_filename(raw, expected):
    assert storage_service.safe_display_filename(raw) == expected


@pytest.mark.parametrize("bad_id", ["", "abc", "../x", "A" * 32, "0" * 31, None, 123])
def test_document_id_validation(bad_id):
    with pytest.raises(ServiceError):
        storage_service.get_document_dir(bad_id)


def test_remove_path_refuses_outside_storage(tmp_path):
    outside = tmp_path / "keep.txt"
    outside.write_text("keep", encoding="utf-8")
    with pytest.raises(RuntimeError):
        storage_service.remove_path(outside)
    assert outside.exists()


def test_paths_are_cross_platform():
    assert config.STORAGE_DIR.is_absolute()
    for path in source_files():
        if path.suffix == ".py" and path.name != "test_project_rules.py":
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"['\"][A-Za-z]:\\\\", text), f"Hardcoded Windows path in {path.name}"


def test_oracle_disabled_by_default():
    result = oracle_db_service.test_connection()
    assert result["connected"] is False
    assert "disabled" in result["error"]


def test_oracle_failure_never_exposes_password(monkeypatch, caplog):
    secret = "Very-Secret-Password-123"
    monkeypatch.setattr(config, "ORACLE_ENABLED", True)
    monkeypatch.setattr(config, "ORACLE_USER", "test_user")
    monkeypatch.setattr(config, "ORACLE_PASSWORD", secret)
    monkeypatch.setattr(config, "ORACLE_DSN", "127.0.0.1:1/NOSERVICE")
    monkeypatch.setattr(config, "ORACLE_CONNECT_TIMEOUT", 2)
    monkeypatch.setattr(config, "ORACLE_CLIENT_LIB_DIR", "")

    with caplog.at_level(logging.DEBUG):
        result = oracle_db_service.test_connection()

    assert result["connected"] is False
    assert result["error"] == "Could not connect to the Oracle database."
    assert secret not in str(result)
    assert secret not in caplog.text


def test_env_example_has_no_secrets():
    text = (config.PROJECT_DIR / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^ORACLE_PASSWORD=$", text, re.M)
    assert re.search(r"^ORACLE_ENABLED=false$", text, re.M)
