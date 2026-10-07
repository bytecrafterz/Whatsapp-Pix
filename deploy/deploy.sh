#!/usr/bin/env bash
# =============================================================================
# deploy.sh - envia o codigo para o VPS e reinicia os servicos.
#
# Feito para rodar no GIT BASH do Windows (tambem funciona em Linux/macOS):
#   bash deploy/deploy.sh
#   bash deploy/deploy.sh --setup      # 1a vez: envia e roda deploy/setup_server.sh no servidor
#
# Variaveis (env, ou linhas VPS_*/SSH_KEY_PATH do secrets.env local):
#   VPS_HOST=179.199.147.30   VPS_USER=root   SSH_PORT=22
#   SSH_KEY_PATH=~/.ssh/id_ed25519_pix        APP_DIR=/opt/pix-recovery
#
# Nunca envia: .venv, secrets.env, .git, dev.db, __pycache__, caches, *.pdf.
# =============================================================================
set -euo pipefail

# Git Bash reescreve argumentos que parecem caminhos POSIX (/opt/... vira
# C:/Program Files/Git/opt/...). Estas duas variaveis desligam essa "ajuda",
# senao todo caminho remoto passado ao ssh/scp chega corrompido.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_ENV="${LOCAL_ENV:-$ROOT/secrets.env}"

info() { printf '==> %s\n' "$*"; }
warn() { printf '    AVISO: %s\n' "$*" >&2; }
die()  { printf 'ERRO: %s\n' "$*" >&2; exit 1; }

# Le UMA chave nao-secreta do secrets.env (VPS_HOST, SSH_KEY_PATH, ...).
# Nao usamos `source`: assim nenhum token da Meta/Kirvano entra no ambiente
# deste processo nem vaza para a linha de comando do ssh.
read_env_key() {
  [ -f "$LOCAL_ENV" ] || return 0
  sed -n "s/^$1=//p" "$LOCAL_ENV" | tail -n1 | tr -d '"'"'"'\r'
}

# --- variaveis ----------------------------------------------------------------
VPS_HOST="${VPS_HOST:-$(read_env_key VPS_HOST)}"
VPS_HOST="${VPS_HOST:-179.199.147.30}"
VPS_USER="${VPS_USER:-$(read_env_key VPS_USER)}"
VPS_USER="${VPS_USER:-root}"
SSH_PORT="${SSH_PORT:-$(read_env_key SSH_PORT)}"
SSH_PORT="${SSH_PORT:-22}"
SSH_KEY_PATH="${SSH_KEY_PATH:-$(read_env_key SSH_KEY_PATH)}"
SSH_KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/id_ed25519_pix}"
SSH_KEY_PATH="${SSH_KEY_PATH/#\~/$HOME}"   # "~/..." vindo do env nao expande sozinho
APP_DIR="${APP_DIR:-$(read_env_key APP_DIR)}"
APP_DIR="${APP_DIR:-/opt/pix-recovery}"
API_DOMAIN="${API_DOMAIN:-$(read_env_key API_DOMAIN)}"
API_DOMAIN="${API_DOMAIN:-api.jornadaanjo.cloud}"
LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-$(read_env_key LETSENCRYPT_EMAIL)}"
APP_USER="${APP_USER:-pixapp}"

RUN_SETUP=0
WITH_TESTS=0
RESTART=1
for arg in "$@"; do
  case "$arg" in
    --setup)      RUN_SETUP=1 ;;
    --with-tests) WITH_TESTS=1 ;;
    --no-restart) RESTART=0 ;;
    -h|--help)    sed -n '3,13p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)            die "opcao desconhecida: $arg" ;;
  esac
done

[ -f "$SSH_KEY_PATH" ] || die "chave SSH nao encontrada: $SSH_KEY_PATH (defina SSH_KEY_PATH)"

SSH_OPTS=(-i "$SSH_KEY_PATH" -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
SCP_OPTS=(-i "$SSH_KEY_PATH" -P "$SSH_PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
TARGET="$VPS_USER@$VPS_HOST"

info "servidor: $TARGET:$SSH_PORT  -  destino: $APP_DIR  -  chave: $SSH_KEY_PATH"

# --- 1. conectividade ---------------------------------------------------------
info "testando SSH..."
ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$TARGET" 'echo "conectado em $(hostname)"' \
  || die "SSH falhou. Confira a chave, a porta 22 no firewall do hPanel e o IP."

# --- 2. pacote ----------------------------------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
TARBALL="${TMPDIR:-/tmp}/pix-recovery-$STAMP.tgz"
EXCLUDES=(
  --exclude=./.venv
  --exclude=./.git
  --exclude=./secrets.env
  --exclude=./dev.db
  --exclude=./dev.db-wal
  --exclude=./dev.db-shm
  --exclude='./*.db'
  --exclude='./*.pdf'
  --exclude='*/__pycache__'
  --exclude=__pycache__
  --exclude=./.pytest_cache
  --exclude=./.ruff_cache
  --exclude=./.mypy_cache
  --exclude=./node_modules
  --exclude=./.idea
  --exclude=./.vscode
)
# SPEC: os testes ficam na maquina de desenvolvimento. --with-tests envia mesmo assim
# (util para rodar `uv run pytest` no servidor uma unica vez).
[ "$WITH_TESTS" -eq 1 ] || EXCLUDES+=(--exclude=./tests)

info "empacotando o codigo (sem .venv, .git, secrets.env, dev.db, __pycache__)..."
tar -czf "$TARBALL" -C "$ROOT" "${EXCLUDES[@]}" .
info "pacote: $TARBALL ($(du -h "$TARBALL" 2>/dev/null | cut -f1))"

REMOTE_TAR="/tmp/pix-recovery-$STAMP.tgz"
info "enviando..."
scp "${SCP_OPTS[@]}" "$TARBALL" "$TARGET:$REMOTE_TAR"
rm -f "$TARBALL"

# --- 3. instalar no servidor --------------------------------------------------
info "instalando em $APP_DIR..."
ssh "${SSH_OPTS[@]}" "$TARGET" \
  "APP_DIR='$APP_DIR' APP_USER='$APP_USER' REMOTE_TAR='$REMOTE_TAR' RUN_SETUP='$RUN_SETUP' \
   RESTART='$RESTART' API_DOMAIN='$API_DOMAIN' LETSENCRYPT_EMAIL='$LETSENCRYPT_EMAIL' bash -s" <<'REMOTE'
set -euo pipefail
mkdir -p "$APP_DIR"
# --no-same-owner: os arquivos chegam com o uid do Windows/dev; o chown abaixo corrige.
tar -xzf "$REMOTE_TAR" -C "$APP_DIR" --no-same-owner
rm -f "$REMOTE_TAR"
id -u "$APP_USER" >/dev/null 2>&1 && chown -R "$APP_USER":"$APP_USER" "$APP_DIR" || true
chmod +x "$APP_DIR"/deploy/*.sh 2>/dev/null || true
echo "    codigo atualizado"

if [ "$RUN_SETUP" = "1" ]; then
  echo "    rodando setup_server.sh..."
  export API_DOMAIN APP_DIR LETSENCRYPT_EMAIL
  bash "$APP_DIR/deploy/setup_server.sh"
  exit 0
fi

if [ -x "$APP_DIR/.venv/bin/pip" ]; then
  "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
  echo "    dependencias em dia"
else
  echo "    AVISO: $APP_DIR/.venv nao existe - rode: bash deploy/deploy.sh --setup"
fi

# `systemctl cat` falha quando a unidade nao existe; `list-unit-files` devolve 0 mesmo
# sem encontrar nada, entao nao serve para decidir se o setup ja rodou.
if [ "$RESTART" = "1" ] && systemctl cat pix-api.service >/dev/null 2>&1; then
  systemctl restart pix-api pix-worker
  sleep 3
  for unit in pix-api pix-worker; do
    state="$(systemctl is-active "$unit" || true)"
    echo "    $unit: $state"
    if [ "$state" != "active" ]; then
      journalctl -u "$unit" -n 20 --no-pager || true
    fi
  done
  # /health responde 200 mesmo "degraded" (worker sem batida ainda); mostramos o JSON.
  # A API leva alguns segundos para abrir a porta depois do restart: tenta por ate 30 s
  # antes de dizer SEM RESPOSTA (antes um unico teste aos 3 s dava alarme falso).
  echo -n "    /health: "
  health=""
  for _ in $(seq 1 15); do
    health="$(curl -fsS --max-time 5 http://127.0.0.1:8000/health 2>/dev/null)" && break
    health=""
    sleep 2
  done
  echo "${health:-SEM RESPOSTA}"
else
  echo "    servicos nao reiniciados"
fi
REMOTE

info "pronto. Verifique de fora com: bash deploy/check.sh"
info "logs: ssh -i $SSH_KEY_PATH $TARGET 'journalctl -u pix-api -u pix-worker -n 50 --no-pager'"
