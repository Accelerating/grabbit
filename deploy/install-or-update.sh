#!/usr/bin/env bash

set -Eeuo pipefail

readonly APP_NAME="grabbit"
readonly APP_USER="grabbit"
readonly APP_GROUP="grabbit"
readonly INSTALL_ROOT="/opt/grabbit"
readonly RELEASE_ROOT="${INSTALL_ROOT}/releases"
readonly CURRENT_LINK="${INSTALL_ROOT}/current"
readonly STATE_ROOT="/var/lib/grabbit"
readonly DOWNLOAD_ROOT="/srv/grabbit/downloads"
readonly CONFIG_DIR="/etc/grabbit"
readonly ENV_FILE="${CONFIG_DIR}/grabbit.env"
readonly SERVICE_FILE="/etc/systemd/system/grabbit.service"
readonly DEFAULT_REPO="https://github.com/Accelerating/grabbit.git"
readonly DEFAULT_REF="main"
readonly UV_VERSION="0.8.22"

repo="${GRABBIT_REPO:-${DEFAULT_REPO}}"
ref="${GRABBIT_REF:-${DEFAULT_REF}}"
keep_releases=3
skip_system_packages=0
skip_admin=0
first_install=0
new_release=""
old_release=""
switched=0
service_was_active=0

usage() {
  cat <<'EOF'
Install or update Grabbit on Debian 13.

Usage:
  sudo ./deploy/install-or-update.sh [options]

Options:
  --repo URL             Git repository (default: official GitHub repository)
  --ref REF              Branch, tag, or commit to deploy (default: main)
  --keep-releases N      Releases to retain after success (default: 3)
  --skip-system-packages Do not run apt-get; verify required commands instead
  --skip-admin            Do not prompt to create an administrator on first install
  -h, --help             Show this help

The script manages Grabbit only. It listens on 127.0.0.1:8000 and does not
install or configure a domain, TLS, firewall, Caddy, or Nginx.

For a private repository, configure Git credentials for root before running
the script, or pass a credential-helper-compatible repository URL. Tokens are
not written to Grabbit configuration.
EOF
}

log() { printf '[grabbit-deploy] %s\n' "$*"; }
die() { printf '[grabbit-deploy] ERROR: %s\n' "$*" >&2; exit 1; }

while (($#)); do
  case "$1" in
    --repo) (($# >= 2)) || die "--repo requires a value"; repo="$2"; shift 2 ;;
    --ref) (($# >= 2)) || die "--ref requires a value"; ref="$2"; shift 2 ;;
    --keep-releases) (($# >= 2)) || die "--keep-releases requires a value"; keep_releases="$2"; shift 2 ;;
    --skip-system-packages) skip_system_packages=1; shift ;;
    --skip-admin) skip_admin=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || die "run this script as root (sudo)"
[[ "${keep_releases}" =~ ^[1-9][0-9]*$ ]] || die "--keep-releases must be a positive integer"
[[ -n "${repo}" && -n "${ref}" ]] || die "repository and ref cannot be empty"

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "debian" ]] || die "only Debian is currently supported"
  [[ "${VERSION_ID:-}" == "13" ]] || die "Debian 13 is required"
else
  die "cannot identify the operating system"
fi

rollback() {
  local code=$?
  if ((code != 0)); then
    log "deployment failed"
    if ((switched)) && [[ -n "${old_release}" && -d "${old_release}" ]]; then
      log "rolling back to ${old_release}"
      ln -sfn "${old_release}" "${CURRENT_LINK}"
      systemctl daemon-reload || true
      systemctl restart "${APP_NAME}.service" || true
    elif ((service_was_active)); then
      systemctl start "${APP_NAME}.service" || true
    fi
  fi
  exit "${code}"
}
trap rollback EXIT

install_system_packages() {
  if ((skip_system_packages)); then
    local command
    for command in git curl python3 aria2c ffmpeg ffprobe sudo; do
      command -v "${command}" >/dev/null 2>&1 || die "required command is missing: ${command}"
    done
    return
  fi
  log "installing system packages"
  apt-get -o DPkg::Lock::Timeout=60 update
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=60 install -y \
    ca-certificates curl git sudo python3.13 python3.13-venv aria2 ffmpeg
}

install_uv() {
  local uv_venv="${INSTALL_ROOT}/tools/uv"
  if [[ -x "${uv_venv}/bin/uv" ]] && [[ "$("${uv_venv}/bin/uv" --version)" == "uv ${UV_VERSION} "* ]]; then
    return
  fi
  log "installing uv ${UV_VERSION} in ${uv_venv}"
  python3.13 -m venv "${uv_venv}"
  "${uv_venv}/bin/python" -m pip install --disable-pip-version-check --no-cache-dir "uv==${UV_VERSION}"
}

prepare_host() {
  getent group "${APP_GROUP}" >/dev/null || groupadd --system "${APP_GROUP}"
  if ! id "${APP_USER}" >/dev/null 2>&1; then
    useradd --system --gid "${APP_GROUP}" --home-dir "${STATE_ROOT}" --shell /usr/sbin/nologin "${APP_USER}"
  fi
  install -d -o root -g root -m 0755 "${INSTALL_ROOT}" "${RELEASE_ROOT}" "${INSTALL_ROOT}/tools"
  install -d -o root -g "${APP_GROUP}" -m 0750 "${CONFIG_DIR}"
  install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0700 "${STATE_ROOT}" "${STATE_ROOT}/private" "${STATE_ROOT}/backups"
  install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 "${DOWNLOAD_ROOT}"

  if [[ ! -f "${ENV_FILE}" ]]; then
    first_install=1
    log "creating ${ENV_FILE}"
    install -o root -g "${APP_GROUP}" -m 0640 /dev/null "${ENV_FILE}"
    cat >"${ENV_FILE}" <<EOF
GRABBIT_PUBLIC_ORIGIN=http://localhost:8000
GRABBIT_SECURE_COOKIES=false
GRABBIT_DATABASE_URL=sqlite+aiosqlite:////var/lib/grabbit/grabbit.db
GRABBIT_PRIVATE_ROOT=/var/lib/grabbit/private
GRABBIT_DOWNLOAD_ROOT=/srv/grabbit/downloads
EOF
  fi
}

checkout_release() {
  local staging commit
  staging="$(mktemp -d "${RELEASE_ROOT}/.staging.XXXXXX")"
  log "fetching ${repo} (${ref})"
  git clone --filter=blob:none --no-checkout "${repo}" "${staging}"
  git -C "${staging}" fetch --depth 1 origin "${ref}"
  git -C "${staging}" checkout --detach FETCH_HEAD
  commit="$(git -C "${staging}" rev-parse --short=12 HEAD)"
  new_release="${RELEASE_ROOT}/${commit}"
  if [[ -e "${new_release}" ]]; then
    rm -rf "${staging}"
    [[ -d "${new_release}/backend" ]] || die "existing release is incomplete: ${new_release}"
    log "release ${commit} already exists; reusing it"
  else
    mv "${staging}" "${new_release}"
  fi
  [[ -f "${new_release}/backend/uv.lock" ]] || die "backend/uv.lock is missing"
  [[ -f "${new_release}/backend/app/static/index.html" ]] || die "prebuilt frontend is missing; build and commit backend/app/static first"
  old_release="$(readlink -f "${CURRENT_LINK}" 2>/dev/null || true)"
}

sync_dependencies() {
  local uv="${INSTALL_ROOT}/tools/uv/bin/uv"
  log "installing locked Python dependencies"
  (
    cd "${new_release}/backend"
    "${uv}" sync --python 3.13 --locked --no-dev
  )

  local video_env="${INSTALL_ROOT}/tools/yt-dlp/current"
  if [[ ! -x "${video_env}/bin/yt-dlp" ]]; then
    log "installing yt-dlp tool environment"
    "${uv}" venv --python 3.13 "${video_env}"
    "${uv}" pip install --python "${video_env}/bin/python" 'yt-dlp[default]'
  fi
}

install_service_files() {
  install -o root -g root -m 0644 "${new_release}/deploy/systemd/grabbit.service" "${SERVICE_FILE}"
  install -d -o root -g root -m 0755 /usr/local/libexec
  install -o root -g root -m 0755 "${new_release}/deploy/grabbit-install" /usr/local/libexec/grabbit-install
  install -o root -g root -m 0440 "${new_release}/deploy/sudoers/grabbit-install" /etc/sudoers.d/grabbit-install
  visudo -cf /etc/sudoers.d/grabbit-install >/dev/null
  systemctl daemon-reload
}

backup_current() {
  [[ -n "${old_release}" && -x "${old_release}/backend/.venv/bin/python" ]] || return
  local output="${STATE_ROOT}/backups/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ)"
  log "backing up persistent state to ${output}"
  (
    cd "${old_release}/backend"
    runuser -u "${APP_USER}" -- bash -c \
      "set -a; source '${ENV_FILE}'; set +a; .venv/bin/python -m app.cli backup --output '${output}' --env-file '${ENV_FILE}'"
  )
}

run_as_app() {
  local command="$1"
  runuser -u "${APP_USER}" -- bash -c "set -a; source '${ENV_FILE}'; set +a; ${command}"
}

activate_release() {
  if systemctl is-active --quiet "${APP_NAME}.service"; then
    service_was_active=1
  fi
  backup_current
  systemctl stop "${APP_NAME}.service" 2>/dev/null || true
  log "running database migrations"
  (cd "${new_release}/backend" && run_as_app ".venv/bin/python -m app.cli db upgrade")
  ln -sfn "${new_release}" "${CURRENT_LINK}"
  switched=1
  systemctl enable --now "${APP_NAME}.service"

  local attempt
  for attempt in {1..20}; do
    if curl --fail --silent --show-error http://127.0.0.1:8000/healthz >/dev/null; then
      log "health check passed"
      return
    fi
    sleep 1
  done
  journalctl -u "${APP_NAME}.service" -n 30 --no-pager >&2 || true
  die "health check failed"
}

create_admin_if_needed() {
  if ((first_install && !skip_admin)); then
    [[ -t 0 ]] || die "first install needs an interactive terminal to create the administrator; rerun with --skip-admin to defer it"
    log "create the initial administrator"
    (cd "${CURRENT_LINK}/backend" && run_as_app ".venv/bin/python -m app.cli admin create")
  fi
}

prune_releases() {
  mapfile -t releases < <(find "${RELEASE_ROOT}" -mindepth 1 -maxdepth 1 -type d ! -name '.staging.*' -printf '%T@ %p\n' | sort -rn | awk '{print $2}')
  local index path
  for ((index=keep_releases; index<${#releases[@]}; index++)); do
    path="${releases[index]}"
    [[ "$(readlink -f "${CURRENT_LINK}")" == "$(readlink -f "${path}")" ]] && continue
    log "removing old release ${path}"
    rm -rf --one-file-system "${path}"
  done
}

install_system_packages
prepare_host
install_uv
checkout_release
sync_dependencies
install_service_files
activate_release
create_admin_if_needed
prune_releases

trap - EXIT
log "deployment complete: $(readlink -f "${CURRENT_LINK}")"
log "Grabbit is listening on http://127.0.0.1:8000"
