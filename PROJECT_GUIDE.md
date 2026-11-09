# Project Guide

A plain-English map of the Document Automation project, so you can find and change things yourself.
For deployment details and the full API table see `README.md`. For the rules that tests enforce see `CLAUDE.md`.

## 1. What the application does

1. You upload **one document** (PDF, PNG, JPG, BMP or TIFF).
2. A PDF is converted into one PNG image per page. Images become one PNG per image (or per TIFF frame).
3. OCR (PaddleOCR) reads the text on every page.
4. Rule-based code finds the invoice fields and line items in the OCR text.
5. On the **Review** screen you check and correct the values, one invoice at a time. A document can contain many invoices.

There is no database for the documents. Everything is stored as files in `storage/`.
Oracle is optional. It is used for connection checks and for "Post to database" (saved invoices go to staging tables).

## 2. Folder structure

```
d:\PaddleOCR
|-- main.py                  Starts the application and connects all the parts
|-- .env                     Your local settings (never committed; copy from .env.example)
|-- .env.example             List of every setting, with safe defaults
|-- requirements.txt         Packages the application needs
|-- requirements-dev.txt     requirements.txt + test packages
|-- requirements-paddle-windows.txt   Optional: PaddlePaddle engine, Windows only
|-- constraints.txt          Exact version of every package (needed for Oracle Linux 7.9)
|-- pytest.ini               Test settings
|-- app/
|   |-- config.py            All settings, read from .env
|   |-- errors.py            The one error type (ServiceError)
|   |-- logging_setup.py     Log to console and logs/app.log
|   |-- api/                 HTTP layer: receives requests, calls a service, returns JSON or HTML
|   |-- services/            The real work (all business logic)
|   |-- templates/           HTML pages (Jinja2)
|   `-- static/
|       |-- css/             Styles
|       `-- js/              Browser code
|-- scripts/                 Helper programs you run by hand
|-- deploy/oracle-linux-7.9/ Files for the production server
|-- tests/                   Automatic tests
|-- storage/                 Created at run time: uploaded documents and their results (do not delete)
|-- logs/                    Created at run time: app.log
`-- models/                  OCR model files (created by scripts/prepare_ocr_models.py)
```

Folders you can ignore: `.venv` (Python packages), `__pycache__` and `.pytest_cache` (automatic caches), `.vscode` (editor settings).

## 3. How the layers work together

```
Browser  ->  app/api/xxx_api.py  ->  app/services/xxx_service.py  ->  files in storage/
             (thin: URL + input)     (all logic lives here)
```

- An **API module** only defines URLs and calls one service function. Keep it short.
- A **service module** contains the logic. If something goes wrong it raises `ServiceError("message", status_code, "CODE")`.
- `app/api/error_handlers.py` turns every error into the standard JSON error. Every successful endpoint returns `{"success": true, "message": ..., "data": ...}` through `api_responses.success(...)`. The JavaScript depends on this shape, so do not change it.

## 4. Python files

### Top level
| File | Purpose |
|---|---|
| `main.py` | Creates the FastAPI app, serves `/static`, registers error handlers, includes every API router, and on startup creates the storage folders and resumes interrupted OCR jobs. |
| `app/config.py` | Reads `.env` / environment variables into constants (paths, DPI limits, OCR model names, Oracle settings). Change settings here or in `.env`. |
| `app/errors.py` | `ServiceError`, the single application error. |
| `app/logging_setup.py` | Sets up logging. |

### API modules (`app/api/`)
| File | URLs | Purpose |
|---|---|---|
| `frontend_api.py` | `/`, `/review`, `/review/{id}` | Returns the HTML pages. |
| `health_api.py` | `/api/health` | Reports app version, storage folders, OCR engine and model status, and Oracle settings (it does not load OCR or connect to Oracle). |
| `document_validation_api.py` | `/api/documents/validation-rules` | Tells the upload page the allowed types and DPI limits. |
| `document_upload_api.py` | `POST /api/documents/upload` | Receives the uploaded file. |
| `pdf_processing_api.py` | `POST /api/documents/{id}/convert` | Converts (or re-converts) a PDF at a chosen DPI. |
| `page_image_api.py` | `/api/documents/{id}/pages`, `.../pages/{n}/image` | Lists pages and sends one page image. |
| `ocr_processing_api.py` | `POST .../ocr`, `POST .../ocr/detail`, `GET .../ocr/status` | Starts OCR, starts the detail pass, reports progress. |
| `ocr_results_api.py` | `.../ocr/results`, `.../pages/{n}/ocr` | Returns stored OCR text. |
| `document_review_api.py` | `/api/review/documents...` | Document list and page data for the review screen. |
| `invoice_data_api.py` | `.../invoice-data` (+ `/extract`, `/page-break`, `/parse-value`) | Read, save, re-read and split the invoice data. |
| `invoice_posting_api.py` | `.../invoice-data/post` | Post a saved invoice to the database. |
| `document_delete_api.py` | `DELETE /api/documents/{id}` | Delete a whole document from storage (button in the Review Queue). |
| `api_responses.py` | - | Builds the standard JSON responses. |
| `error_handlers.py` | - | Turns errors into the standard JSON error. |

### Services (`app/services/`)
| File | Purpose |
|---|---|
| `storage_service.py` | Where files live. Builds every path, checks document IDs (blocks `../` tricks), writes JSON safely, deletes only inside `storage/`. |
| `document_metadata_service.py` | Reads and writes `document.json` (status of conversion and OCR). All writes go through one lock. |
| `document_validation_service.py` | Checks file type, file content, PDF/image validity, DPI and page numbers. |
| `document_upload_service.py` | The upload workflow (see section 7). |
| `pdf_processing_service.py` | Renders PDF pages to PNG with PyMuPDF. |
| `page_image_service.py` | Creates pages from image uploads; looks up page images. |
| `ocr_processing_service.py` | Runs OCR in one background thread, page by page; handles the detail pass and recovery after a restart. |
| `ocr_results_service.py` | Converts raw PaddleOCR output into the JSON saved per page. |
| `document_review_service.py` | Combines document, page and OCR data for the review screen (read only). |
| `invoice_extraction_service.py` | The largest file. Finds fields and line items in OCR text using plain rules. It is split into labelled sections (dates, amounts, field definitions, line item table, totals, splitting into invoices). |
| `invoice_data_service.py` | Stores the invoice data, applies user corrections and keeps them safe when OCR runs again. |
| `oracle_db_service.py` | Optional Oracle connection, read-only checks, and `run_in_transaction()` for writes (all or nothing). Never creates or changes database objects. |
| `document_delete_service.py` | Deletes a document folder. Blocked while OCR runs; a guard (wired in `main.py`) asks for confirmation when invoices were posted. |
| `invoice_posting_service.py` | "Post to database": turns a saved invoice into rows for the staging tables (`deploy/database/invoice_staging_schema.sql`) and inserts them in one transaction. |

### Scripts (`scripts/`)
| File | Purpose |
|---|---|
| `prepare_ocr_models.py` | Downloads the OCR models into `models/` (run once, and after changing model names). |
| `check_oracle_connection.py` | Tests the Oracle settings in `.env` with read-only queries. |
| `check_linux_wheels.py` | Checks that downloaded Linux packages work on Oracle Linux 7.9 (only needed when changing package versions). |

## 5. Web pages (HTML, CSS, JavaScript)

Each page has its own CSS and JavaScript file. `base.*` is shared by all pages.

| Page | Template | CSS | JavaScript |
|---|---|---|---|
| Shared layout (header, theme button, footer) | `templates/base.html` | `static/css/base.css` | `static/js/base.js` |
| Upload | `templates/upload.html` | `static/css/upload.css` | `static/js/upload.js` |
| Review | `templates/review.html` | `static/css/review.css` | `static/js/review.js` |

- `base.js` provides shared helpers as `window.App` (API calls, toasts, formatting, theme).
- Colours are CSS variables at the top of `base.css` (`:root`). Dark mode uses `data-theme` on `<html>`.
- Nothing is loaded from the internet, so the application works offline.

## 6. Where is the data stored?

```
storage/documents/<32-character id>/
    document.json         status of upload, conversion and OCR
    original/             the uploaded file (original.pdf, original.png, ...)
    pages/page_0001.png   one image per page
    ocr/page_0001.json    OCR text of each page
    invoice_data.json     invoices found in the document and your corrections
storage/temp/             files that are still being uploaded
```

Never edit these files while the application is running. Never delete `storage/` if you want to keep your documents.

## 7. Workflows

**Run:** `main.py` starts Uvicorn with one worker. At startup it creates the storage folders, removes old temp files and marks OCR jobs that were cut off by a restart as "interrupted".

**Upload** (`document_upload_service.upload_document`)
1. Check the file extension and the DPI value.
2. Stream the file into `storage/temp/`.
3. Check the real content (the first bytes must match the extension, and the file must open).
4. Create the document folder and move the file to `original/`.
5. Create the page images. If anything fails here, the new folder is removed again.

**PDF to image** (`pdf_processing_service.convert_pdf_to_images`)
- Pages are rendered one at a time with PyMuPDF at the chosen DPI (zoom = DPI / 72).
- Converting again at the same DPI reuses the existing images. A different DPI renders new images into a staging folder and swaps them in only when every page succeeded; the old OCR results are removed at the same time.
- DPI limits: minimum 50, maximum 300, default 75. They are constants in `app/config.py` (`PDF_DPI_MIN`, `PDF_DPI_MAX`, `PDF_DPI_DEFAULT`).

**OCR** (`ocr_processing_service`)
1. The API marks the document as `queued` and puts it in a queue.
2. One background thread takes documents from the queue and loads the OCR model once.
3. Each page is read on its own and saved to `ocr/page_XXXX.json`. A failed page does not stop the others.
4. Afterwards a **detail pass** re-renders only incomplete PDF pages at `OCR_DETAIL_DPI` (150) and reads them again.
5. The browser polls `/ocr/status` to show progress.

**Invoice data** (`invoice_data_service`)
- The first time the review screen asks for data after OCR, `invoice_extraction_service` finds the fields and splits the document into invoices.
- Your saved corrections are never replaced by a new OCR run. Only the explicit "re-read" action replaces them.

**Oracle:** disabled by default (`ORACLE_ENABLED=false`). When enabled, the review screen shows "Post to database" for saved invoices. Upload, conversion, OCR and review never need the database.

## 8. How to change things

| I want to... | Go to |
|---|---|
| Change a setting (port, folders, OCR models, detail DPI) | `.env` (list in `.env.example`), or `app/config.py` for a new setting |
| Change DPI limits | `PDF_DPI_*` in `app/config.py` (and update the tests in `tests/test_dpi_validation.py`) |
| Change allowed file types | `ALLOWED_EXTENSIONS` in `app/config.py` and `detect_file_type` in `document_validation_service.py` |
| Change how a field is found | `invoice_extraction_service.py`: field labels are in the "Field definitions" section |
| Change the rules for saving an invoice | `invoice_data_service.py` (`REQUIRED_KEYS`, `clean_values`) |
| Change colours or fonts | variables at the top of `static/css/base.css` |
| Change the look of one page | that page's CSS file |
| Change what a button does | that page's JS file |
| Change page text or add a form field | that page's template, then its JS and the matching service |

**Add a new API:** create `app/api/<name>_api.py` with an `APIRouter`, then add it to the imports and to the `for api_module in (...)` list in `main.py`. Return `success(...)` from every endpoint.

**Add a new service:** create `app/services/<name>_service.py` with the same base name as its API module. Raise `ServiceError` for problems. Use `storage_service` for every path.

**Rules to remember** (checked by `tests/test_project_rules.py`):
- The word "invoice" may appear only in the invoice files listed in `INVOICE_WORDING_ALLOWED` in that test. Upload, PDF, OCR, storage and Oracle code stay generic ("document").
- Never write SQL that creates, alters or drops tables, indexes, views or sequences.
- No hard-coded Windows paths like `C:\...`.
- Never log or return the Oracle password.
- Run one Uvicorn worker only.

## 9. Install, start and test

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements-dev.txt -c constraints.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe scripts\prepare_ocr_models.py     # once

.\.venv\Scripts\python.exe main.py                           # open http://127.0.0.1:8000
.\.venv\Scripts\python.exe -m pytest -q                      # run all tests
```

- Python must be 3.11.9. Do not upgrade `pymupdf`, `pillow`, `onnxruntime` or `numpy` (newer Linux versions do not run on Oracle Linux 7.9).
- `http://127.0.0.1:8000/docs` lists every API; `http://127.0.0.1:8000/api/health` shows whether everything is ready.
- Logs are in `logs/app.log`.
