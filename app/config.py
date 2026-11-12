"""
Application configuration.

Values are read from environment variables. A local .env file in the project
folder is loaded automatically (it never overrides variables that are already
set in the environment). See .env.example for every supported variable.

Relative paths are resolved against the project folder, so the same .env
values work on Windows and Linux.
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_DIR / ".env", override=False)


def read_text(name, default=""):
    """Read an environment variable as text. Empty or missing values give the default."""
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def read_int(name, default, minimum=None, maximum=None):
    """Read an environment variable as a whole number and check the optional minimum and maximum."""
    raw_value = read_text(name, str(default))
    try:
        value = int(raw_value)
    except ValueError:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw_value!r}.")
    if minimum is not None and value < minimum:
        raise ValueError(f"Environment variable {name} must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"Environment variable {name} must be at most {maximum}.")
    return value


def read_bool(name, default):
    """Read an environment variable as true/false (accepts 1/0, true/false, yes/no, on/off)."""
    raw_value = read_text(name, "true" if default else "false").lower()
    if raw_value in ("1", "true", "yes", "on"):
        return True
    if raw_value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"Environment variable {name} must be true or false, got {raw_value!r}.")


def read_path(name, default):
    """Read a folder path. A relative path is resolved against the project folder."""
    path = Path(read_text(name, default)).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.resolve()


# Application
APP_NAME = "Document Automation"
APP_VERSION = "3.2.2"
APP_ENV = read_text("APP_ENV", "development")
APP_HOST = read_text("APP_HOST", "127.0.0.1")
APP_PORT = read_int("APP_PORT", 8000, 1, 65535)
LOG_LEVEL = read_text("LOG_LEVEL", "INFO").upper()

# Storage
TEMPLATES_DIR = PROJECT_DIR / "app" / "templates"
STATIC_DIR = PROJECT_DIR / "app" / "static"
STORAGE_DIR = read_path("STORAGE_DIR", "storage")
DOCUMENTS_DIR = STORAGE_DIR / "documents"
TEMP_DIR = STORAGE_DIR / "temp"
LOG_DIR = read_path("LOG_DIR", "logs")

# Document upload
# There is no limit on file size or page count. MAX_PAGE_PIXELS only protects memory:
# it is the largest page image (width x height) allowed, about 180 MB.
MAX_PAGE_PIXELS = read_int("MAX_PAGE_PIXELS", 60_000_000, 1_000_000, 2_000_000_000)

# Allowed file extensions and the file type each one stands for.
ALLOWED_EXTENSIONS = {
    ".pdf": "pdf",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".bmp": "bmp",
    ".tif": "tiff",
    ".tiff": "tiff",
}

# DPI limits for turning PDF pages into images (fixed by the project requirements).
PDF_DPI_MIN = 50
PDF_DPI_MAX = 300
PDF_DPI_DEFAULT = 75

# OCR
# "onnxruntime" works on Windows 11 and Oracle Linux 7.9.
# "paddle" works on Windows 11 only (PaddlePaddle does not run on Oracle Linux 7.9).
OCR_ENGINE = read_text("OCR_ENGINE", "onnxruntime").lower()
OCR_DEVICE = read_text("OCR_DEVICE", "cpu").lower()
OCR_TEXT_DETECTION_MODEL = read_text("OCR_TEXT_DETECTION_MODEL", "PP-OCRv6_small_det")
OCR_TEXT_RECOGNITION_MODEL = read_text("OCR_TEXT_RECOGNITION_MODEL", "PP-OCRv6_small_rec")
OCR_MODEL_DIR = read_path("OCR_MODEL_DIR", "models")
OCR_CPU_THREADS = read_int("OCR_CPU_THREADS", 4, 1, 256)
# Keep this off: with the "paddle" engine on CPU, turning it on causes a known error.
OCR_ENABLE_MKLDNN = read_bool("OCR_ENABLE_MKLDNN", False)
# PDF pages that were not read completely are rendered again at this DPI and read again (0 = off).
OCR_DETAIL_DPI = read_int("OCR_DETAIL_DPI", 150, 0, PDF_DPI_MAX)
if OCR_DETAIL_DPI and OCR_DETAIL_DPI < PDF_DPI_MIN:
    raise ValueError(f"Environment variable OCR_DETAIL_DPI must be 0 or between {PDF_DPI_MIN} and {PDF_DPI_MAX}.")

# Invoice data extraction
# How a date such as 03/04/2025 is read when the invoice itself does not make
# it clear: "dmy" = day/month/year (3 April), "mdy" = month/day/year (March 4).
INVOICE_DATE_ORDER = read_text("INVOICE_DATE_ORDER", "dmy").lower()
if INVOICE_DATE_ORDER not in ("dmy", "mdy"):
    raise ValueError("Environment variable INVOICE_DATE_ORDER must be dmy or mdy.")

# Oracle Database 19c (optional)
ORACLE_ENABLED = read_bool("ORACLE_ENABLED", False)
ORACLE_USER = read_text("ORACLE_USER")
ORACLE_PASSWORD = read_text("ORACLE_PASSWORD")
ORACLE_DSN = read_text("ORACLE_DSN")
# Empty: connect without installing Oracle Client ("Thin" mode).
# A folder path: use the Oracle Instant Client in that folder ("Thick" mode).
ORACLE_CLIENT_LIB_DIR = read_text("ORACLE_CLIENT_LIB_DIR")
# Thick mode only: folder that holds sqlnet.ora and tnsnames.ora (empty = the client's default).
ORACLE_CONFIG_DIR = str(read_path("ORACLE_CONFIG_DIR", "")) if read_text("ORACLE_CONFIG_DIR") else ""
ORACLE_CONNECT_TIMEOUT = read_int("ORACLE_CONNECT_TIMEOUT", 10, 1, 300)
# Owner of the staging tables, if it is not the app's own database user (empty = the app's user).
ORACLE_SCHEMA = read_text("ORACLE_SCHEMA").upper()
if ORACLE_SCHEMA and not re.match(r"^[A-Z][A-Z0-9_$#]{0,127}$", ORACLE_SCHEMA):
    raise ValueError("Environment variable ORACLE_SCHEMA must be a plain Oracle schema name.")

# Reviewer name written to the database (the app has no login, so one name per server).
APP_USER_NAME = read_text("APP_USER_NAME", "document-automation")[:100]
