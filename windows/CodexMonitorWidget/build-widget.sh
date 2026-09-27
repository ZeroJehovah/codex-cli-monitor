#!/usr/bin/env bash
set -euo pipefail

# Build CodexMonitorWidget.exe without root access and without installing a
# system-wide toolchain.  Ubuntu's mingw-w64 packages are downloaded and
# unpacked into a private prefix, then reused for every later build.
#
# Usage: windows/CodexMonitorWidget/build-widget.sh [output-directory]
#
# Defaults to dist/CodexMonitorWidget-win-x64/.

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
WIDGET_DIR="${REPO_ROOT}/windows/CodexMonitorWidget"
CACHE_DIR="${CODEX_MONITOR_MINGW_CACHE:-/tmp/codex-mingw}"
DEB_DIR="${CACHE_DIR}/debs"
# Ubuntu's packages unpack under usr/, so keep the same layout in the cache.
PREFIX="${CACHE_DIR}/root"
TOOL_BIN="${PREFIX}/usr/bin"
TOOL_INCLUDE="${PREFIX}/usr/x86_64-w64-mingw32/include"
OUTPUT_DIR="${1:-${REPO_ROOT}/dist/CodexMonitorWidget-win-x64}"

PACKAGES=(
  mingw-w64-common
  mingw-w64-x86-64-dev
  gcc-mingw-w64-base
  gcc-mingw-w64-x86-64-posix
  gcc-mingw-w64-x86-64-posix-runtime
  binutils-mingw-w64
  binutils-mingw-w64-x86-64
)

CC_BIN="${TOOL_BIN}/x86_64-w64-mingw32-gcc-posix"
if [[ ! -x "${CC_BIN}" ]]; then
  echo "Fetching mingw-w64 packages into ${CACHE_DIR}"
  mkdir -p "${DEB_DIR}" "${PREFIX}"
  (cd "${DEB_DIR}" && apt-get download "${PACKAGES[@]}")
  for deb in "${DEB_DIR}"/*.deb; do
    dpkg -x "${deb}" "${PREFIX}"
  done
fi

make -C "${WIDGET_DIR}" clean >/dev/null 2>&1 || true
PATH="${TOOL_BIN}:${PATH}" \
CC="${CC_BIN}" \
BINUTILS_DIR="${TOOL_BIN}" \
MINGW_INCLUDE_DIR="${TOOL_INCLUDE}" \
  make -C "${WIDGET_DIR}"

mkdir -p "${OUTPUT_DIR}"
cp -f "${WIDGET_DIR}/CodexMonitorWidget.exe" "${OUTPUT_DIR}/CodexMonitorWidget.exe"
if [[ ! -f "${OUTPUT_DIR}/CodexMonitorWidget.ini" ]]; then
  cp -f "${WIDGET_DIR}/CodexMonitorWidget.ini.example" "${OUTPUT_DIR}/CodexMonitorWidget.ini"
fi
echo "Built ${OUTPUT_DIR}/CodexMonitorWidget.exe"
echo "Deploy by replacing the exe next to the existing CodexMonitorWidget.ini"
