"""
Oracle Database 19c connectivity with python-oracledb.

- Connection settings come from .env (ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN).
- Thin mode is used by default (no Oracle Client needed).
- If ORACLE_CLIENT_LIB_DIR is set, Thick mode is enabled with Oracle Instant
  Client from that folder (needed for some database security settings, for
  example Native Network Encryption).
- Only parameterized SQL is executed. Passwords are never logged or returned.
- Document upload, page conversion and OCR do not depend on this module, so the
  application works without a database.

Database safety: this module does not create, alter, drop, truncate or rename
any database object, and it does not write to any table.
"""

import logging
import threading

from app import config
from app.errors import ServiceError

logger = logging.getLogger(__name__)

_client_lock = threading.Lock()
_thick_mode_ready = False


def is_configured():
    return bool(config.ORACLE_ENABLED and config.ORACLE_USER and config.ORACLE_PASSWORD and config.ORACLE_DSN)


def get_mode():
    return "thick" if config.ORACLE_CLIENT_LIB_DIR else "thin"


def init_client_mode():
    """Enable Thick mode once if ORACLE_CLIENT_LIB_DIR is set."""
    global _thick_mode_ready
    if not config.ORACLE_CLIENT_LIB_DIR:
        return
    with _client_lock:
        if _thick_mode_ready:
            return
        import oracledb

        oracledb.init_oracle_client(lib_dir=config.ORACLE_CLIENT_LIB_DIR)
        _thick_mode_ready = True
        logger.info("python-oracledb Thick mode enabled (Oracle Client folder: %s)", config.ORACLE_CLIENT_LIB_DIR)


def get_connection():
    """Open a new database connection. The caller must close it (use 'with')."""
    if not config.ORACLE_ENABLED:
        raise ServiceError("Oracle connectivity is disabled (ORACLE_ENABLED=false).", 503, "ORACLE_DISABLED")
    if not is_configured():
        raise ServiceError(
            "Oracle connection settings are incomplete. Set ORACLE_USER, ORACLE_PASSWORD and ORACLE_DSN in .env.",
            503,
            "ORACLE_NOT_CONFIGURED",
        )
    import oracledb

    try:
        init_client_mode()
        return oracledb.connect(
            user=config.ORACLE_USER,
            password=config.ORACLE_PASSWORD,
            dsn=config.ORACLE_DSN,
            tcp_connect_timeout=config.ORACLE_CONNECT_TIMEOUT,
        )
    except oracledb.Error as error:
        # The error text from python-oracledb does not contain the password.
        logger.error("Oracle connection to %s as %s failed: %s", config.ORACLE_DSN, config.ORACLE_USER, error)
        raise ServiceError("Could not connect to the Oracle database.", 503, "ORACLE_CONNECTION_FAILED") from error


def fetch_all(sql, parameters=None):
    """Run a parameterized SELECT statement and return the rows as a list of dicts."""
    import oracledb

    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, parameters or {})
                columns = [column[0].lower() for column in cursor.description or []]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
    except oracledb.Error as error:
        logger.error("Oracle query failed: %s", error)
        raise ServiceError("The database query failed.", 500, "ORACLE_QUERY_FAILED") from error


def test_connection():
    """Check connectivity with a harmless query. Returns a dict without secrets."""
    result = {
        "enabled": config.ORACLE_ENABLED,
        "configured": is_configured(),
        "mode": get_mode(),
        "dsn": config.ORACLE_DSN or None,
        "user": config.ORACLE_USER or None,
        "connected": False,
        "server_version": None,
        "error": None,
    }
    try:
        with get_connection() as connection:
            result["server_version"] = connection.version
            with connection.cursor() as cursor:
                cursor.execute("SELECT :value AS check_value FROM dual", {"value": 1})
                cursor.fetchone()
        result["connected"] = True
    except ServiceError as error:
        result["error"] = error.message
    return result
