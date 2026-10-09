#!/usr/bin/env bash
# =============================================================================
# run_remote.sh - roda um script do projeto NO SERVIDOR, com o mesmo usuario
# (pixapp) e o mesmo ambiente (/etc/pix-recovery/env) dos servicos.
#
#   bash deploy/run_remote.sh scripts.reprocessar_carrinhos
#   bash deploy/run_remote.sh scripts.reprocessar_carrinhos --enviar
#
# Mesmas variaveis do deploy.sh (VPS_HOST, VPS_USER, SSH_PORT, SSH_KEY_PATH,
# APP_DIR). O codigo precisa estar no servidor: rode o deploy.sh antes.
# Nenhum segredo passa pela linha de comando: o systemd le o arquivo de
# ambiente la no servidor, como faz para o pix-api e o pix-worker.
# =============================================================================
set -euo pipefail

# Git Bash reescreve argumentos que parecem caminhos POSIX (ver deploy.sh).
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_ENV="${LOCAL_ENV:-$ROOT/secrets.env}"

die() { printf 'ERRO: %s\n' "$*" >&2; exit 1; }

# Le UMA chave nao-secreta do secrets.env (igual ao deploy.sh, sem `source`).
read_env_key() {
  [ -f "$LOCAL_ENV" ] || return 0
  sed -n "s/^$1=//p" "$LOCAL_ENV" | tail -n1 | tr -d '"'"'"'\r'
}

VPS_HOST="${VPS_HOST:-$(read_env_key VPS_HOST)}"
VPS_HOST="${VPS_HOST:-179.199.147.30}"
VPS_USER="${VPS_USER:-$(read_env_key VPS_USER)}"
VPS_USER="${VPS_USER:-root}"
SSH_PORT="${SSH_PORT:-$(read_env_key SSH_PORT)}"
SSH_PORT="${SSH_PORT:-22}"
SSH_KEY_PATH="${SSH_KEY_PATH:-$(read_env_key SSH_KEY_PATH)}"
SSH_KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/id_ed25519_pix}"
SSH_KEY_PATH="${SSH_KEY_PATH/#\~/$HOME}"
APP_DIR="${APP_DIR:-$(read_env_key APP_DIR)}"
APP_DIR="${APP_DIR:-/opt/pix-recovery}"
APP_USER="${APP_USER:-pixapp}"
APP_ENV_FILE="${APP_ENV_FILE:-/etc/pix-recovery/env}"

[ $# -ge 1 ] || die "uso: bash deploy/run_remote.sh scripts.<nome> [opcoes]"
MODULE="$1"; shift
# Lista curta e sem aspas: o comando vai para um shell remoto.
[[ "$MODULE" =~ ^scripts\.[a-z_]+$ ]] || die "modulo invalido: $MODULE (ex.: scripts.reprocessar_carrinhos)"
ARGS=""
for arg in "$@"; do
  [[ "$arg" =~ ^[A-Za-z0-9=._-]+$ ]] || die "argumento invalido: $arg"
  ARGS="$ARGS $arg"
done

[ -f "$SSH_KEY_PATH" ] || die "chave SSH nao encontrada: $SSH_KEY_PATH (defina SSH_KEY_PATH)"
SSH_OPTS=(-i "$SSH_KEY_PATH" -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)

exec ssh "${SSH_OPTS[@]}" "$VPS_USER@$VPS_HOST" \
  "systemd-run --quiet --wait --pipe --collect \
     -p User=$APP_USER -p Group=$APP_USER \
     -p EnvironmentFile=$APP_ENV_FILE -p WorkingDirectory=$APP_DIR \
     -p Environment=PYTHONUNBUFFERED=1 -p Environment=PYTHONDONTWRITEBYTECODE=1 \
     $APP_DIR/.venv/bin/python -m $MODULE$ARGS"
