#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build and install Python 3.11.9 on Oracle Linux 7.9.
#
#   sudo bash deploy/oracle-linux-7.9/install_python_3.11.9.sh
#
# Result: /opt/python-3.11.9/bin/python3.11
# The system Python (2.7, used by yum) is NOT changed ("make altinstall").
#
# Why a source build: Oracle Linux 7.9 has no Python 3.11 package.
# Why openssl11:      Python 3.11 requires OpenSSL >= 1.1.1 (checked by its
#                     configure script); Oracle Linux 7 ships OpenSSL 1.0.2.
#                     OpenSSL 1.1.1 is installed side by side from Oracle's
#                     EPEL mirror (package openssl11), without replacing 1.0.2.
#
# STATUS: prepared from the Python 3.11.9 source (checksum and configure
# script verified), NOT yet executed on an Oracle Linux 7.9 server.
# ---------------------------------------------------------------------------
set -euo pipefail

PYTHON_VERSION="3.11.9"
PYTHON_SHA256="e7de3240a8bc2b1e1ba5c81bf943f06861ff494b69fda990ce2722a504c6153d"
PREFIX="/opt/python-${PYTHON_VERSION}"
BUILD_DIR="/usr/local/src"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this script as root (sudo)." >&2
    exit 1
fi

echo "== Operating system: $(cat /etc/oracle-release 2>/dev/null || echo unknown)"
if ! grep -q "release 7.9" /etc/oracle-release 2>/dev/null; then
    echo "WARNING: this script was written for Oracle Linux 7.9." >&2
fi

echo "== 1/5 Installing build dependencies"
yum install -y oracle-epel-release-el7 yum-utils
yum-config-manager --enable ol7_latest ol7_optional_latest ol7_developer_EPEL >/dev/null
yum install -y \
    gcc make pkgconfig wget tar xz \
    zlib-devel bzip2-devel xz-devel libffi-devel readline-devel sqlite-devel \
    ncurses-devel gdbm-devel tk-devel libuuid-devel expat-devel \
    openssl11 openssl11-devel

pkg-config --modversion openssl11 >/dev/null || {
    echo "openssl11-devel is not available. See README: Troubleshooting > OpenSSL 1.1.1." >&2
    exit 1
}
echo "   OpenSSL for Python: $(pkg-config --modversion openssl11)"

echo "== 2/5 Downloading Python ${PYTHON_VERSION} source"
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
if [ ! -f "Python-${PYTHON_VERSION}.tgz" ]; then
    wget "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tgz"
fi
echo "${PYTHON_SHA256}  Python-${PYTHON_VERSION}.tgz" | sha256sum -c -

echo "== 3/5 Configuring"
rm -rf "Python-${PYTHON_VERSION}"
tar -xzf "Python-${PYTHON_VERSION}.tgz"
cd "Python-${PYTHON_VERSION}"
# Point configure at OpenSSL 1.1.1 (pkg-config name "openssl11") instead of 1.0.2.
sed -i 's/PKG_CONFIG openssl /PKG_CONFIG openssl11 /g' configure
./configure --prefix="$PREFIX" --with-ensurepip=install

echo "== 4/5 Building (this takes several minutes)"
make -j"$(nproc)"
make altinstall

echo "== 5/5 Verifying"
"$PREFIX/bin/python3.11" --version
"$PREFIX/bin/python3.11" - <<'EOF'
import platform, ssl, sqlite3, ctypes, lzma, bz2, zlib, uuid, readline
assert platform.python_version() == "3.11.9", platform.python_version()
print("Python", platform.python_version(), "| OpenSSL:", ssl.OPENSSL_VERSION, "| SQLite:", sqlite3.sqlite_version)
print("glibc:", platform.libc_ver())
EOF
echo "Python ${PYTHON_VERSION} installed in ${PREFIX}"
