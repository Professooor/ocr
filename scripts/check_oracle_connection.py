"""
Check the Oracle Database 19c connection configured in .env.

Runs only read-only queries against DUAL and SYS_CONTEXT. It does not create,
change or delete any database object. The password is never printed.

Usage (run from the project folder):
    Windows: .\\.venv\\Scripts\\python.exe scripts\\check_oracle_connection.py
    Linux:   .venv/bin/python scripts/check_oracle_connection.py
"""

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from app import config  # noqa: E402
from app.errors import ServiceError  # noqa: E402
from app.services import oracle_db_service  # noqa: E402


def main():
    print(f"ORACLE_ENABLED : {config.ORACLE_ENABLED}")
    print(f"ORACLE_DSN     : {config.ORACLE_DSN or '(not set)'}")
    print(f"ORACLE_USER    : {config.ORACLE_USER or '(not set)'}")
    print(f"ORACLE_PASSWORD: {'(set)' if config.ORACLE_PASSWORD else '(not set)'}")
    print(f"Client mode    : {oracle_db_service.get_mode()}"
          + (f" ({config.ORACLE_CLIENT_LIB_DIR})" if config.ORACLE_CLIENT_LIB_DIR else ""))
    print()

    result = oracle_db_service.test_connection()
    if not result["connected"]:
        print(f"FAILED: {result['error']}")
        print("Details are in the application log (logs/app.log).")
        return 1

    print(f"Connected. Database server version: {result['server_version']}")
    try:
        rows = oracle_db_service.fetch_all(
            "SELECT SYS_CONTEXT('USERENV', 'DB_NAME') AS db_name, "
            "SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') AS current_schema FROM dual"
        )
        print(f"Database name : {rows[0]['db_name']}")
        print(f"Current schema: {rows[0]['current_schema']}")
    except ServiceError as error:
        print(f"Connected, but the information query failed: {error.message}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
