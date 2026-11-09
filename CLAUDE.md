# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Document Automation": a FastAPI app that uploads one document (PDF/PNG/JPG/BMP/TIFF), renders PDF pages to PNG at a chosen DPI, OCRs each page with PaddleOCR 3.x, extracts invoice fields/line items with rule-based logic, and lets the user correct them on a review screen, one invoice at a time (a document can hold many invoices). The frontend is plain HTML/CSS/JS under Jinja2 templates: `static/css/base.css` + `static/js/base.js` are shared, and each page has its own `<page>.css` and `<page>.js`. It has no framework, no build step, and loads nothing from the internet, because the server can be offline. Colours are CSS custom properties on `:root`; dark mode uses `data-theme` on `<html>` (set before paint, toggled by the theme button).

Development happens on Windows 11 and production runs on **Oracle Linux 7.9 (glibc 2.17)**. Both use **Python 3.11.9 exactly**. The OL7 target drives most of the non-obvious constraints below. README.md is the long-form reference (deployment, API table, troubleshooting).

## Commands (PowerShell, from the project root)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements-dev.txt -c constraints.txt
.\.venv\Scripts\python.exe -m pip check
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe scripts\prepare_ocr_models.py      # once; also after changing OCR model names

.\.venv\Scripts\python.exe main.py                            # http://127.0.0.1:8000, /docs, /api/health

.\.venv\Scripts\python.exe -m pytest -q                       # full suite
.\.venv\Scripts\python.exe -m pytest tests/test_review.py -q  # one file
.\.venv\Scripts\python.exe -m pytest tests/test_ocr.py::test_name -q
.\.venv\Scripts\python.exe -m pytest -k "dpi" -q
```

There is no linter or formatter configured. Real-OCR tests skip automatically when `models/` is not prepared (`tests/test_ocr.py` checks `get_engine_info()["models_ready"]`).

## Dependency rules (OL7 / glibc 2.17)

- Always install with `--only-binary=:all: ... -c constraints.txt`. `constraints.txt` pins every package, transitive ones included, to versions that have glibc 2.17 wheels. Windows uses the same pins so that development exercises production code paths.
- Do not bump `pymupdf` (1.26.0), `pillow` (12.2.0), `onnxruntime` (1.16.3) or `numpy` (1.26.4, needed by ORT 1.16.3): newer Linux wheels need glibc 2.28. Before changing any pin, check the downloaded Linux wheels with `scripts/check_linux_wheels.py <wheel-folder>`.
- PaddlePaddle cannot run on OL7. The default and production engine is `OCR_ENGINE=onnxruntime` with PP-OCRv6 ONNX models. `OCR_ENGINE=paddle` (from `requirements-paddle-windows.txt`) is for Windows-only comparison and needs `OCR_ENABLE_MKLDNN=false`.
- `scripts/prepare_ocr_models.py` downloads the ONNX models and lowers their IR version label from 10 to 9 so ORT 1.16.3 will load them. It checks the opset first. An "Unsupported model IR version" error means this step was skipped.

## Architecture

- `main.py` only wires things together: it mounts static files, registers error handlers, includes one router per `app/api/*_api.py` module, and runs the startup lifespan (create storage dirs, clean temp, `recover_interrupted_jobs()`).
- **Layering:** `app/api/<feature>_api.py` is a thin HTTP layer that calls `app/services/<feature>_service.py`, where all the logic lives. Each functionality has one api module and one service module with matching names.
- **Errors:** services raise `app.errors.ServiceError(message, status_code, code, details)`, the single application error type. `api/error_handlers.py` turns it into JSON. Unexpected exceptions are logged and returned as a generic message, never with a traceback.
- **Response envelope:** every endpoint returns `api_responses.success(data, message)` → `{"success": true, "message", "data"}`. Errors return `{"success": false, "error": {"code", "message", "details"}}`. The frontend JS depends on this shape.
- **Config:** `app/config.py` reads env vars plus `.env` (which never overrides real env vars) into module-level constants. Relative paths resolve against the project dir. The DPI limits (min 50, max 300, default 75) are fixed constants by requirement and are not configurable through env. Tests monkeypatch `config.X` directly.
- **Single process by design:** run one Uvicorn worker only. OCR runs in one daemon thread (`ocr_processing_service`) that takes documents from a `queue.Queue`, so the model loads once. `document.json` writes go through an `RLock` in `document_metadata_service` (`update_metadata(id, fn)` is the locked read-modify-write). Multiple workers would break both of these.
- **Storage is the database.** Each document lives in `storage/documents/<32-hex id>/`: `document.json` (metadata and OCR status), `original/`, `pages/page_0001.png`, `ocr/page_0001.json`, `invoice_data.json`. JSON is written atomically through `storage_service.write_json_atomic`. Document IDs are validated on every access (`storage_service.get_document_dir`) to block path traversal. `storage_service.remove_path` refuses paths outside storage.
- **Re-conversion:** converting at the same DPI reuses the existing images. A new DPI regenerates the images and removes the old OCR results. OCR only processes pages without a successful result, and an interrupted job resumes without redoing finished pages. After OCR, a **detail pass** re-renders only incomplete PDF pages at `OCR_DETAIL_DPI` (150) and reads them again; the OCR layer stays generic and asks a check function that `main.py` wires to `invoice_extraction_service.needs_more_detail`. Such pages keep their own `dpi` in the page record.
- **Invoice data** (`invoice_data_service`): a document is split into invoices (groups of consecutive pages) by `invoice_extraction_service.split_into_invoices` (page marker → invoice number → own date/total); `invoice_data.json` (version 2) stores one record per invoice keyed by its first page, plus the user's `page_breaks`. Every endpoint takes a `page` and works on the invoice containing it. Draft/saved invoices keep their pages when OCR changes. Extraction runs lazily on first GET after OCR, using `invoice_extraction_service`, which matches labels and takes the value from the same line, the line to the right, or the line below, and reads line items from the table under a header. `ocr_signature` detects new OCR results. Re-OCR refreshes `extracted` but replaces `values` only if the user hasn't saved, so user corrections are never overwritten except by an explicit `/invoice-data/extract`. The status `saved` requires `REQUIRED_KEYS`. `draft` does not.
- **Oracle DB** (`oracle_db_service`): optional (`ORACLE_ENABLED=false` by default). Thin mode is the default; `ORACLE_CLIENT_LIB_DIR` switches to Thick mode. The only writes go through `run_in_transaction(work)` (all or nothing).
- **Delete** (`document_delete_service`, `DELETE /api/documents/{id}`): removes the document folder; refused while OCR is queued/running. `main.py` registers a guard (`invoice_data_service.delete_warning`) so a document with posted invoices needs `?confirm=true`; the database rows are never touched.
- **Posting** (`invoice_posting_service`): status flow `needs_review → draft → saved → posted`. "Post to database" sends one **saved** invoice to the staging tables in `deploy/database/invoice_staging_schema.sql` (header, lines, field audit, status history) in one transaction; the DBA creates those tables, the app only INSERTs. A posted invoice is read-only (save, re-read and page breaks return 409 `INVOICE_POSTED`). A failed post keeps the invoice `saved` with `posting.last_error`. Moving rows on to Oracle EBS R12 (AP open interface) happens on the database side, not in the app.

## Project rules enforced by `tests/test_project_rules.py`

- **Invoice wording is allowed only in specific files.** Generic modules (upload, PDF, OCR, storage, Oracle, and their tests) must not contain the word "invoice" in their content or filename. The allow-list is `INVOICE_WORDING_ALLOWED` in that test. If a new invoice-specific file needs the word, add it to the allow-list deliberately; otherwise keep the generic layers generic.
- No `CREATE/ALTER/DROP/TRUNCATE/RENAME TABLE|INDEX|VIEW|SEQUENCE|SYNONYM` anywhere in the source. The app must never change the database schema.
- No hardcoded Windows drive paths (`"C:\\..."`) in `.py` files. Code must run on both Windows and Linux.
- Oracle errors and logs must never include the password. `.env.example` must keep `ORACLE_PASSWORD=` empty and `ORACLE_ENABLED=false`.

## Testing notes

`tests/conftest.py` points `STORAGE_DIR`/`LOG_DIR` at a temp dir and forces `ORACLE_ENABLED=false` **before** importing `main`. Any test that needs the app must go through the session-scoped `client` fixture. Test PDFs and images are generated in memory (`make_pdf`, `make_image`, and `tests/sample_invoice.py`). `run_ocr(client, id)` calls `ocr_processing_service.wait_until_idle()` to synchronize with the OCR worker thread.
