#!/usr/bin/env bash
# =============================================================================
# check.sh - verificacao externa (roda do Windows/Git Bash ou de qualquer lugar).
#
#   bash deploy/check.sh
#   API_DOMAIN=api.jornadaanjo.cloud VPS_HOST=179.199.147.30 bash deploy/check.sh
#
# Checa, nesta ordem:
#   1  DNS: API_DOMAIN resolve para VPS_HOST?
#   2  portas 22, 80 e 443 abertas (ufw + firewall do hPanel)
#   3  http:// redireciona para https://
#   4  GET https://API_DOMAIN/health responde 200 e o worker esta batendo
#   5  validade do certificado TLS (se openssl estiver disponivel)
#
# Sai com codigo 0 quando tudo passa, ou com o numero de falhas.
# =============================================================================
set -uo pipefail   # sem -e: queremos rodar TODAS as checagens e somar as falhas

export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_ENV="${LOCAL_ENV:-$ROOT/secrets.env}"

read_env_key() {
  [ -f "$LOCAL_ENV" ] || return 0
  sed -n "s/^$1=//p" "$LOCAL_ENV" | tail -n1 | tr -d '"'"'"'\r'
}

API_DOMAIN="${API_DOMAIN:-$(read_env_key API_DOMAIN)}"
API_DOMAIN="${API_DOMAIN:-api.jornadaanjo.cloud}"
VPS_HOST="${VPS_HOST:-$(read_env_key VPS_HOST)}"
VPS_HOST="${VPS_HOST:-179.199.147.30}"

FAILS=0
ok()   { printf '  [ok]    %s\n' "$*"; }
bad()  { printf '  [FALHA] %s\n' "$*"; FAILS=$((FAILS + 1)); }
skip() { printf '  [--]    %s\n' "$*"; }
head_() { printf '\n%s\n' "$*"; }

printf 'Checando %s (%s)\n' "$API_DOMAIN" "$VPS_HOST"

# --- 1. DNS -------------------------------------------------------------------
head_ "1) DNS"
resolved=""
if command -v getent >/dev/null 2>&1; then
  resolved="$(getent ahostsv4 "$API_DOMAIN" 2>/dev/null | awk 'NR==1{print $1}')"
fi
if [ -z "$resolved" ] && command -v nslookup >/dev/null 2>&1; then
  # Git Bash no Windows nao tem getent; nslookup existe sempre.
  resolved="$(nslookup "$API_DOMAIN" 2>/dev/null | awk '/^Address: /{print $2; exit}')"
fi
if [ -z "$resolved" ]; then
  bad "$API_DOMAIN nao resolve (registro A ausente ou DNS ainda propagando)"
elif [ "$resolved" = "$VPS_HOST" ]; then
  ok "$API_DOMAIN -> $resolved"
else
  bad "$API_DOMAIN -> $resolved (esperado $VPS_HOST) - corrija o registro A no hPanel"
fi

# --- 2. portas ----------------------------------------------------------------
head_ "2) Portas TCP no $VPS_HOST"
port_open() {
  # /dev/tcp e um recurso do proprio bash: funciona no Git Bash sem instalar nc.
  # Porta filtrada por firewall nao devolve RST, entao sem `timeout` a conexao
  # ficaria pendurada ate o TCP desistir (~2 min).
  if command -v timeout >/dev/null 2>&1; then
    timeout 6 bash -c "exec 3<>/dev/tcp/$VPS_HOST/$1" >/dev/null 2>&1
  else
    ( exec 3<>"/dev/tcp/$VPS_HOST/$1" ) >/dev/null 2>&1
  fi
}
for port in 22 80 443; do
  if port_open "$port"; then
    ok "porta $port aberta"
  else
    bad "porta $port fechada - libere no hPanel > Firewall (o ufw sozinho nao basta)"
  fi
done

# --- 3. redirecionamento http -> https ----------------------------------------
head_ "3) Redirecionamento http -> https"
redirect="$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 10 \
  "http://$API_DOMAIN/health" 2>/dev/null)"
case "$redirect" in
  30[12]*https://*) ok "http responde $redirect" ;;
  200*)             bad "http respondeu 200 sem redirecionar - certbot --nginx --redirect nao rodou" ;;
  000*)             bad "sem resposta em http://$API_DOMAIN (porta 80 ou DNS)" ;;
  *)                bad "resposta inesperada em http: $redirect" ;;
esac

# --- 4. /health ---------------------------------------------------------------
head_ "4) https://$API_DOMAIN/health"
body="$(curl -s --max-time 15 -w '\n%{http_code}' "https://$API_DOMAIN/health" 2>/dev/null)"
code="$(printf '%s' "$body" | tail -n1)"
json="$(printf '%s' "$body" | sed '$d')"
if [ "$code" = "200" ]; then
  ok "HTTP 200"
  printf '          %s\n' "$json"
  case "$json" in
    *'"db":"ok"'*)     ok "banco de dados ok" ;;
    *)                 bad "banco de dados com problema (veja journalctl -u pix-api)" ;;
  esac
  case "$json" in
    *'"worker":"ok"'*) ok "worker batendo (heartbeat recente)" ;;
    *)                 bad "worker parado ou atrasado (systemctl status pix-worker)" ;;
  esac
elif [ -z "$code" ] || [ "$code" = "000" ]; then
  bad "sem resposta em https (TLS ausente? porta 443 fechada?)"
else
  bad "HTTP $code em /health"
fi

# --- 5. certificado -----------------------------------------------------------
head_ "5) Certificado TLS"
if command -v openssl >/dev/null 2>&1; then
  expiry="$(echo | openssl s_client -servername "$API_DOMAIN" -connect "$API_DOMAIN:443" 2>/dev/null \
    | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)"
  if [ -n "$expiry" ]; then
    ok "valido ate $expiry (o timer do certbot renova sozinho)"
  else
    bad "nao consegui ler o certificado de $API_DOMAIN"
  fi
else
  skip "openssl indisponivel - pulei a checagem do certificado"
fi

# --- resumo -------------------------------------------------------------------
printf '\n'
if [ "$FAILS" -eq 0 ]; then
  printf 'Tudo certo.\n'
else
  printf '%d checagem(ns) falharam. Veja o runbook: README.md\n' "$FAILS"
fi
exit "$FAILS"
