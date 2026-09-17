#!/usr/bin/env bash

set -Eeuo pipefail

# Build the frontend on a development/CI machine and put only the browser
# bundle in the backend release.  The VPS must not run the frontend dev server
# or a Node build.

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

FRONTEND_DIR="${FRONTEND_DIR:-${REPO_DIR}/frontend}"
BACKEND_STATIC_DIR="${BACKEND_STATIC_DIR:-${REPO_DIR}/backend/app/static}"
CLIENT_BUILD_DIR="${CLIENT_BUILD_DIR:-${FRONTEND_DIR}/build/client}"
PNPM_BIN="${PNPM_BIN:-pnpm}"
SKIP_INSTALL="${SKIP_INSTALL:-0}"

die() {
  printf 'build_frontend: %s\n' "$*" >&2
  exit 1
}

[[ -d "${FRONTEND_DIR}" ]] || die "frontend directory does not exist: ${FRONTEND_DIR}"
command -v "${PNPM_BIN}" >/dev/null 2>&1 || die "pnpm is required (set PNPM_BIN to an explicit executable if needed)"

if [[ "${SKIP_INSTALL}" != "1" ]]; then
  (
    cd "${FRONTEND_DIR}"
    "${PNPM_BIN}" install --frozen-lockfile
  )
fi

(
  cd "${FRONTEND_DIR}"
  "${PNPM_BIN}" run build
)

[[ -d "${CLIENT_BUILD_DIR}" ]] || die "frontend build did not produce ${CLIENT_BUILD_DIR}"
[[ -f "${CLIENT_BUILD_DIR}/index.html" ]] || die "frontend build has no index.html; enable React Router SPA mode (ssr: false)"

static_parent="$(dirname "${BACKEND_STATIC_DIR}")"
mkdir -p "${static_parent}"

# Stage first and swap the directory at the end.  A failed copy/build therefore
# leaves the previous static bundle intact, and stale hashed assets are not
# retained between releases.
stage_dir="$(mktemp -d "${static_parent}/.static-stage.XXXXXX")"
backup_dir=''
old_static_moved=0
cleanup() {
  exit_code=$?
  restore_failed=0

  # If replacing the bundle failed after moving the old directory away, put it
  # back before cleaning temporary paths.  This keeps a usable release on any
  # failed filesystem operation.
  if [[ "${old_static_moved}" == "1" \
    && ! -e "${BACKEND_STATIC_DIR}" \
    && -n "${backup_dir}" \
    && -e "${backup_dir}" ]]; then
    if ! mv "${backup_dir}" "${BACKEND_STATIC_DIR}"; then
      printf 'build_frontend: could not restore previous static bundle at %s\n' "${backup_dir}" >&2
      restore_failed=1
      exit_code=1
    fi
  fi
  if [[ "${restore_failed}" == "0" && -n "${backup_dir}" && -e "${backup_dir}" ]]; then
    rm -rf "${backup_dir}"
  fi
  if [[ -e "${stage_dir}" ]]; then
    rm -rf "${stage_dir}"
  fi
  return "${exit_code}"
}
trap cleanup EXIT

chmod 755 "${stage_dir}"
cp -a "${CLIENT_BUILD_DIR}/." "${stage_dir}/"

if [[ -e "${BACKEND_STATIC_DIR}" || -L "${BACKEND_STATIC_DIR}" ]]; then
  backup_dir="$(mktemp -d "${static_parent}/.static-old.XXXXXX")"
  rmdir "${backup_dir}"
  mv "${BACKEND_STATIC_DIR}" "${backup_dir}"
  old_static_moved=1
fi
mv "${stage_dir}" "${BACKEND_STATIC_DIR}"
stage_dir=''

if [[ -n "${backup_dir}" && -e "${backup_dir}" ]]; then
  rm -rf "${backup_dir}"
  backup_dir=''
fi
old_static_moved=0

trap - EXIT INT TERM
printf 'Frontend bundle copied to %s\n' "${BACKEND_STATIC_DIR}"
