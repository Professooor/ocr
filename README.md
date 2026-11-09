# Document Automation

A web application that reads invoices:

1. **Upload** one document (PDF, PNG, JPG/JPEG, BMP, TIF/TIFF).
2. **Convert** every PDF page to a PNG image at a selectable DPI (**50–300, default 75**).
3. **OCR** every page with **PaddleOCR 3.x**, stored page by page with text, confidence and coordinates.
4. **Review** the invoice: the page on the left, the data read from it on the right (supplier and
   buyer with NTN, STRN and address; invoice, FBR invoice, PO, DC, gate pass and contract numbers;
   dates; value excluding tax, discount, sales tax % and amount, further tax, total, total quantity,
   amount in words; line items). Correct anything OCR got wrong,
   and save it. A scanned batch with many invoices is split into invoices automatically.

| Component | Version |
|---|---|
| Development OS | Windows 11 |
| Production OS | Oracle Linux 7.9 (x86_64) |
| Python | **3.11.9** on both |
| Backend | FastAPI 0.162.2 + Uvicorn 0.54.0 |
| Frontend | HTML, CSS, JavaScript (Jinja2 templates) |
| PDF processing | PyMuPDF 1.26.0 |
| OCR | PaddleOCR 3.7.0 (PaddleX 3.7.2) with PP-OCRv6 models on ONNX Runtime 1.16.3 |
| Database | Oracle Database 19c through python-oracledb 26.1.0 (optional) |

---

## Contents

- [Compatibility status](#compatibility-status)
- [Project structure](#project-structure)
- [Part 1 — Windows 11 development](#part-1--windows-11-development)
- [Part 2 — Oracle Linux 7.9 production deployment](#part-2--oracle-linux-79-production-deployment)
- [API reference](#api-reference)
- [Known limitations](#known-limitations)

---

## Compatibility status

This section separates what has been **tested** from what is supported only by **evidence**.

### Verified by running it (Windows 11 Pro 22621, Python 3.11.9)

- Clean install from `requirements-dev.txt` + `constraints.txt` (`pip check`: no broken requirements).
- Full automated test suite: **269 tests passed** (upload, validation, DPI 50/75/150/200/250/300,
  rejection below 50 and above 300, multi-page order, image retrieval, re-conversion, real PaddleOCR,
  invoice field extraction including real OCR of generated sample invoices, saving and validation of
  corrected data, Document Review, error handling, frontend assets, security rules,
  a 60 MB upload and a 400-page PDF).
- Real browser test (headless Microsoft Edge 157): drag-and-drop upload → conversion at 150 DPI →
  OCR → invoice review: autofill, field notes, click-the-invoice-to-fill, required-field errors,
  editing amounts and line items, save, reload, resizable
  divider, light and dark mode and a phone-sized screen; 44 of 44 checks passed, no JavaScript errors.
  A 120 MB PDF was uploaded successfully.
- Invoice field extraction was tested only on generated sample invoices and unit-test layouts, **not
  on your real invoices**. Expect to correct some fields on layouts it has not seen.
- PaddleOCR 3.7.0 runs with **PaddlePaddle not installed** (ONNX Runtime engine). This is the
  exact package set used on Oracle Linux 7.9.
- Optional `OCR_ENGINE=paddle` with PaddlePaddle 3.3.1 (Windows only) also passed the real OCR test.

### Supported by evidence, not yet run on an Oracle Linux 7.9 server

Oracle Linux 7.9 ships **glibc 2.17** and **libstdc++ from GCC 4.8.5 (GLIBCXX_3.4.19)**.
Every Linux wheel in `constraints.txt` was downloaded for that platform and the ELF
"version needs" of every native library inside it were read with `scripts/check_linux_wheels.py`:

| Result | Detail |
|---|---|
| All 93 packages | Linux x86_64 wheels exist for cp311 with tags ≤ manylinux2014 (glibc 2.17) |
| Native libraries | None needs more than GLIBC_2.17, GLIBCXX_3.4.19, CXXABI_1.3.7 (0 failures) |
| Linux-only dependencies | None (all platform markers in the tree only add Windows packages) |

### Not compatible: PaddlePaddle on Oracle Linux 7.9

Every PaddlePaddle 3.x Linux wheel on PyPI (**3.0.0, 3.1.1, 3.2.2, 3.3.0, 3.3.1** were checked)
is tagged `manylinux1`, but its `libpaddle.so` actually needs:

| Needed by PaddlePaddle 3.3.1 | Provided by Oracle Linux 7.9 |
|---|---|
| GLIBC_2.23, 2.27, 2.28 (`lgamma`, `expf/logf/powf/log2f`, `fcntl64`) | GLIBC_2.17 |
| GLIBCXX_3.4.20 – 3.4.22, CXXABI_1.3.8 – 1.3.11 | GLIBCXX_3.4.19, CXXABI_1.3.7 |

pip would install it without an error and `import paddle` would then fail. glibc cannot be
upgraded on Oracle Linux 7.9.

**Solution used (keeps Oracle Linux 7.9 and Python 3.11.9):** PaddleOCR 3.7.0 has an official
`engine="onnxruntime"` option and official ONNX versions of the PP-OCRv5/PP-OCRv6 models.
ONNX Runtime **1.16.3** is the newest release with glibc 2.17 wheels. The PP-OCRv6 detection
models are labelled ONNX IR version 10, while ONNX Runtime 1.16.3 accepts up to IR 9; the models
use only opset 14 / 11 operators, so `scripts/prepare_ocr_models.py` lowers the label (it checks
the opset and refuses to patch otherwise). Recognition results were identical to the PaddlePaddle
engine in testing.

Other options, if PaddlePaddle itself is required on the server (none tested):
build PaddlePaddle 3.x from source on Oracle Linux 7.9 with a newer GCC (devtoolset), or run
Python with a separately installed newer glibc. Both are complex and not recommended.

### Version pins caused by Oracle Linux 7.9

| Package | Pinned | Reason |
|---|---|---|
| pymupdf | 1.26.0 | 1.26.1+ Linux wheels need glibc 2.28 |
| pillow | 12.2.0 | 12.3+ Linux wheels need glibc 2.28 |
| onnxruntime | 1.16.3 | 1.17+ Linux wheels need glibc 2.28 |
| numpy | 1.26.4 | onnxruntime 1.16.3 is built against NumPy 1.x |

Windows uses the same versions, so development tests the production code paths.

---

## Project structure

```
PaddleOCR/
├── main.py                         FastAPI app: settings, routers, static files, startup
├── requirements.txt                runtime dependencies (both platforms)
├── requirements-dev.txt            + pytest, httpx
├── requirements-paddle-windows.txt OPTIONAL: PaddlePaddle engine, Windows only
├── constraints.txt                 exact versions of all 93 packages (both platforms)
├── .env.example                    configuration template (copy to .env)
├── app/
│   ├── config.py                   reads .env; DPI limits 50/300/75 are constants here
│   ├── errors.py                   ServiceError (the single application error type)
│   ├── logging_setup.py            console + rotating logs/app.log
│   ├── api/                        HTTP layer - one module per functionality
│   │   ├── frontend_api.py         HTML pages: /  and  /review
│   │   ├── document_upload_api.py
│   │   ├── document_validation_api.py
│   │   ├── pdf_processing_api.py
│   │   ├── page_image_api.py
│   │   ├── ocr_processing_api.py
│   │   ├── ocr_results_api.py
│   │   ├── document_review_api.py
│   │   ├── invoice_data_api.py      invoice fields: read, correct, save
│   │   ├── health_api.py
│   │   ├── api_responses.py        standard JSON response format
│   │   └── error_handlers.py       errors -> JSON (no tracebacks to the browser)
│   ├── services/                   processing logic - one module per functionality
│   │   ├── document_upload_service.py
│   │   ├── document_validation_service.py
│   │   ├── pdf_processing_service.py
│   │   ├── page_image_service.py
│   │   ├── ocr_processing_service.py
│   │   ├── ocr_results_service.py
│   │   ├── document_review_service.py
│   │   ├── invoice_extraction_service.py  finds fields and line items in the OCR text
│   │   ├── invoice_data_service.py     stores, validates and saves the reviewed data
│   │   ├── oracle_db_service.py
│   │   ├── document_metadata_service.py
│   │   └── storage_service.py
│   ├── templates/                  base.html, upload.html, review.html
│   └── static/
│       ├── css/                    base.css, upload.css, review.css
│       └── js/                     base.js, upload.js, review.js
├── scripts/
│   ├── prepare_ocr_models.py       download + prepare ONNX models
│   ├── check_linux_wheels.py       glibc/libstdc++ check of Linux wheels
│   └── check_oracle_connection.py  read-only database connectivity check
├── deploy/oracle-linux-7.9/
│   ├── install_python_3.11.9.sh    builds Python 3.11.9 from source
│   ├── document-automation.service systemd unit
│   ├── nginx-document-automation.conf (optional)
│   └── verify_installation.sh
├── tests/                          pytest suite
├── models/        (created)        OCR models
├── storage/       (created)        documents, page images, OCR results, temp files
└── logs/          (created)        app.log
```

### Storage layout

```
storage/documents/<document_id>/
    document.json          metadata: file, DPI, pages, OCR status
    original/original.pdf  the uploaded file (the user's filename is metadata only)
    pages/page_0001.png    one image per page, original page order
    ocr/page_0001.json     one OCR result per page
storage/temp/              upload staging (cleaned at startup after 24 h)
```

Document IDs are 32 random hexadecimal characters. They are validated on every request,
which also prevents path traversal.

### Process model

The application runs as **one process**. Uploads and page conversion run in web-server worker
threads. OCR runs in **one dedicated background thread** that takes documents from a queue one at
a time, so the OCR model is loaded once and memory stays predictable. Do not start several Uvicorn
workers. If the process restarts during OCR, the job is marked `interrupted` at startup and can
be resumed with **Run OCR** in Document Review. Pages that already finished are not repeated.

---

## Part 1 — Windows 11 development

All commands are **PowerShell**. The examples use `D:\PaddleOCR`; on a workstation where the
project lives in `E:\PaddleOCR`, use that path instead.

### 1.1 Requirements

- Windows 11 x64
- Python **3.11.9** exactly
- About 2 GB of free disk space (packages and models)
- Internet access for the first installation (packages and OCR models)

### 1.2 Install and verify Python 3.11.9

```powershell
# Install Python 3.11.9 for the current user (does not change other Python versions)
winget install --id Python.Python.3.11 --version 3.11.9 --scope user --exact

# Verify (open a new PowerShell window first)
py -3.11 --version          # must print: Python 3.11.9
py -0p                      # lists all installed Python versions
```

### 1.3 Create the virtual environment

```powershell
Set-Location D:\PaddleOCR
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe --version     # must print: Python 3.11.9
```

If an existing `.venv` reports another version, rename it (for example
`Rename-Item .venv .venv-old`) and create it again. Do not mix Python versions.

### 1.4 Activate the virtual environment (optional)

```powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks the script: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
The commands below call `.\.venv\Scripts\python.exe` directly, so activation is not required.

### 1.5 Install dependencies

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements-dev.txt -c constraints.txt
.\.venv\Scripts\python.exe -m pip check
```

Use `requirements.txt` instead of `requirements-dev.txt` if you do not need the tests.
Always pass `-c constraints.txt`, which keeps Windows on the same versions as Oracle Linux 7.9.

### 1.6 Configure `.env`

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Never commit `.env`. Every variable is explained in `.env.example`.

### 1.7 Prepare the OCR models (once)

```powershell
.\.venv\Scripts\python.exe scripts\prepare_ocr_models.py
```

This downloads the configured PP-OCRv6 ONNX models from Hugging Face into `models\`
(about 30 MB for the small models) and makes them loadable by ONNX Runtime 1.16.3.
Messages printed by the Hugging Face downloader as "errors" in PowerShell are progress bars.

### 1.8 Oracle Database 19c (optional)

Document upload, conversion, OCR and review work **without** a database. To enable it, set these in `.env`:

```ini
ORACLE_ENABLED=true
ORACLE_USER=your_user
ORACLE_PASSWORD=your_password
ORACLE_DSN=dbhost.example.com:1521/ORCLPDB1
# Thin mode (default) needs no Oracle Client. For Thick mode:
ORACLE_CLIENT_LIB_DIR=C:\Oracle\instantclient_19
```

Thick mode is needed only for database features that Thin mode does not support, for example
Oracle Native Network Encryption. Check the connection (read-only, the password is never printed):

```powershell
.\.venv\Scripts\python.exe scripts\check_oracle_connection.py
```

The application never creates, alters, drops, truncates or renames database objects.
Writing document data to the existing staging tables needs their names and columns first
(see [Known limitations](#known-limitations)).

### 1.9 Start the application

```powershell
.\.venv\Scripts\python.exe main.py
```

Open **http://127.0.0.1:8000** in a browser. Stop with `Ctrl+C`.
Health check: http://127.0.0.1:8000/api/health

### 1.10 Using the application

1. **Upload** page: drag a document onto the drop zone (or click to browse), set the
   **PDF conversion DPI** with the slider or the number field (whole number 50–300, default 75;
   the A4 pixel size is shown live), then click **Process Document**. The timeline shows the
   upload percentage, page conversion, and OCR progress; page thumbnails get a check mark as each
   page is read. There is no file size or page count limit.
2. **Document Review** page: search the document list (each row shows its review status) and
   open an invoice. The screen is split in two; drag the divider to resize.
   - **Left:** page thumbnails and the page image. The recognised text boxes are off by default;
     switch them on with **Text boxes** (they also appear while a field is selected, so text can be
     clicked). Zoom with the toolbar or the `+` / `-` / `0` keys; change page with the Left/Right arrow keys.
   - **Small print:** after OCR, every PDF page whose invoice number, date or amounts could not be read
     (typically small print at 75 DPI) is rendered again at `OCR_DETAIL_DPI` (150) and read again. Only
     those pages are affected, so the batch stays fast; the sharper reading is kept only when it reads
     at least as much text.
   - **Several invoices in one document:** the form always shows the invoice of the open page.
     Where each invoice starts is found from the OCR text: a "Page 1" / "Page 2 of 3" marker decides
     first, then the invoice number (same number = same invoice), then whether the page has its own
     date or total; a page with none of these (terms, attachments) belongs to the invoice before it.
     A "+" on a thumbnail marks a page that continues the previous invoice. If the split is wrong,
     use **New invoice from this page** or **Join with previous invoice**. When you open a page of
     another invoice, unsaved changes are saved as a draft automatically. The document list shows
     how many invoices are saved (for example *12 / 161 saved*).
   - **Right, "Invoice details":** the form is filled automatically from the OCR text. Under each
     field a note says where the value came from: *Autofilled from invoice*, *please verify*
     (low OCR confidence or an unclear date), *Not found on the invoice*, *Entered manually* or
     *Edited* (with what OCR had read). Vendor name, invoice number, invoice date, due date and total
     due are required. The form is grouped into Supplier, Buyer, Invoice and Amounts. Line items
     are an editable table (description, HS code, UOM, quantity, rate, value excluding tax, tax %,
     sales tax, further tax, discount, value including tax): add or delete lines, and show or hide
     the optional columns with **Edit Columns** (further tax and discount are hidden at first).
   - **Fixing a wrong value:** type the correct value, or select the field and **click the right
     text on the invoice**; the value is read from the clicked text and filled in. While a field is
     selected, the line it was read from is outlined in green on the page. Esc cancels.
   - **Save as draft** keeps whatever is entered. **Save changes** also checks the required fields.
     Corrections are never overwritten when OCR runs again; **Re-read invoice** replaces them with a
     fresh reading of the invoice on request.
   - **Right, "OCR text":** every recognised line with its confidence; hover a line to see its box on
     the page. Lines below 80 % confidence are shown in amber.

How the fields are found: labels such as "Invoice Number", "Due Date" or "Total Due" are located in
the OCR text, and the value is taken from the same line, from the line to the right, or from the
line below. Supplier and buyer details (NTN, STRN, address) are told apart by the nearest section
heading above them ("Buyer", "Purchaser's Details", "Customer"...) or the buyer's name; details with no
heading above them belong to the supplier. Line items are read from the table under a header such as
"Description / Qty / Rate / Amount"; on each row the HS code, unit and tax % are picked up, and sales
tax, further tax and the value including tax are found from value + tax (+ further tax) = value
including tax. This is rule based, not perfect: unusual layouts will need corrections, which is what the
review screen is for. A date like 03/04/2025 is read day/month by default; set
`INVOICE_DATE_ORDER=mdy` in `.env` for month/day (an unambiguous date elsewhere on the invoice,
such as 25/12/2025, overrides this).

The interface uses only plain HTML, CSS and JavaScript (no frameworks, no internet resources), so it
also works on a server without internet access. It follows the operating system's light or dark mode, and the
button in the header switches between system, light and dark.

DPI applies to PDF files only. Images are processed at their original resolution.
Higher DPI gives sharper images, larger files and longer processing. An A4 page at 300 DPI is
2480 × 3509 pixels. Pages larger than `MAX_PAGE_PIXELS` at the chosen DPI are rejected with the
highest DPI that would work.

### 1.11 OCR settings

| Setting | Default | Notes |
|---|---|---|
| `OCR_ENGINE` | `onnxruntime` | Same engine as production |
| `OCR_TEXT_DETECTION_MODEL` / `OCR_TEXT_RECOGNITION_MODEL` | `PP-OCRv6_small_det` / `PP-OCRv6_small_rec` | `PP-OCRv6_medium_*` is more accurate on hard scans but slower |
| `OCR_CPU_THREADS` | `4` | |
| `OCR_DETAIL_DPI` | `150` | detail pass: an incomplete PDF page is rendered again at this DPI and read again; `0` = off |

Measured on an Intel Core i5-7300U (2 cores / 4 threads), A4 page at 150 DPI, 17 text lines:
small models **3.3 s** per page, medium models 22.6 s per page, same text recognised.
After changing the model names, run `scripts\prepare_ocr_models.py` again.

**Optional PaddlePaddle engine (Windows only, for comparison):**

```powershell
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements-paddle-windows.txt -c constraints.txt
# then in .env:  OCR_ENGINE=paddle   and keep   OCR_ENABLE_MKLDNN=false
```

With the Paddle engine, PaddleX downloads Paddle-format models into `models\paddlex_cache` on first use.

### 1.12 Run the tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use a temporary storage folder and generated test files; real documents are never touched.
The real-OCR test runs only when the models are prepared.

### 1.13 Common errors (Windows)

| Problem | Solution |
|---|---|
| `python --version` shows 3.13 | Use `py -3.11` or `.\.venv\Scripts\python.exe`; recreate `.venv` with `py -3.11 -m venv .venv` |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or skip activation |
| OCR fails with "OCR model files are missing" | Run `scripts\prepare_ocr_models.py` |
| Model download fails (proxy/firewall) | Copy the `models\` folder from another machine, then run `prepare_ocr_models.py --skip-download` |
| `Unsupported model IR version: 10` | The models were not prepared by `prepare_ocr_models.py`; run it |
| `ConvertPirAttribute2RuntimeAttribute not support` | Paddle engine with oneDNN enabled; set `OCR_ENABLE_MKLDNN=false` |
| Port 8000 already in use | Set `APP_PORT=8001` in `.env` |
| `Could not connect to the Oracle database` | Details are in `logs\app.log`; check `ORACLE_DSN`, network/firewall, user and password |

---

## Part 2 — Oracle Linux 7.9 production deployment

All commands are **bash** on the Oracle Linux 7.9 server, run as root unless noted.
Paths: application `/opt/document-automation`, Python `/opt/python-3.11.9`, service user `docauto`.

> **Status:** these steps were prepared from verified package metadata and the verified
> Python 3.11.9 source, but they have **not yet been executed on an Oracle Linux 7.9 server**.
> Run `deploy/oracle-linux-7.9/verify_installation.sh` after installing and keep its output.
> The open items are listed in [2.17](#217-checks-still-required-on-the-server).

### 2.1 Requirements

- Oracle Linux **7.9** x86_64 (`cat /etc/oracle-release`)
- Access to the Oracle Linux yum repositories `ol7_latest`, `ol7_optional_latest`, `ol7_developer_EPEL`
  (directly or through a local mirror)
- At least 4 GB RAM (8 GB recommended), 3 GB free disk space plus space for documents
- Internet access, or the offline method in 2.5 and 2.7

### 2.2 PaddleOCR compatibility status

See [Compatibility status](#compatibility-status). In short: PaddleOCR 3.7.0 runs with the
ONNX Runtime engine; **PaddlePaddle must not be installed** on Oracle Linux 7.9 and
`OCR_ENGINE` must stay `onnxruntime`.

### 2.3 Install Python 3.11.9

Oracle Linux 7.9 has no Python 3.11 package, so the script builds it from source. It installs
GCC and the development libraries, and OpenSSL 1.1.1 (`openssl11` from Oracle's EPEL mirror)
side by side with the system OpenSSL 1.0.2, because Python 3.11 requires OpenSSL 1.1.1 or newer.
The system Python 2.7 used by `yum` is not changed.

```bash
sudo bash deploy/oracle-linux-7.9/install_python_3.11.9.sh
/opt/python-3.11.9/bin/python3.11 --version        # Python 3.11.9
```

The source checksum (SHA-256 `e7de3240…6153d`) is verified by the script.

### 2.4 Create the service user and copy the application

```bash
useradd --system --home-dir /opt/document-automation --shell /sbin/nologin docauto
mkdir -p /opt/document-automation
# Copy the project WITHOUT .venv, storage, logs and .env, for example from Windows:
#   scp -r main.py app scripts deploy tests requirements*.txt constraints.txt .env.example pytest.ini README.md user@server:/tmp/document-automation/
cp -r /tmp/document-automation/. /opt/document-automation/
chown -R docauto:docauto /opt/document-automation
```

### 2.5 Create the virtual environment and install dependencies

```bash
cd /opt/document-automation
sudo -u docauto /opt/python-3.11.9/bin/python3.11 -m venv .venv
sudo -u docauto .venv/bin/python --version          # Python 3.11.9
sudo -u docauto .venv/bin/python -m pip install --upgrade pip
sudo -u docauto .venv/bin/python -m pip install --only-binary=:all: -r requirements-dev.txt -c constraints.txt
sudo -u docauto .venv/bin/python -m pip check
```

`--only-binary=:all:` is important. pip on glibc 2.17 then accepts only wheels built for
Oracle Linux 7 and never tries to compile packages from source.
`requirements-dev.txt` adds pytest for verification; `requirements.txt` is enough to run the app.

**Offline server:** build a wheelhouse on the Windows machine, check it, and copy it:

```powershell
# Windows PowerShell, in the project folder
$plats = "manylinux_2_17_x86_64","manylinux2014_x86_64","manylinux_2_12_x86_64","manylinux2010_x86_64","manylinux_2_5_x86_64","manylinux1_x86_64" | ForEach-Object { "--platform", $_ }
.\.venv\Scripts\python.exe -m pip download --dest wheelhouse-ol7 --only-binary=:all: @plats --python-version 3.11 --implementation cp --abi cp311 -r requirements-dev.txt -c constraints.txt
.\.venv\Scripts\python.exe scripts\check_linux_wheels.py wheelhouse-ol7      # must report 0 failures
```

```bash
# On the server, after copying wheelhouse-ol7 to /opt/document-automation/
sudo -u docauto .venv/bin/python -m pip install --no-index --find-links wheelhouse-ol7 -r requirements-dev.txt -c constraints.txt
```

### 2.6 Configure `.env`

```bash
cd /opt/document-automation
sudo -u docauto cp .env.example .env
sudo -u docauto vi .env
chmod 600 .env
```

Production values to review: `APP_ENV=production`, `APP_HOST` (`127.0.0.1` behind nginx, otherwise
`0.0.0.0`), `APP_PORT`, `MAX_PAGE_PIXELS` (memory safeguard), `OCR_CPU_THREADS` (number of CPU
cores), Oracle settings.
Keep `OCR_ENGINE=onnxruntime`. Use Linux paths only (relative paths such as `storage` are fine).

### 2.7 Prepare the OCR models

```bash
sudo -u docauto .venv/bin/python scripts/prepare_ocr_models.py
```

Offline: copy the `models/` folder from the Windows machine (the model files are platform
independent), then run `sudo -u docauto .venv/bin/python scripts/prepare_ocr_models.py --skip-download`.

### 2.8 Oracle connectivity

Thin mode (default) needs no Oracle Client and connects to Oracle Database 19c directly.
For Thick mode, install Oracle Instant Client 19 from Oracle's yum repository and set its folder:

```bash
yum install -y oracle-instantclient-release-el7
yum list available 'oracle-instantclient19*basic*'      # choose a 19.x Basic package
yum install -y oracle-instantclient19.<version>-basic   # example name, use the listed one
# in .env, for example:  ORACLE_CLIENT_LIB_DIR=/usr/lib/oracle/19.<version>/client64/lib
```

Test (read-only):

```bash
sudo -u docauto .venv/bin/python scripts/check_oracle_connection.py
```

### 2.9 Storage directories and permissions

The application creates `storage/` and `logs/` at startup. To create them in advance with safe permissions:

```bash
cd /opt/document-automation
install -d -o docauto -g docauto -m 750 storage storage/documents storage/temp logs models
chmod 600 .env
```

There is **no upload size limit**, so disk space is the only limit. An upload is first received
in the system temporary folder (`/tmp`, private to the service through `PrivateTmp`) and then
copied to `storage/`, so both need free space for the largest file you expect, plus the page
images (an A4 page at 300 DPI is roughly 0.3–1 MB as PNG).

### 2.10 First start (manual test)

```bash
cd /opt/document-automation
sudo -u docauto .venv/bin/python main.py
# in a second terminal:
curl -s http://127.0.0.1:8000/api/health
```

Stop with `Ctrl+C`.

### 2.11 Run as a service (systemd)

```bash
cp deploy/oracle-linux-7.9/document-automation.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now document-automation
systemctl status document-automation
```

### 2.12 Network access

Either open the application port directly (with `APP_HOST=0.0.0.0`):

```bash
firewall-cmd --permanent --add-port=8000/tcp && firewall-cmd --reload
```

or use the optional nginx reverse proxy (keeps `APP_HOST=127.0.0.1`), which is described in
`deploy/oracle-linux-7.9/nginx-document-automation.conf`. With SELinux enforcing, nginx needs
`setsebool -P httpd_can_network_connect 1`.

### 2.13 Logs

```bash
journalctl -u document-automation -f                 # service output
tail -f /opt/document-automation/logs/app.log        # application log (rotates at 10 MB, 5 files kept)
```

### 2.14 Restart and update

```bash
systemctl restart document-automation
```

To update the application: stop the service, back up `.env` and `storage/`, copy the new code
(never overwrite `.env`, `storage/` or `models/`), install dependencies again with the command in
2.5, run the verification script, then start the service. An OCR job cut off by a restart is shown
as *Interrupted* and can be resumed from Document Review.

### 2.15 Verify the installation

```bash
bash deploy/oracle-linux-7.9/verify_installation.sh /opt/document-automation
```

It checks the OS release, glibc, Python 3.11.9, that every native package loads, the OCR models,
the test suite and the health endpoint.

### 2.16 Troubleshooting (Oracle Linux 7.9)

| Problem | Cause / solution |
|---|---|
| `version 'GLIBC_2.28' not found` or `GLIBCXX_3.4.2x not found` | A package newer than `constraints.txt` (or PaddlePaddle) was installed. Recreate `.venv` and install with `-c constraints.txt --only-binary=:all:` |
| pip tries to build a package from source | `--only-binary=:all:` was not used |
| `No package openssl11-devel available` | Enable `ol7_developer_EPEL` (`yum-config-manager --enable ol7_developer_EPEL`) or use a local mirror that contains it. Without OpenSSL 1.1.1 Python 3.11 cannot be built with `ssl` |
| Python build fails with a compiler error | Install a newer GCC from Oracle Software Collections (`oracle-softwarecollection-release-el7`, then `devtoolset-11-gcc`) and run the script inside `scl enable devtoolset-11 bash` |
| `ssl module is not available` in pip | Python was built without OpenSSL 1.1.1. Re-run the install script and check step 1 |
| OCR fails: `Unsupported model IR version` | Run `scripts/prepare_ocr_models.py` (or `--skip-download` for copied models) |
| `Permission denied` under `storage/` or `logs/` | `chown -R docauto:docauto /opt/document-automation` |
| nginx returns 502, SELinux denial in `/var/log/audit/audit.log` | `setsebool -P httpd_can_network_connect 1` |
| nginx returns 413 | Use the provided nginx file; it sets `client_max_body_size 0` (no limit) |
| Upload rejected with "too large to process in memory" | One page is bigger than `MAX_PAGE_PIXELS` at the chosen DPI; use a lower DPI or raise `MAX_PAGE_PIXELS` |
| Upload of a large PDF at high DPI times out behind nginx | Increase `proxy_read_timeout`, or use a lower DPI |

### 2.17 Checks still required on the server

1. `install_python_3.11.9.sh` completes and `ssl`, `sqlite3`, `ctypes`, `lzma` import (system GCC 4.8.5 + `openssl11`).
2. `openssl11-devel` is available in the server's `ol7_developer_EPEL` repository or mirror.
3. `pip install --only-binary=:all: -r requirements-dev.txt -c constraints.txt` resolves without errors.
4. All native packages import (PyMuPDF, NumPy, ONNX Runtime, OpenCV, Pillow, oracledb).
5. `pytest -q` passes (including the real OCR test) and OCR speed is acceptable on the server CPU.
6. python-oracledb connects to the Oracle Database 19c instance (Thin mode, or Thick mode if required by the database's security settings).
7. systemd service starts at boot; the health endpoint answers; SELinux and firewall settings allow access.

---

## API reference

All API responses use one format:

```json
{"success": true,  "message": "...", "data": {}}
{"success": false, "error": {"code": "INVALID_DPI", "message": "DPI must be a whole number from 50 to 300.", "details": null}}
```

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/documents/upload` | multipart: `file` (exactly one), `dpi` (50–300; omitted → 75; empty or invalid → 422) |
| GET | `/api/documents/validation-rules` | allowed types, DPI min/max/default (no size limit) |
| POST | `/api/documents/{id}/convert` | JSON `{"dpi": 150}`: same DPI → images reused; new DPI → new images, old OCR results removed |
| GET | `/api/documents/{id}/pages` | page list with image URLs, sizes and DPI |
| GET | `/api/documents/{id}/pages/{n}/image` | PNG image of page *n* |
| POST | `/api/documents/{id}/ocr` | queue OCR for pages without a successful result (202) |
| POST | `/api/documents/{id}/ocr/detail` | read pages that were not read completely again at `OCR_DETAIL_DPI` (for documents read before) |
| GET | `/api/documents/{id}/ocr/status` | overall and per-page OCR status |
| GET | `/api/documents/{id}/ocr/results` | OCR results of all pages |
| GET | `/api/documents/{id}/pages/{n}/ocr` | OCR result of page *n* |
| GET | `/api/review/documents` | documents for the review list |
| GET | `/api/review/documents/{id}` | document details and pages |
| GET | `/api/review/documents/{id}/pages/{n}` | page, OCR result and a message when no result exists |
| GET | `/api/documents/{id}/invoice-data?page=n` | fields and line items of the invoice that contains page *n* (default 1), its pages, and how many invoices are saved |
| PUT | `/api/documents/{id}/invoice-data` | JSON `{"status": "draft"\|"saved", "values": {...}, "line_items": [...], "page": n, "pages": [...]}`; `saved` needs the required fields; `pages` that no longer match → 409 |
| POST | `/api/documents/{id}/invoice-data/extract` | JSON `{"page": n}`: discard the values of that invoice and read it again |
| POST | `/api/documents/{id}/invoice-data/page-break` | JSON `{"page": n, "starts_invoice": true\|false}`: correct where an invoice starts |
| DELETE | `/api/documents/{id}?confirm=true\|false` | delete the whole document (original, page images, OCR results, saved data). 409 `OCR_IN_PROGRESS` while OCR runs; 409 `DOCUMENT_HAS_POSTED` when invoices were posted to the database, repeat with `confirm=true` to delete anyway (the database rows stay) |
| POST | `/api/documents/{id}/invoice-data/post` | JSON `{"page": n}`: send the **saved** invoice to the Oracle staging tables (`deploy/database/invoice_staging_schema.sql`); it becomes read-only. 503 when Oracle is not configured, 409 `INVOICE_NOT_SAVED` / `INVOICE_POSTED` / `INVOICE_DUPLICATE` |
| POST | `/api/documents/{id}/invoice-data/parse-value` | JSON `{"kind": "text\|code\|date\|amount\|quantity", "text": "..."}` → the value read from that text |
| GET | `/api/health` | status (does not load OCR or connect to Oracle) |

Interactive API documentation: http://127.0.0.1:8000/docs

OCR page statuses: `pending`, `completed`, `no_text` (no readable text), `failed`.
Document OCR statuses: `not_started`, `queued`, `running`, `completed`, `partial`, `failed`, `interrupted`.

---

## Known limitations

- **Oracle staging tables:** the database service connects and runs read-only checks only. Writing
  document or OCR data into the existing staging tables requires their exact names and columns;
  nothing has been written to the database and no schema change has been made.
- **Oracle Linux 7.9:** not yet tested on a server (see 2.17). PaddlePaddle cannot run there; the
  ONNX Runtime engine is used instead.
- **Old dependency versions:** several packages are held at the last versions that support glibc
  2.17 and will not receive further updates on that platform. OpenSSL 1.1.1 (used by Python on
  the server) is no longer maintained upstream.
- **Single process:** one Uvicorn process with one OCR worker; OCR jobs run one after another.
- **No authentication:** the application has no login. Run it on a trusted network or behind a
  reverse proxy that adds authentication.
- **No delete function:** documents stay in `storage/` until removed manually.
- **OCR accuracy:** OCR output can contain errors; always review it.
