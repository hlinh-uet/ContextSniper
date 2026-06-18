#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON:-python3}"
VENV_DIR="$ROOT_DIR/.venv"
FORCE_AGFS=0
SKIP_PYTHON=0
SKIP_AGFS=0
INSTALL_OPENCLAW=0
INSTALL_CLAUDE=0
INSTALL_SYSTEM_DEPS=1
INSTALL_SWE_DEPS=0
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage: ./bootstrap.sh [options]

Prepare a fresh ContextSniper checkout for local Claude/OpenClaw use.

Options:
  -y, --yes                 Accept recommended installer prompts.
  --no-system-deps          Do not auto-install missing Ubuntu packages.
  --force-agfs              Rebuild agfs/build/agfs-server even if it exists.
  --skip-python             Do not create .venv or install requirements.txt.
  --skip-agfs               Do not build the bundled AGFS server.
  --install-openclaw-plugin Install and enable the linked OpenClaw plugin.
  --install-claude-cli      Install Claude Code CLI if missing.
  --install-swe-deps        Install optional SWE-bench runner dependencies.
  -h, --help                Show this help.

Put local API keys and overrides in your shell profile or export them before
running the tools. Then load repo defaults with: source setup_env.sh
EOF
}

log() {
  printf '[bootstrap] %s\n' "$*"
}

die() {
  printf '[bootstrap] error: %s\n' "$*" >&2
  exit 1
}

prompt_yes_no() {
  local prompt="$1"
  local default="${2:-y}"
  local answer suffix

  if [ "$ASSUME_YES" -eq 1 ]; then
    log "$prompt $default"
    [ "$default" = "y" ]
    return
  fi

  if [ "$default" = "y" ]; then
    suffix="[Y/n]"
  else
    suffix="[y/N]"
  fi

  printf '[bootstrap] %s %s ' "$prompt" "$suffix"
  read -r answer || answer=""
  answer="${answer:-$default}"
  case "$answer" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

have() {
  command -v "$1" >/dev/null 2>&1
}

run_apt() {
  if [ "$(id -u)" -eq 0 ]; then
    # Rootless/user-namespace containers may not allow apt to drop privileges to
    # the _apt user. Keeping the sandbox user as root lets fresh container
    # bootstrap continue in those disposable environments.
    DEBIAN_FRONTEND=noninteractive apt-get -o APT::Sandbox::User=root "$@"
  elif have sudo; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get "$@"
  else
    return 1
  fi
}

install_ripgrep_with_brew() {
  prompt_yes_no "Missing rg/ripgrep. Install it with Homebrew now?" "y" || \
    die "missing rg; install it with: brew install ripgrep"
  brew install ripgrep || die "failed to install ripgrep with Homebrew"
}

ensure_ripgrep() {
  have rg && return 0

  if have apt-get; then
    prompt_yes_no "Missing rg/ripgrep. Install it with apt-get now?" "y" || \
      die "missing rg; install it with: sudo apt-get install ripgrep"
    run_apt update || die "cannot run apt-get update for ripgrep"
    run_apt install -y ripgrep || die "failed to install ripgrep"
    have rg || die "ripgrep installed, but rg is still not on PATH"
    return 0
  fi

  if [ "$(uname -s 2>/dev/null || true)" = "Darwin" ]; then
    have brew || die "missing rg and Homebrew; install Homebrew, then run: brew install ripgrep"
    install_ripgrep_with_brew
    have rg || die "ripgrep installed, but rg is still not on PATH; open a new shell or check Homebrew PATH"
    return 0
  fi

  if have brew; then
    install_ripgrep_with_brew
    have rg || die "ripgrep installed, but rg is still not on PATH"
    return 0
  fi

  die "missing rg/ripgrep; install ripgrep with your system package manager and rerun bootstrap"
}

ensure_system_deps() {
  [ "$INSTALL_SYSTEM_DEPS" -eq 1 ] || return 0
  if ! have apt-get; then
    ensure_ripgrep
    return 0
  fi

  local missing=0
  local cmd
  for cmd in curl git make rg go "$PYTHON_BIN"; do
    if ! have "$cmd"; then
      missing=1
    fi
  done

  if have "$PYTHON_BIN" && ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
    missing=1
  fi

  if [ "$missing" -eq 0 ]; then
    log "system packages look ready"
    return 0
  fi

  log "installing missing Ubuntu packages"
  prompt_yes_no "Missing Ubuntu packages are required. Install them with apt-get now?" "y" || \
    die "missing system packages; install python3 python3-venv python3-pip make git curl ripgrep golang-go"
  run_apt update || die "missing system packages and cannot run apt-get; install python3 python3-venv python3-pip make git curl ripgrep golang-go"
  run_apt install -y \
    ca-certificates curl git make build-essential ripgrep \
    python3 python3-venv python3-pip golang-go || die "failed to install Ubuntu packages"
  have rg || die "ripgrep installed, but rg is still not on PATH"
}

ensure_openclaw_cli() {
  have openclaw && return 0

  if [ -x "$HOME/.openclaw/bin/openclaw" ]; then
    repair_openclaw_node_link
    export PATH="$HOME/.openclaw/bin:$PATH"
    if [ "$(id -u)" -eq 0 ] || [ -w /usr/local/bin ]; then
      ln -sf "$HOME/.openclaw/bin/openclaw" /usr/local/bin/openclaw
    else
      mkdir -p "$HOME/.local/bin"
      ln -sf "$HOME/.openclaw/bin/openclaw" "$HOME/.local/bin/openclaw"
      export PATH="$HOME/.local/bin:$PATH"
    fi
    have openclaw && return 0
  fi

  have curl || die "missing curl; cannot install OpenClaw CLI"
  log "installing OpenClaw CLI"
  curl -fsSL https://openclaw.ai/install-cli.sh | bash
  repair_openclaw_node_link
  export PATH="$HOME/.local/bin:$HOME/.openclaw/bin:$PATH"
  if [ -x "$HOME/.openclaw/bin/openclaw" ]; then
    if [ "$(id -u)" -eq 0 ] || [ -w /usr/local/bin ]; then
      ln -sf "$HOME/.openclaw/bin/openclaw" /usr/local/bin/openclaw
    else
      mkdir -p "$HOME/.local/bin"
      ln -sf "$HOME/.openclaw/bin/openclaw" "$HOME/.local/bin/openclaw"
    fi
  fi
  have openclaw || die "OpenClaw installer finished, but openclaw is not on PATH; open a new shell or add the installer path to PATH"
}

repair_openclaw_node_link() {
  local tools_dir="$HOME/.openclaw/tools"
  local node_link="$tools_dir/node"
  local node_dir

  [ -d "$tools_dir" ] || return 0
  if [ -x "$node_link/bin/node" ]; then
    return 0
  fi

  node_dir="$(find "$tools_dir" -maxdepth 1 -type d -name 'node-v*' 2>/dev/null | sort -V | tail -1 || true)"
  if [ -n "$node_dir" ] && [ -x "$node_dir/bin/node" ]; then
    log "repairing OpenClaw Node symlink"
    ln -sfn "$node_dir" "$node_link"
  fi
}

node_major_version() {
  node -v 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/'
}

ensure_node_npm() {
  if have node && have npm; then
    local node_major
    node_major="$(node_major_version)"
    if [ -n "$node_major" ] && [ "$node_major" -ge 18 ]; then
      return 0
    fi
  fi

  have apt-get || die "missing Node.js/npm; install Node.js 18+ and npm, then rerun bootstrap"
  prompt_yes_no "Claude Code needs Node.js 18+ and npm. Install Ubuntu nodejs/npm packages now?" "y" || \
    die "missing Node.js/npm for Claude Code CLI"
  run_apt update || die "cannot run apt-get update for nodejs/npm"
  run_apt install -y nodejs npm || die "failed to install nodejs/npm"

  local node_major
  node_major="$(node_major_version)"
  if [ -z "$node_major" ] || [ "$node_major" -lt 18 ]; then
    die "Claude Code requires Node.js 18+; found $(node -v 2>/dev/null || printf missing)"
  fi
}

run_npm_global_install() {
  if [ "$(id -u)" -eq 0 ]; then
    npm install -g "$@"
  elif have sudo; then
    sudo npm install -g "$@"
  else
    npm install -g "$@"
  fi
}

ensure_claude_cli() {
  have claude && return 0

  ensure_node_npm
  log "installing Claude Code CLI"
  run_npm_global_install @anthropic-ai/claude-code
  have claude || die "Claude Code installer finished, but claude is not on PATH"
}

prepare_openclaw_plugin_dir() {
  PLUGIN_DIR="$ROOT_DIR/openclaw-plugin"
  local plugin_uid
  plugin_uid="$(stat -c '%u' "$PLUGIN_DIR" 2>/dev/null || id -u)"
  if [ "$plugin_uid" = "$(id -u)" ]; then
    return 0
  fi

  PLUGIN_DIR="${CONTEXTSNIPER_OPENCLAW_PLUGIN_DIR:-$HOME/.cache/contextsniper/openclaw-plugin}"
  local cache_root
  cache_root="$(dirname "$PLUGIN_DIR")"
  log "copying OpenClaw plugin to current-user-owned path: $PLUGIN_DIR"
  rm -rf "$PLUGIN_DIR"
  mkdir -p "$PLUGIN_DIR"
  tar -C "$ROOT_DIR/openclaw-plugin" -cf - . | tar --no-same-owner -C "$PLUGIN_DIR" -xf -

  # Keep the linked plugin user-owned while letting it auto-detect the source tree.
  mkdir -p "$cache_root"
  ln -sfn "$ROOT_DIR/claude-plugin" "$cache_root/claude-plugin"
  ln -sfn "$ROOT_DIR/server" "$cache_root/server"
  ln -sfn "$ROOT_DIR/agfs" "$cache_root/agfs"
  ln -sfn "$ROOT_DIR/.venv" "$cache_root/.venv"
  ln -sfn "$ROOT_DIR/setup_env.sh" "$cache_root/setup_env.sh"
  if [ -d "$ROOT_DIR/code-version" ]; then
    ln -sfn "$ROOT_DIR/code-version" "$cache_root/code-version"
  fi
}

load_local_env() {
  set +e +u
  if [ -f "$ROOT_DIR/setup_env.sh" ]; then
    # shellcheck disable=SC1091
    source "$ROOT_DIR/setup_env.sh" >/dev/null 2>&1
  fi
  set -e -u
  return 0
}

openclaw_provider_from_model() {
  local model="${OPENCLAW_MODEL:-}"
  if [ -n "${OPENCLAW_PROVIDER:-}" ]; then
    printf '%s\n' "$OPENCLAW_PROVIDER"
  elif [ "$model" != "${model#*/}" ]; then
    printf '%s\n' "${model%%/*}"
  else
    printf '%s\n' "openai"
  fi
}

ensure_go_version() {
  have go || die "missing Go; install Go 1.22+ or rerun with --skip-agfs"

  local version major minor
  version="$(go version | awk '{print $3}')"
  version="${version#go}"
  major="${version%%.*}"
  minor="${version#*.}"
  minor="${minor%%.*}"

  if ! [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]]; then
    die "could not parse Go version from: $(go version)"
  fi
  if [ "$major" -lt 1 ] || { [ "$major" -eq 1 ] && [ "$minor" -lt 22 ]; }; then
    die "Go 1.22+ is required to build AGFS; found $(go version). Install a newer Go or rerun with --skip-agfs"
  fi
}

ensure_openclaw_model_auth() {
  [ "$INSTALL_OPENCLAW" -eq 1 ] || return 0
  [ -n "${OPENCLAW_MODEL:-}" ] || return 0

  local provider api_key profile
  provider="$(openclaw_provider_from_model)"
  api_key="${OPENCLAW_API_KEY:-${OPENAI_API_KEY:-}}"
  profile="${OPENCLAW_AUTH_PROFILE:-$provider:manual}"

  if [ -n "$api_key" ]; then
    if ! openclaw models auth list 2>/dev/null | grep -q "$profile"; then
      log "configuring OpenClaw auth profile $profile"
      printf '%s\n' "$api_key" | openclaw models auth paste-api-key --provider "$provider" --profile-id "$profile" >/dev/null
    fi
  fi

  log "setting OpenClaw default model to $OPENCLAW_MODEL"
  openclaw models set "$OPENCLAW_MODEL" >/dev/null || true
}

verify_openclaw_runtime() {
  local inspect
  if ! inspect="$(openclaw plugins inspect contextsniper --runtime --json 2>&1)"; then
    printf '%s\n' "$inspect" >&2
    die "OpenClaw plugin installed but runtime inspection failed"
  fi
  printf '%s\n' "$inspect" | grep -q "contextsniper_search_code" || die "OpenClaw plugin loaded, but contextsniper_search_code was not visible"
  printf '%s\n' "$inspect" | grep -q "contextsniper_edit_file" || die "OpenClaw plugin loaded, but contextsniper_edit_file was not visible"
  log "OpenClaw plugin runtime verified"
}

warn_openclaw_auth() {
  local status
  status="$(openclaw models status 2>&1 || true)"
  if printf '%s\n' "$status" | grep -Eiq 'missing|not configured|no configured|no model|No API key'; then
    log "OpenClaw model auth is not configured yet; run: openclaw configure"
  fi
}

setup_claude_integration() {
  if have claude; then
    log "Claude Code CLI found: $(command -v claude)"
  else
    log "Claude Code CLI not found"
  fi

  if [ "$INSTALL_CLAUDE" -eq 0 ]; then
    if prompt_yes_no "Set up Claude Code CLI/helper now?" "n"; then
      INSTALL_CLAUDE=1
    fi
  fi

  if [ "$INSTALL_CLAUDE" -eq 1 ]; then
    ensure_claude_cli
    log "Claude helper ready: $ROOT_DIR/claude-plugin/bin/contextsniper-claude"
  fi
}

setup_openclaw_integration() {
  if have openclaw; then
    log "OpenClaw CLI found: $(command -v openclaw)"
  else
    log "OpenClaw CLI not found"
  fi

  if [ "$INSTALL_OPENCLAW" -eq 0 ]; then
    if prompt_yes_no "Set up OpenClaw CLI/plugin now?" "y"; then
      INSTALL_OPENCLAW=1
    fi
  fi

  if [ "$INSTALL_OPENCLAW" -eq 1 ]; then
    ensure_openclaw_cli
    have openclaw || die "missing openclaw CLI"
    ensure_openclaw_model_auth
    prepare_openclaw_plugin_dir
    log "installing linked OpenClaw plugin"
    openclaw plugins uninstall contextsniper --force >/dev/null 2>&1 || true
    openclaw plugins install --link "$PLUGIN_DIR" --dangerously-force-unsafe-install
    openclaw plugins enable contextsniper
    verify_openclaw_runtime
    warn_openclaw_auth
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    -y|--yes)
      ASSUME_YES=1
      ;;
    --no-system-deps)
      INSTALL_SYSTEM_DEPS=0
      ;;
    --force-agfs)
      FORCE_AGFS=1
      ;;
    --skip-python)
      SKIP_PYTHON=1
      ;;
    --skip-agfs)
      SKIP_AGFS=1
      ;;
    --install-openclaw-plugin)
      INSTALL_OPENCLAW=1
      ;;
    --install-claude-cli)
      INSTALL_CLAUDE=1
      ;;
    --install-swe-deps)
      INSTALL_SWE_DEPS=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
  shift
done

cd "$ROOT_DIR"
log "repository: $ROOT_DIR"
ensure_system_deps

if [ "$SKIP_PYTHON" -eq 0 ]; then
  have "$PYTHON_BIN" || die "missing Python: $PYTHON_BIN"
  if [ -d "$VENV_DIR" ] && [ ! -x "$VENV_DIR/bin/python" ]; then
    log "removing incomplete Python virtual environment at .venv"
    rm -rf "$VENV_DIR"
  fi
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    prompt_yes_no "Create a Python virtual environment and install requirements.txt now?" "y" || \
      die "Python environment is required; rerun with --skip-python only if you manage dependencies yourself"
    log "creating Python virtual environment at .venv"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  else
    log "using existing .venv"
  fi

  log "installing Python dependencies from requirements.txt"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  "$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/requirements.txt"
  if [ "$INSTALL_SWE_DEPS" -eq 1 ]; then
    log "installing optional SWE-bench dependencies from requirements-swe.txt"
    "$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/requirements-swe.txt"
  fi
else
  log "skipping Python dependency setup"
fi

load_local_env

AGFS_BIN="$ROOT_DIR/agfs/build/agfs-server"
if [ "$SKIP_AGFS" -eq 0 ]; then
  if [ "$FORCE_AGFS" -eq 1 ] || [ ! -x "$AGFS_BIN" ]; then
    have make || die "missing make; install make or rerun with --skip-agfs"
    ensure_go_version
    log "building bundled AGFS server"
    make -C "$ROOT_DIR/agfs" build
  else
    log "AGFS server already built at agfs/build/agfs-server"
  fi
else
  log "skipping AGFS build"
fi

setup_claude_integration
setup_openclaw_integration

cat <<EOF

Bootstrap complete.

Next:
  1. Edit the user-editable settings at the top of:
     $ROOT_DIR/setup_env.sh
  2. Load those settings into this shell:
     source "$ROOT_DIR/setup_env.sh"
  3. If you set up Claude, start Claude from a target project:
     cd /path/to/project
     $ROOT_DIR/claude-plugin/bin/contextsniper-claude
  4. If you set up OpenClaw, start OpenClaw from a target project:
     cd /path/to/project
     openclaw chat --local
     Then name the target path in your prompt, because OpenClaw's native shell
     may still start in ~/.openclaw/workspace:
     The target project is /path/to/project. Run cd /path/to/project && <command>.

EOF
