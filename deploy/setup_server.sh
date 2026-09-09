#!/usr/bin/env bash
# =============================================================================
# setup_server.sh - one-shot, IDEMPOTENT provisioning of the PIX recovery VPS.
#
#   Ubuntu 24.04 (Hostinger KVM) - run as root - safe to run again after a change
#
# What it does, in order:
#   1  sanity checks (root, Ubuntu 24.04, files present)
#   2  apt packages
#   3  system user `pixapp` + /opt/pix-recovery
#   4  PostgreSQL role + database with a generated password
#   5  /etc/pix-recovery/env (0600 root:pixapp) with DATABASE_URL merged in
#   6  virtualenv + requirements
#   7  nginx site (http) and reload
#   8  systemd units pix-api / pix-worker
#   9  ufw + fail2ban
#  10  TLS with certbot --nginx (skipped when DNS does not point here yet)
#  11  SSH hardening - ONLY if the public key is already in authorized_keys
#  12  start everything and print a summary + /health
#
# Usage:
#   API_DOMAIN=api.jornadaanjo.cloud LETSENCRYPT_EMAIL=you@example.com \
#     bash /opt/pix-recovery/deploy/setup_server.sh
#
# Escape hatches (env vars): SKIP_TLS=1  SKIP_SSH_HARDENING=1  SKIP_APT=1  FORCE_OS=1
# =============================================================================
set -euo pipefail

# --- variables (override from the environment) --------------------------------
API_DOMAIN="${API_DOMAIN:-api.jornadaanjo.cloud}"
LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-jornadacommeuanjo@outlook.com}"
APP_DIR="${APP_DIR:-/opt/pix-recovery}"
# Env file you uploaded (built from deploy/env.example). Copied to ENV_FILE once.
ENV_SRC="${ENV_SRC:-/root/pix-recovery.env}"
# Public key that MUST already be in /root/.ssh/authorized_keys before SSH is hardened.
PUBKEY="${PUBKEY:-$APP_DIR/kanari-pix.pub}"

APP_USER="${APP_USER:-pixapp}"
DB_NAME="${DB_NAME:-pixrecovery}"
DB_USER="${DB_USER:-pixrecovery}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"
ENV_DIR=/etc/pix-recovery
ENV_FILE="$ENV_DIR/env"
NGINX_SITE=/etc/nginx/sites-available/pix-api.conf
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

STEP=0
step() { STEP=$((STEP + 1)); printf '\n[%02d] %s\n' "$STEP" "$*"; }
info() { printf '     %s\n' "$*"; }
warn() { printf '     AVISO: %s\n' "$*" >&2; }
die()  { printf '\nERRO: %s\n' "$*" >&2; exit 1; }

# -----------------------------------------------------------------------------
step "Verificacoes iniciais"
[ "$(id -u)" -eq 0 ] || die "rode como root:  sudo bash $0"

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  info "sistema: ${PRETTY_NAME:-desconhecido}"
  if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "24.04" ]; then
    if [ "${FORCE_OS:-0}" = "1" ]; then
      warn "esperado Ubuntu 24.04 - seguindo por causa de FORCE_OS=1"
    else
      die "este script foi escrito para Ubuntu 24.04 (use FORCE_OS=1 para ignorar)"
    fi
  fi
else
  [ "${FORCE_OS:-0}" = "1" ] || die "/etc/os-release ausente (use FORCE_OS=1 para ignorar)"
fi

for f in "$SCRIPT_DIR/nginx-pix-api.conf" "$SCRIPT_DIR/pix-api.service" "$SCRIPT_DIR/pix-worker.service"; do
  [ -f "$f" ] || die "arquivo do repositorio nao encontrado: $f (envie o codigo antes com deploy/deploy.sh)"
done
info "dominio: $API_DOMAIN - app: $APP_DIR - usuario: $APP_USER"

# -----------------------------------------------------------------------------
step "Pacotes do sistema (apt)"
if [ "${SKIP_APT:-0}" = "1" ]; then
  info "SKIP_APT=1 - pulando"
else
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  # python3.12 is the default interpreter on 24.04; we only need the venv module.
  apt-get install -y -qq \
    python3.12-venv python3-pip \
    postgresql postgresql-client \
    nginx certbot python3-certbot-nginx \
    ufw fail2ban python3-systemd \
    curl ca-certificates tar openssl >/dev/null
  info "instalados: python3.12-venv postgresql nginx certbot ufw fail2ban"
fi

# -----------------------------------------------------------------------------
step "Usuario do sistema e diretorio da aplicacao"
if id -u "$APP_USER" >/dev/null 2>&1; then
  info "usuario $APP_USER ja existe"
else
  # System account with no shell: it only runs the two services.
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  info "usuario $APP_USER criado"
fi
mkdir -p "$APP_DIR"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
info "diretorio: $APP_DIR"

# -----------------------------------------------------------------------------
step "PostgreSQL: papel e banco"
systemctl enable --now postgresql >/dev/null 2>&1 || true

psql_su() { su - postgres -c "psql -v ON_ERROR_STOP=1 -Atqc \"$1\""; }

role_exists="$(psql_su "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" || true)"
db_exists="$(psql_su "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" || true)"

# The password is only ever generated when the env file does not already carry a
# DATABASE_URL - re-running the script must NOT invalidate the credentials the
# running services are already using.
existing_url=""
if [ -f "$ENV_FILE" ]; then
  existing_url="$(grep -E '^DATABASE_URL=' "$ENV_FILE" | tail -n1 | cut -d= -f2- || true)"
fi

if [ -n "$existing_url" ]; then
  info "DATABASE_URL ja existe em $ENV_FILE - mantendo a senha atual"
  [ -n "$role_exists" ] || warn "papel $DB_USER nao existe no Postgres mas ha DATABASE_URL no env: verifique manualmente"
  NEW_DB_URL=""
else
  # Hex only: no quoting/escaping surprises in SQL, in the URL or in the env file.
  DB_PASSWORD="$(openssl rand -hex 24)"
  if [ -n "$role_exists" ]; then
    psql_su "ALTER ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASSWORD}'" >/dev/null
    info "papel $DB_USER ja existia - senha redefinida"
  else
    psql_su "CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASSWORD}'" >/dev/null
    info "papel $DB_USER criado"
  fi
  NEW_DB_URL="postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
  unset DB_PASSWORD
fi

if [ -n "$db_exists" ]; then
  info "banco $DB_NAME ja existe"
else
  su - postgres -c "createdb -O ${DB_USER} ${DB_NAME}"
  info "banco $DB_NAME criado (dono: $DB_USER)"
fi

# -----------------------------------------------------------------------------
step "Arquivo de segredos $ENV_FILE"
mkdir -p "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$ENV_SRC" ]; then
    install -m 0600 -o root -g root "$ENV_SRC" "$ENV_FILE"
    info "copiado de $ENV_SRC"
    # The uploaded copy holds META_ACCESS_TOKEN, META_APP_SECRET,
    # KIRVANO_WEBHOOK_TOKEN and PANEL_PASSWORD at whatever mode scp created (0644 with
    # a default umask) and never gets the 0600 root:pixapp treatment applied below.
    # Leaving it in /root would put the full secret set into every future backup or
    # support snapshot TWICE. It has been consumed, so it goes.
    if [ "$ENV_SRC" != "$ENV_FILE" ]; then
      chmod 0600 "$ENV_SRC" 2>/dev/null || true
      shred -u "$ENV_SRC" 2>/dev/null || rm -f "$ENV_SRC"
      info "$ENV_SRC removido (os segredos agora vivem so em $ENV_FILE)"
    fi
  else
    ( umask 077; : > "$ENV_FILE" )
    warn "$ENV_SRC nao encontrado - criei $ENV_FILE VAZIO; preencha antes de usar em producao"
  fi
else
  info "ja existe - preservado (edite com: nano $ENV_FILE)"
fi

# Merge one KEY=value into the env file without ever echoing the value.
set_env_var() {
  local key="$1" value="$2" file="$3"
  if grep -qE "^${key}=" "$file"; then
    # `|` as the sed delimiter: the URL contains `/` and `:` but never `|`.
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

if [ -n "${NEW_DB_URL:-}" ]; then
  ( umask 077; set_env_var DATABASE_URL "$NEW_DB_URL" "$ENV_FILE" )
  info "DATABASE_URL gravado (o valor nunca e impresso)"
fi
set_env_var API_DOMAIN "$API_DOMAIN" "$ENV_FILE"
grep -qE '^APP_ENV=' "$ENV_FILE" || printf 'APP_ENV=prod\n' >> "$ENV_FILE"

# SPEC: 0600 root:pixapp. systemd reads EnvironmentFile as root before dropping
# privileges, so the app never needs read access - the group is only there so a
# future 0640 can be granted without touching ownership.
chown root:"$APP_USER" "$ENV_FILE"
chmod 0600 "$ENV_FILE"
chown root:"$APP_USER" "$ENV_DIR"
chmod 0750 "$ENV_DIR"
info "permissoes: $(stat -c '%a %U:%G' "$ENV_FILE") $ENV_FILE"

if ! grep -qE '^PANEL_PASSWORD=.+' "$ENV_FILE"; then
  warn "PANEL_PASSWORD vazio: o painel respondera 503 ate voce preencher"
fi

# -----------------------------------------------------------------------------
step "Ambiente virtual Python e dependencias"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/.venv"
  info "venv criada em $APP_DIR/.venv"
else
  info "venv ja existe"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip wheel
if [ -f "$APP_DIR/requirements.txt" ]; then
  "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
  info "requirements.txt instalado"
else
  warn "$APP_DIR/requirements.txt nao encontrado - envie o codigo com deploy/deploy.sh e rode de novo"
fi
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

# -----------------------------------------------------------------------------
step "nginx"
# NEVER overwrite a vhost certbot already owns. `certbot --nginx` EDITS this file in
# place (it adds `listen 443 ssl`, the certificate paths and the port-80 redirect), so
# rewriting it on a re-run would leave the site with NO port-443 server block at all -
# both webhooks would go dark, and README section 9.2 tells the operator to re-run this
# very script after Hostinger's "Reset SSH". The TLS step below re-applies certbot when
# the file is missing those directives.
if grep -q 'listen 443' "$NGINX_SITE" 2>/dev/null; then
  info "$NGINX_SITE ja tem TLS (editado pelo certbot) - preservado"
  NGINX_HAS_TLS=1
else
  sed "s|api\.jornadaanjo\.cloud|${API_DOMAIN}|g" "$SCRIPT_DIR/nginx-pix-api.conf" > "$NGINX_SITE"
  info "site http instalado em $NGINX_SITE"
  NGINX_HAS_TLS=0
fi
ln -sfn "$NGINX_SITE" /etc/nginx/sites-enabled/pix-api.conf
# The default site would answer for our domain too (first block wins by port).
rm -f /etc/nginx/sites-enabled/default
if nginx -t >/dev/null 2>&1; then
  systemctl enable --now nginx >/dev/null 2>&1 || true
  systemctl reload nginx
  info "site pix-api.conf ativo e nginx recarregado"
else
  nginx -t || true
  die "configuracao do nginx invalida (veja a saida acima)"
fi

# -----------------------------------------------------------------------------
step "Unidades systemd"
for unit in pix-api pix-worker pix-retention; do
  sed -e "s|/opt/pix-recovery|${APP_DIR}|g" \
      -e "s|^User=pixapp$|User=${APP_USER}|" \
      -e "s|^Group=pixapp$|Group=${APP_USER}|" \
      "$SCRIPT_DIR/${unit}.service" > "/etc/systemd/system/${unit}.service"
  info "instalada /etc/systemd/system/${unit}.service"
done
# The purge is a timer, not a long-running service: it only runs once a day.
install -m 0644 "$SCRIPT_DIR/pix-retention.timer" /etc/systemd/system/pix-retention.timer
systemctl daemon-reload
systemctl enable pix-api pix-worker >/dev/null 2>&1
# --now: the timer must be armed from this run, otherwise the 12-month limit the
# public privacy page promises would only start after the next reboot.
systemctl enable --now pix-retention.timer >/dev/null 2>&1
info "pix-api, pix-worker e o timer pix-retention habilitados no boot"

# -----------------------------------------------------------------------------
step "Firewall (ufw) e fail2ban"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp  >/dev/null
ufw allow 443/tcp >/dev/null
# --force: no interactive "proceed? (y|n)" - and OpenSSH is already allowed above.
ufw --force enable >/dev/null
info "ufw: $(ufw status | head -n1) - 22, 80 e 443 liberados"

cat > /etc/fail2ban/jail.d/sshd.local <<'JAIL'
# Managed by deploy/setup_server.sh
[sshd]
enabled = true
mode = aggressive
port = ssh
# Ubuntu 24.04 keeps sshd logs in the journal, not in /var/log/auth.log.
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
JAIL
systemctl enable fail2ban >/dev/null 2>&1 || true
# fail2ban lendo o journal precisa de python3-systemd (instalado acima). Se ainda assim
# nao subir, o deploy NAO pode parar por causa disso: o resto do servidor e mais importante.
if systemctl restart fail2ban; then
  info "fail2ban: jail sshd ativa (5 tentativas / 10 min = ban de 1 h)"
else
  warn "fail2ban nao subiu: veja 'journalctl -u fail2ban -n 30'. O ufw continua ativo."
fi

# -----------------------------------------------------------------------------
step "Certificado TLS (Let's Encrypt)"
if [ "${SKIP_TLS:-0}" = "1" ]; then
  info "SKIP_TLS=1 - pulando"
elif [ -d "/etc/letsencrypt/live/$API_DOMAIN" ]; then
  if [ "${NGINX_HAS_TLS:-0}" = "1" ]; then
    info "certificado para $API_DOMAIN ja existe - renovacao automatica pelo timer do certbot"
  else
    # The certificate exists but the vhost does not reference it (fresh nginx config,
    # or someone removed the site). Re-install it: --keep-until-expiring does NOT ask
    # Let's Encrypt for a new certificate, so no rate limit is burned.
    warn "certificado existe mas o vhost esta sem TLS - reinstalando no nginx"
    certbot --nginx -d "$API_DOMAIN" --non-interactive --agree-tos \
      -m "$LETSENCRYPT_EMAIL" --redirect --keep-until-expiring --reinstall
    nginx -t && systemctl reload nginx
    info "TLS reinstalado para $API_DOMAIN"
  fi
else
  resolved="$(getent ahostsv4 "$API_DOMAIN" 2>/dev/null | awk 'NR==1{print $1}' || true)"
  public_ip="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
  if [ -n "$resolved" ] && { [ -z "$public_ip" ] || [ "$resolved" = "$public_ip" ]; }; then
    certbot --nginx -d "$API_DOMAIN" --non-interactive --agree-tos \
      -m "$LETSENCRYPT_EMAIL" --redirect
    info "TLS emitido para $API_DOMAIN"
  else
    # Asking Let's Encrypt before DNS is right burns the failure rate limit.
    warn "DNS de $API_DOMAIN aponta para '${resolved:-nada}' e este servidor e '${public_ip:-?}'."
    warn "Ajuste o registro A no hPanel e rode depois:"
    warn "  certbot --nginx -d $API_DOMAIN --non-interactive --agree-tos -m $LETSENCRYPT_EMAIL --redirect"
  fi
fi

# -----------------------------------------------------------------------------
step "Endurecimento do SSH"
# ORDER MATTERS: password login is only turned off after PROVING that the key
# which replaces it already works - otherwise a typo locks everyone out of the box.
if [ "${SKIP_SSH_HARDENING:-0}" = "1" ]; then
  info "SKIP_SSH_HARDENING=1 - pulando"
elif [ ! -f "$PUBKEY" ]; then
  warn "chave publica nao encontrada em $PUBKEY - SSH NAO foi endurecido"
  warn "envie a .pub e rode de novo, ou defina PUBKEY=/caminho/da/chave.pub"
elif [ ! -f /root/.ssh/authorized_keys ]; then
  warn "/root/.ssh/authorized_keys nao existe - SSH NAO foi endurecido"
else
  # Compare the key material only (field 2): comments and options differ freely.
  key_body="$(awk '{print $2}' "$PUBKEY" | head -n1)"
  if [ -n "$key_body" ] && grep -qF "$key_body" /root/.ssh/authorized_keys; then
    cat > /etc/ssh/sshd_config.d/00-hardening.conf <<'SSHD'
# Managed by deploy/setup_server.sh - keys only.
# NOTE: Hostinger hPanel > "Reset SSH" rewrites sshd_config and drops this file.
# If that button is ever pressed, re-run setup_server.sh.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
SSHD
    if sshd -t; then
      # A unidade se chama ssh no Ubuntu; sshd e o nome em outras distros.
      systemctl restart ssh || systemctl restart sshd
      info "SSH endurecido: somente chave (a sessao atual continua aberta)"
    else
      rm -f /etc/ssh/sshd_config.d/00-hardening.conf
      die "sshd -t falhou: configuracao revertida, SSH intacto"
    fi
  else
    warn "a chave $PUBKEY NAO esta em /root/.ssh/authorized_keys - SSH NAO foi endurecido"
    warn "adicione-a pelo hPanel (Chaves SSH) ou com ssh-copy-id e rode de novo"
  fi
fi

# -----------------------------------------------------------------------------
step "Subindo os servicos"
systemctl restart pix-api pix-worker || true
sleep 3
for unit in pix-api pix-worker; do
  if systemctl is-active --quiet "$unit"; then
    info "$unit: ativo"
  else
    warn "$unit NAO subiu - veja: journalctl -u $unit -n 50 --no-pager"
  fi
done

# -----------------------------------------------------------------------------
step "Resumo"
cat <<SUMMARY
     dominio ........ https://$API_DOMAIN
     codigo ......... $APP_DIR
     segredos ....... $ENV_FILE  (0600 root:$APP_USER)
     banco .......... $DB_NAME (papel $DB_USER em $DB_HOST:$DB_PORT)
     servicos ....... pix-api (127.0.0.1:8000) e pix-worker
     logs ........... journalctl -u pix-api -f  /  journalctl -u pix-worker -f
     painel ......... https://$API_DOMAIN/painel   (usuario/senha do env)
     webhooks ....... https://$API_DOMAIN/webhooks/kirvano  /  /webhooks/meta
SUMMARY

echo
info "checando /health localmente:"
curl -fsS --max-time 10 http://127.0.0.1:8000/health || warn "a API nao respondeu em 127.0.0.1:8000"
echo
info "checando /health pelo dominio:"
curl -fsS --max-time 10 "https://$API_DOMAIN/health" || warn "https://$API_DOMAIN/health nao respondeu (DNS/TLS/firewall do hPanel?)"
echo

# -----------------------------------------------------------------------------
step "Verificacao final (deploy/check.sh)"
# A broken TLS state must be REPORTED, not silent: check.sh proves DNS, the three
# ports, the http->https redirect, /health and the certificate. It never exits
# non-zero here - the provisioning itself already succeeded.
if [ -x "$SCRIPT_DIR/check.sh" ]; then
  API_DOMAIN="$API_DOMAIN" bash "$SCRIPT_DIR/check.sh" || \
    warn "check.sh apontou problemas acima - resolva antes de configurar os webhooks"
else
  warn "$SCRIPT_DIR/check.sh nao encontrado - rode a verificacao manualmente"
fi

printf '\nConcluido.\n'
