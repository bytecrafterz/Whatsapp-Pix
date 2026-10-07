# Recuperação de PIX pelo WhatsApp — Jornada com Meu Anjo

Checkout da Kirvano → o cliente gera um PIX → webhook → depois de um tempo configurável
(padrão 10 minutos), se o pedido continuar **não pago**, **não expirado** e o cliente **não**
tiver pedido para sair, o sistema envia **uma única** mensagem de modelo pelo WhatsApp Cloud
API com o link de uma página que mostra o PIX. Se o pedido for pago, expirar ou for recusado
antes da hora, nada é enviado.

Este README é o runbook de operação. Os contratos técnicos estão em
[`docs/SPEC.md`](docs/SPEC.md) (produto) e [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
(código). O passo a passo de acesso às contas está em [`CHECKLIST.md`](CHECKLIST.md) e os
campos exatos do modelo em [`docs/TEMPLATE.md`](docs/TEMPLATE.md).

---

## 1. Rodar na sua máquina (Windows)

Python 3.12 com [uv](https://docs.astral.sh/uv/):

```bash
uv venv .venv
uv pip install -r requirements.txt

uv run pytest -q                 # a suíte inteira precisa passar
uv run ruff check app tests      # lint

uv run uvicorn app.main:app --reload --port 8000   # API   → http://127.0.0.1:8000
uv run python -m app.worker                        # worker (--once roda um ciclo só)
```

A configuração vem **só do ambiente** — nenhum arquivo `.env` é lido sozinho. No PowerShell,
antes de rodar:

```powershell
Get-Content secrets.env | Where-Object { $_ -match '^[A-Z]' } | ForEach-Object {
  $k,$v = $_ -split '=',2 ; [Environment]::SetEnvironmentVariable($k, $v)
}
```

Sem `DATABASE_URL`, o banco local é o SQLite `dev.db` — apague o arquivo para recomeçar do
zero. Endereços úteis em desenvolvimento: `/health`, `/painel`, `/p/<token>`, `/privacidade`.

Para testar o fluxo inteiro sem gastar um PIX de verdade:

```bash
uv run python scripts/simulate_kirvano.py                                   # PIX gerado
uv run python scripts/simulate_kirvano.py --event SALE_APPROVED --sale-id ABCD1234
uv run python scripts/simulate_kirvano.py --event PIX_EXPIRED  --sale-id ABCD1234
```

O corpo enviado é uma cópia fiel do webhook real (inclusive CPF e cookies, de propósito:
assim você confirma que o servidor descarta os dois).

---

## 2. Deploy

Pré-requisitos: chave SSH criada (`~/.ssh/id_ed25519_pix`), registro DNS `api` apontando para
o IP do VPS e as portas 22/80/443 liberadas no **firewall do hPanel** (veja a seção 9).

### Primeira vez

```bash
# 1. Envie o arquivo de segredos (feito a partir de deploy/env.example, já preenchido)
scp -i ~/.ssh/id_ed25519_pix meu-env.txt root@179.199.147.30:/root/pix-recovery.env

# 2. Envie o código e provisione o servidor inteiro (idempotente, pode repetir)
bash deploy/deploy.sh --setup
```

`deploy/setup_server.sh` instala pacotes, cria o usuário `pixapp`, o banco `pixrecovery` com
senha gerada (gravada em `/etc/pix-recovery/env` como `DATABASE_URL`, nunca impressa), a
virtualenv, o site do nginx, o TLS pelo certbot, o `ufw`, o `fail2ban`, as unidades do
systemd (`pix-api`, `pix-worker` e o timer diário `pix-retention`) e — **somente se a chave
pública já estiver em `/root/.ssh/authorized_keys`** — o endurecimento do SSH. Se a chave não
estiver lá, ele avisa e **não** mexe no SSH: nunca dá para se trancar do lado de fora. No fim
ele roda `deploy/check.sh` sozinho, então um TLS quebrado aparece na tela em vez de ficar
silencioso.

> **O arquivo enviado no passo 1 é consumido.** Depois de copiá-lo para
> `/etc/pix-recovery/env` (0600 root:pixapp), o script **apaga** `/root/pix-recovery.env`:
> ele chega com a permissão que o `scp` criou (0644) e guardaria uma segunda cópia de
> `META_ACCESS_TOKEN`, `META_APP_SECRET`, `KIRVANO_WEBHOOK_TOKEN` e `PANEL_PASSWORD` em todo
> backup de `/root`. Numa reinstalação do zero, envie-o de novo. Re-executar o script com o
> `/etc/pix-recovery/env` já existente **não** mexe nele (edite com `nano`).

Re-executar o script é seguro para o nginx: se o `certbot` já editou o vhost (linha
`listen 443`), o arquivo é **preservado** em vez de reescrito — antes, uma segunda execução
derrubava o bloco HTTPS e os dois webhooks paravam de responder.

### Atualizações do dia a dia

```bash
bash deploy/deploy.sh          # envia o código, atualiza dependências, reinicia os 2 serviços
bash deploy/check.sh           # DNS, portas 22/80/443, redirect http→https, /health, certificado
```

O pacote enviado **exclui** `.venv`, `secrets.env`, `.git`, `dev.db`, `__pycache__`, caches e
PDFs. Os testes ficam de fora por padrão (`--with-tests` envia).

Variáveis aceitas pelos dois scripts (ou lidas do `secrets.env` local): `VPS_HOST`,
`VPS_USER`, `SSH_PORT`, `SSH_KEY_PATH`, `APP_DIR`, `API_DOMAIN`, `LETSENCRYPT_EMAIL`.

---

## 3. Checklist do primeiro dia

1. `bash deploy/check.sh` → tudo verde (DNS, 22/80/443, `/health` com `"db":"ok"` e
   `"worker":"ok"`).
2. Abra `https://api.jornadaanjo.cloud/painel` e faça login (usuário/senha do env). Confira em
   **Configurações**: ativado, 10 minutos, horário silencioso 22:00–08:00, limite diário 250.
3. Modelo aprovado: `uv run python scripts/check_template.py` → `status=APPROVED` e
   `categoria=UTILITY`.
4. Inscreva o app nos webhooks da WABA: `uv run python scripts/subscribe_app.py`
   (depois `--list` para conferir).
5. Webhook da Meta verificado no painel de apps e app em **Live**; webhook da Kirvano criado
   com a URL `https://api.jornadaanjo.cloud/webhooks/kirvano`, com o token, para os eventos
   **PIX gerado, Compra aprovada, PIX expirado**.
6. Envio de teste para o seu próprio número:
   `uv run python scripts/send_test.py --to 55DDDNUMERO --order TESTE123`.
   Abra o link do botão e confirme que a página do PIX mostra o QR e o "copia e cola".
7. Três testes reais com a oferta de R$ 1,00 (não pagar / pagar antes / deixar expirar) —
   veja `CHECKLIST.md` §7. Depois de cada um, olhe **Painel › Início** e o log do worker.
8. **Token da Kirvano**: o primeiro webhook real revela como o token chega. Veja em
   **Painel › Eventos** o campo `auth_debug` (só nomes de cabeçalho, nunca valores) e então
   troque `KIRVANO_TOKEN_MODE` de `log` para `enforce` no `/etc/pix-recovery/env` e reinicie:
   `systemctl restart pix-api`.
9. Anote no calendário: verificação do negócio na Meta libera o limite de 250 → 2.000
   destinatários por 24 h.

---

## 4. Ler os logs

Tudo vai para o journald. Nada de arquivo de log para rotacionar.

```bash
ssh -i ~/.ssh/id_ed25519_pix root@179.199.147.30

journalctl -u pix-api -f                     # API ao vivo (webhooks chegando)
journalctl -u pix-worker -f                  # worker ao vivo (lembretes saindo)
journalctl -u pix-api -u pix-worker -n 200 --no-pager     # últimas 200 linhas dos dois
journalctl -u pix-worker --since "1 hour ago" -p warning  # só avisos e erros
journalctl -u pix-worker --since today | grep -i "sale\|job\|graph"

systemctl status pix-api pix-worker          # está rodando? desde quando? reiniciou?
systemctl restart pix-api pix-worker         # reinício manual
journalctl -u nginx -n 50 --no-pager         # problemas de TLS/proxy
tail -f /var/log/nginx/pix-api.access.log    # requisições que chegaram de fora
```

O que procurar:

- `kirvano webhook rejected` → token errado no modo `enforce`;
- `meta webhook: bad or missing signature` → `META_APP_SECRET` errado;
- `graph send failed` → veja o código do erro na tabela da seção 7;
- worker sem nenhuma linha há mais de 1 minuto → `/health` mostra `"worker":"stale"`.

Os segredos nunca aparecem no log: o token vai só no cabeçalho `Authorization`, o CPF é
descartado na entrada e o modo `log` da Kirvano guarda apenas **nomes** de cabeçalho.

---

## 5. O painel (sem deploy, sem código)

`https://api.jornadaanjo.cloud/painel`, com usuário e senha do `/etc/pix-recovery/env`
(`PANEL_USER` / `PANEL_PASSWORD`).

| Página | Para quê |
|---|---|
| **Início** | pendentes agora, lembretes enviados hoje/7 dias, pagos depois do lembrete, cancelados por pagamento, expirados, falhas, últimos 50 pedidos |
| **Configurações** | ligar/desligar, minutos de espera, horário silencioso, limite diário, nome/idioma do modelo, índice do botão, link do checkout |
| **Carrinho** | recuperação de carrinho abandonado: ligar/desligar, cupom, 1 a 3 mensagens (horário, modelo, botão, parâmetros), resultados (enviadas, entregues, lidas, cliques, vendas e valor recuperados) e os últimos 50 carrinhos |
| **Conversas** | respostas dos clientes; responder dentro da janela de 24 h |
| **Descadastros** | quem pediu para sair; incluir/remover na mão |
| **Eventos** | os últimos 100 webhooks recebidos, com o JSON cru |
| **Modelo** | status e categoria do modelo, como vistos pela última vez |

Desligar os lembretes é uma chave só (**Configurações › Ativado**); vale no próximo ciclo do
worker, em segundos.

### Trocar o nome do modelo

1. Crie o novo modelo no Gerenciador do WhatsApp (campos exatos em `docs/TEMPLATE.md`) e
   espere sair de `PENDING`.
2. Confirme: `uv run python scripts/check_template.py --name pix_pendente_v3`.
3. **Painel › Configurações › Nome do modelo** → `pix_pendente_v3` → salvar.
   Se o modelo novo tiver botões em outra ordem, ajuste **Índice do botão de URL**
   (`-1` quando não houver botão de link); se tiver outra ordem de variáveis, ajuste a
   **ordem dos parâmetros**.
4. Pronto — o valor do banco tem prioridade sobre o `.env`, e vale já no próximo envio.
   Nenhum deploy, nenhum restart.

### Recuperação de carrinho abandonado

Vem **desligada**. Para ligar:

1. Na Kirvano, edite o webhook "Recuperação PIX" e marque também o evento **Carrinho
   abandonado** (mesma URL, mesmo token). Confira a primeira entrega em **Painel › Eventos**:
   o corpo real desse evento ainda não tinha sido capturado quando o fluxo foi escrito, e o
   parser foi feito tolerante (telefone, nome, produto, valor e link procurados nos mesmos
   campos dos outros eventos).
2. Crie o modelo de **Marketing** do `docs/TEMPLATE.md` §9 e espere a aprovação.
3. **Painel › Carrinho**: cupom (precisa existir na Kirvano), modelo, link do checkout de
   reserva → marque **Recuperação de carrinho ativada** → salvar → **Consultar status na
   Meta**.
4. Teste sem cliente real: `uv run python scripts/simulate_kirvano.py --event ABANDONED_CART
   --phone 55DDDSEUNUMERO --url https://api.jornadaanjo.cloud/webhooks/kirvano` e veja o
   carrinho aparecer em **Carrinho** com a mensagem agendada.

Regras que o sistema aplica sozinho: cada mensagem de um carrinho sai no máximo uma vez
(mesmo com o evento repetido pela Kirvano, e o mesmo telefone no mesmo dia continua a mesma
sequência); gerar o PIX ou comprar cancela as mensagens que faltam na hora; quem está com PIX
em andamento, recebeu o lembrete do PIX nas últimas 24 h, já comprou o produto nos últimos 30
dias ou pediu para sair não recebe; horário silencioso e limite diário são os mesmos do PIX
(o PIX tem prioridade). **Venda recuperada** = compra aprovada depois de pelo menos uma
mensagem de carrinho; compra antes da mensagem conta à parte.

---

## 6. Rotacionar o token da Meta

O token é de usuário do sistema e **não expira**, mas troque se ele vazar, se alguém sair da
equipe ou se aparecer erro `#190`/HTTP 401 (o worker pausa os envios sozinho nesse caso e
registra um alerta).

1. `business.facebook.com` › **Configurações › Usuários › Usuários do sistema** › `pix-bot`
   › **Gerar novo token** › app correto › expiração **Nunca** › marque
   `whatsapp_business_messaging`, `whatsapp_business_management`, `business_management`.
2. Copie o token (aparece uma vez só).
3. No servidor:
   ```bash
   ssh -i ~/.ssh/id_ed25519_pix root@179.199.147.30
   nano /etc/pix-recovery/env          # troque a linha META_ACCESS_TOKEN=
   systemctl restart pix-api pix-worker
   ```
4. Atualize também o `secrets.env` da sua máquina (nunca comite, nunca cole em chat).
5. Valide: `uv run python scripts/check_template.py` e
   `uv run python scripts/subscribe_app.py --list`.
6. Revogue o token antigo na mesma tela do passo 1.

O arquivo `/etc/pix-recovery/env` é `0600 root:pixapp`: o systemd lê como root e injeta nos
serviços, então o usuário da aplicação nunca precisa abrir o arquivo.

---

## 7. Erros da Meta: o que fazer em cada código

O código já trata cada um destes; a coluna "o que fazer" é para quando o problema persiste.

| Código | Significado | O sistema faz | O que fazer |
|---|---|---|---|
| `131009` | parâmetro inválido (quebra de linha, tab, 4+ espaços) | higieniza mais forte e tenta 1x | se repetir, veja o nome do cliente no pedido — caractere estranho |
| `131026` | número não recebe / não é WhatsApp | tenta a outra forma do 9º dígito e depois marca `número não tem WhatsApp` | nada; é cadastro errado do cliente |
| `132000` | quantidade de parâmetros diferente do modelo | falha e gera alerta | conserte a ordem em **Configurações › parâmetros** (`docs/TEMPLATE.md` §5) |
| `132001` | modelo não existe nesse idioma | falha e gera alerta | confira nome e `pt_BR` com `check_template.py` |
| `132012` / `132018` | formato do parâmetro não bate | higieniza e tenta 1x | compare com a amostra cadastrada no modelo |
| `132015` | modelo **pausado** por qualidade | marca o modelo indisponível e gera alerta | espere a Meta liberar ou crie `..._v3` e troque no painel |
| `132016` | modelo **desativado** | idem | crie um modelo novo; revise o texto |
| `131047` | fora da janela de 24 h (só texto livre) | mostra no painel | responda por modelo, não por texto livre |
| `131049` | limite de marketing por usuário | **não** tenta de novo por 24 h | peça reclassificação para *Utilidade* |
| `131050` | usuário optou por não receber marketing | registra o descadastro | nada — respeite |
| `130429` / `131056` / `80007` | limite de envio / de par | espera progressiva, até 3 tentativas | se for constante, o volume passou do limite da conta |
| `190` / HTTP 401 | token inválido ou expirado | **pausa os envios** e gera alerta | rotacione o token (seção 6) |

Outros motivos de "não enviei" que aparecem no painel, e não são erro da Meta:
`pedido já pago`, `PIX expiraria antes`, `adiado (horário silencioso)`, `limite diário
atingido`, `cliente pediu para não receber`, `sem telefone válido`, `pausado (lembretes
desligados)`.

---

## 8. Kirvano

- O webhook aceita qualquer entrega enquanto `KIRVANO_TOKEN_MODE=log` e guarda só os
  **nomes** dos cabeçalhos (o transporte do token não está documentado). Depois da primeira
  entrega real, veja **Painel › Eventos**, descubra onde o token vem e mude para `enforce`.
- Não existe API nem sandbox: o nosso banco de eventos é a única fonte de verdade. Para
  repetir um cenário use **Ver logs › Detalhes › Reenviar webhook** na própria Kirvano, ou o
  `scripts/simulate_kirvano.py`.
- O PIX desse checkout vale **24 horas** (não 1 hora como no exemplo da documentação). Se o
  cliente mudar essa configuração, o sistema se adapta sozinho: a validade vem no payload.
- `payment.qrcode_image` **não é uma URL** — é o mesmo "copia e cola". Por isso a página
  `/p/<token>` desenha o QR no servidor.

---

## 9. Hostinger: dois cuidados que derrubam tudo

1. **Firewall do hPanel.** Se houver um grupo de firewall ativo no VPS, ele filtra *antes* do
   `ufw`. É preciso ter regras `accept TCP` para **22, 80 e 443** (origem: qualquer lugar).
   Sem a regra da 22 você perde o SSH — sobra só o Terminal do navegador do hPanel.
   Caminho: hPanel › VPS › Gerenciar › **Segurança › Firewall**.
2. **"Reset SSH" desfaz o endurecimento.** O botão *Reset SSH* / *Redefinir SSH* do hPanel
   reescreve o `sshd_config` e apaga `/etc/ssh/sshd_config.d/99-hardening.conf` — o login por
   senha volta a ficar ligado. Se alguém apertar esse botão, rode de novo:
   ```bash
   bash deploy/deploy.sh --setup     # ou, no servidor: bash /opt/pix-recovery/deploy/setup_server.sh
   ```
   O script é idempotente: não recria o banco nem troca a senha já gravada.

Outros detalhes: a senha de root do hPanel continua valendo (o cliente mantém o controle),
e a troca de senha aparece em *Backup & Monitoring › Ações Recentes*.

---

## 10. Por que um pagamento nunca "perde a corrida"

A única promessa do sistema é: **quem já pagou não recebe cobrança**.

Os dois caminhos que podem tocar o mesmo pedido tomam **primeiro o lock da linha do pedido**
(`SELECT … FOR UPDATE`), e só depois o do lembrete (o worker com `FOR UPDATE SKIP LOCKED`):

- o webhook de pagamento chega primeiro → dentro da mesma transação ele marca `pago` e
  cancela o lembrete; o worker, ao acordar, relê o pedido já com o lock e desiste;
- o worker chega primeiro → ele muda o lembrete para `sending` e confirma a transação antes
  de falar com a Meta; a mensagem já está a caminho, e o webhook apenas registra o pagamento
  sem mexer no lembrete.

Como os dois pegam os locks na **mesma ordem**, não existe deadlock; o `SKIP LOCKED` permite
mais de um worker sem envio duplicado. No SQLite (testes/desenvolvimento) o SQLAlchemy ignora
o `FOR UPDATE`, o que é seguro porque o SQLite tem um único escritor.

Além disso, `recovery_jobs.order_id` é **único**: um lembrete por pedido, para sempre. Nunca
mande uma segunda cobrança "na mão" — é o caminho mais rápido para a Meta pausar o modelo.

---

## 11. Privacidade (LGPD)

- O **CPF** (`customer.document`) e os **cookies de anúncio** são descartados na entrada:
  não vão para o banco, nem para o payload guardado, nem para o log.
- O **IP do checkout** e a data do evento ficam no pedido como prova de opt-in. Aparecem na
  coluna *Quando* da lista de pedidos em **Painel › Início** — nunca em página pública.
- **Retenção de 12 meses, aplicada de verdade.** O timer `pix-retention` roda
  `scripts/purge_old_data.py` todo dia às 03:20: pedidos e mensagens com mais de 365 dias têm
  nome, e-mail, telefones, `wa_id`, IP de consentimento e o código PIX apagados (a linha
  continua existindo, então os números do Início não mudam), os `webhook_events` antigos são
  removidos e os contatos inativos ficam sem nome/telefone. A **lista de descadastros nunca é
  apagada** — é ela que impede novas mensagens para quem pediu SAIR. Para conferir antes:
  ```bash
  ssh ... 'cd /opt/pix-recovery && .venv/bin/python scripts/purge_old_data.py --dry-run'
  systemctl list-timers pix-retention.timer     # quando roda a próxima vez
  journalctl -u pix-retention -n 20             # o que a última limpeza fez
  ```
- As páginas públicas mostram no máximo o primeiro nome e o código do pedido; não têm
  rastreadores, fontes externas nem cookies.
- Página de privacidade: `https://api.jornadaanjo.cloud/privacidade` (exigida pela Meta antes
  de o app ir para Live). Controlador: CONNECT LT NEGOCIOS DIGITAIS LTDA, CNPJ
  52.134.502/0001-09; contato `jornadacommeuanjo@outlook.com`.
- Descadastro imediato por `SAIR` (texto ou botão), com resposta automática de confirmação.

---

## 12. Estrutura do repositório

```
app/                 aplicação (mapa dos módulos em docs/ARCHITECTURE.md §2)
app/templates/       páginas Jinja2 (todas estendem base.html)
tests/               pytest; tests/fixtures/kirvano_pix_generated.json é um webhook REAL
deploy/              setup_server.sh, deploy.sh, check.sh, nginx, systemd (+ timer), env.example
scripts/             subscribe_app.py, check_template.py, send_test.py, simulate_kirvano.py,
                     purge_old_data.py (retenção de 12 meses)
docs/                SPEC.md (produto), ARCHITECTURE.md (código), TEMPLATE.md (modelo)
CHECKLIST.md         passo a passo de acessos (Kirvano, Meta, Hostinger)
```

Nada de segredo entra no repositório: `secrets.env` está no `.gitignore` e o servidor lê
tudo de `/etc/pix-recovery/env`.
