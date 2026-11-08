#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Verify a Document Automation installation on Oracle Linux 7.9.
#
#   bash deploy/oracle-linux-7.9/verify_installation.sh [/opt/document-automation]
#
# Prints PASS/FAIL for each check. Read-only except for the test suite, which
# writes only to a temporary folder.
# ---------------------------------------------------------------------------
set -uo pipefail

APP_DIR="${1:-/opt/document-automation}"
PY="$APP_DIR/.venv/bin/python"
FAILED=0
cd "$APP_DIR" || { echo "Folder not found: $APP_DIR"; exit 1; }

check() {
    local name="$1"; shift
    if "$@"; then echo "[PASS] $name"; else echo "[FAIL] $name"; FAILED=$((FAILED + 1)); fi
}

echo "== System"
echo "OS:     $(cat /etc/oracle-release 2>/dev/null || echo unknown)"
echo "Kernel: $(uname -r)"
echo "glibc:  $(ldd --version | head -1)"
echo "libstdc++ max: $(strings /usr/lib64/libstdc++.so.6 | grep -E '^GLIBCXX_3\.4\.[0-9]+$' | sort -V | tail -1)"
check "Oracle Linux 7.9" grep -q "release 7.9" /etc/oracle-release

echo
echo "== Python"
check "virtual environment exists" test -x "$PY"
check "Python is exactly 3.11.9" "$PY" -c 'import platform, sys; sys.exit(platform.python_version() != "3.11.9")'

echo
echo "== Native packages load on this glibc"
check "imports (PyMuPDF, NumPy, ONNX Runtime, OpenCV, Pillow, PaddleOCR, oracledb, FastAPI)" \
    "$PY" -c "
import fitz, numpy, onnxruntime, cv2, PIL, oracledb, fastapi, paddleocr, ssl
print('   PyMuPDF', fitz.VersionBind, '| NumPy', numpy.__version__, '| ONNX Runtime', onnxruntime.__version__,
      '| OpenCV', cv2.__version__, '| PaddleOCR', paddleocr.__version__, '| oracledb', oracledb.__version__)
print('   OpenSSL', ssl.OPENSSL_VERSION)
"

echo
echo "== OCR models"
check "OCR models prepared" "$PY" scripts/prepare_ocr_models.py --skip-download

echo
echo "== Automated tests (needs requirements-dev.txt)"
if "$PY" -c "import pytest" 2>/dev/null; then
    check "pytest suite" "$PY" -m pytest -q
else
    echo "[SKIP] pytest is not installed (pip install -r requirements-dev.txt -c constraints.txt)"
fi

echo
echo "== Running service (optional)"
if systemctl is-active --quiet document-automation; then
    check "health endpoint" bash -c "curl -sf http://127.0.0.1:8000/api/health | grep -q '\"status\":\"ok\"'"
else
    echo "[SKIP] document-automation service is not running"
fi

echo
if [ "$FAILED" -eq 0 ]; then echo "All checks passed."; else echo "$FAILED check(s) failed."; fi
exit "$FAILED"
