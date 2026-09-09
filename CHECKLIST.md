# PIX Recovery — your to-do list, click by click

Legend: **YOU** = you click · **ME** = Claude does it (code/scripts, you paste) · **CLIENT** = Tiago must do
Labels shown as *pt-BR / EN* (his panels may be in either). Deep links marked **(verified)** were confirmed on the vendor's own docs on 2026-09-08; others: navigate by clicks.
All secrets go into `secrets.env` in this folder — never into the platform chat.

---

## 0. Right now, before sending the message (15 min) — YOU

**0.1 Hostinger account (so his invite can land)**
- He is Brazilian, so most likely on hostinger.com.br. Register at https://www.hostinger.com.br → *Entrar → Criar conta* with `kanarihirosi0112@gmail.com`. Global site: https://auth.hostinger.com/register (verified: email + password, no purchase needed).
- Gotcha (verified): sharing only works if both accounts are on the **same site** (.com vs .com.br). Add this line to the client message:
  `Sua conta da Hostinger é na hostinger.com ou na hostinger.com.br? Preciso criar a minha no mesmo site para o compartilhamento funcionar.`
  If he answers ".com", register there too (free).

**0.2 SSH key** — PowerShell:
```
ssh-keygen -t ed25519 -C "kanari-pix" -f $env:USERPROFILE\.ssh\id_ed25519_pix
Get-Content $env:USERPROFILE\.ssh\id_ed25519_pix.pub
```
Keep the `.pub` output; you paste it in hPanel at step 3.4. (If Hostinger refuses ed25519: `ssh-keygen -t rsa -b 4096 -f $env:USERPROFILE\.ssh\id_rsa_pix`.)

**0.3 Send the message** (the short one, plus the Hostinger-site line).

---

## 1. Kirvano — you already have access (20 min) — YOU
Login: https://app.kirvano.com/ (verified)

**1.1 Check what kind of access he gave you.** Sidebar must show *Integrações › Webhooks* (older UI: *Extensões › Webhooks*, https://app.kirvano.com/extensions/webhooks). If it's missing, he added you as a **co-producer** of the product, not a collaborator (only *Vendas / Ofertas* visible). Tell me — he'd have to re-invite you via *Colaboradores*, or create the webhook himself at step 6.1.

**1.2 PIX expiry — DONE (24h).** Confirmed from a real webhook log, no need to ask: `created_at 2026-07-10 17:05:30` -> `expires_at 2026-07-11 17:05:30`. Already written to `secrets.env`. Re-check only if he changes the checkout setting.

**1.3 Consent line.** Same checkout › aba **Visual** › "Barra de anúncios" → paste:
`Ao informar seu celular, você concorda em receber da Jornada com Meu Anjo, pelo WhatsApp, avisos sobre esta compra. Para parar, responda SAIR.`
→ *Salvar alterações* (top right). Then *Ofertas › Editar* on the live offer → confirm this checkout is the one selected → *Atualizar a oferta*.
Open the public checkout link and confirm the bar shows the whole sentence (char limit is undocumented). If it truncates, use the shorter one:
`Ao informar seu celular você aceita receber, pelo WhatsApp da Jornada com Meu Anjo, avisos sobre esta compra. Responda SAIR para parar.`

**1.4 Kirvano's own recovery.** *Produtos › product › Configurações* and every sub-menu (Checkout, Ofertas, Gatilhos) — look for anything named *Recuperação de vendas / ChatFlow / Mensagens / WhatsApp / SMS*. If a WhatsApp/SMS PIX reminder is ON, screenshot it and tell me (two messages per PIX would hurt his quality rating). Note: *"Recuperação ativa"* = cancellation-retention offers, unrelated — leave it.

**1.5 Checkout link.** *Produtos › product › Ofertas* → copy the `https://pay.kirvano.com/<uuid>` link → `secrets.env` → `KIRVANO_CHECKOUT_URL`.

**1.6 Do NOT create the webhook yet** — that's step 6.1, once the server has SSL.

---

## 2. Meta — CLIENT does this (you cannot use Facebook; guide him with the pt-BR message sent 2026-09-08)
Login: https://business.facebook.com/ → **top-left portfolio switcher → select HIS portfolio**. Every step below must be inside it, not your own.

**2.1 Business ID.** *Configurações › Informações da empresa / Settings › Business info* — https://business.facebook.com/latest/settings/business_info → the number under the business name ("ID do portfólio empresarial / Meta Business ID") → `secrets.env` → `META_BUSINESS_ID`. Also screenshot *Detalhes da empresa*: razão social and address must equal the CNPJ card exactly (`CONNECT LT NEGOCIOS DIGITAIS LTDA`, Av Pref Osmar Cunha 416, Sala 1108/189 Edif Koerich Empresarial, Centro, Florianópolis/SC, 88015-100).

**2.2 Your role.** *Configurações › Usuários › Pessoas / Settings › Users › People* → find yourself. For steps 2.5 and 5 you need **Controle total / Full control** on the portfolio AND on the WhatsApp account (WABA). If you only have *Acesso de funcionário / Employee access*, send him:
`Tiago, no Gerenciador de Negócios, em Configurações > Usuários > Pessoas, clica no meu e-mail e marca "Controle total", tanto na empresa quanto na conta do WhatsApp. Preciso disso para criar o token do sistema e o modelo.`

**2.3 Templates.** *Gerenciador do WhatsApp › Modelos de mensagem* — https://business.facebook.com/latest/whatsapp_manager/message_templates/?business_id=<ID>&waba_id=958025707339789 (replace `<ID>`) → screenshot the list showing *Status* and *Categoria* → send me. Don't create or delete anything yet.

**2.4 Phone number.** *Gerenciador do WhatsApp › Ferramentas da conta › Números de telefone* — https://business.facebook.com/latest/whatsapp_manager/phone_numbers/?business_id=<ID>&waba_id=958025707339789 → screenshot: *Status* (must be **Conectado / Connected**), *Classificação por qualidade*, *Limite de mensagens*, display name → send me.

**2.5 Permanent API token** (needs Full control — do after 2.2 is OK):
- a) App: https://developers.facebook.com/apps → *Criar app / Create App* → use case "Connect with customers through WhatsApp" (or type *Business* + add the *WhatsApp* product) → link it to HIS portfolio → *App settings › Basic*: copy **App ID** and **App secret** → `secrets.env`.
- b) System user: *Configurações › Usuários › Usuários do sistema / Settings › Users › System users* — https://business.facebook.com/latest/settings/system_users → *Adicionar / Add* → name `pix-bot` → role **Administrador / Admin** → *Criar usuário do sistema*.
- c) *Atribuir ativos / Assign assets*: tab **Apps** → your app → toggle *Gerenciar app / Manage app* (Controle total). Tab **Contas do WhatsApp / WhatsApp accounts** → WABA `958025707339789` → toggle *Gerenciar contas do WhatsApp Business* (Controle total) → save.
- d) *Gerar novo token / Generate new token* → pick the app → expiration **Nunca / Never** → tick `whatsapp_business_messaging`, `whatsapp_business_management`, `business_management` → *Gerar token* → copy (shown once) → `secrets.env` → `META_ACCESS_TOKEN`.
- e) *Configurações › Contas › Contas do WhatsApp* → select the WABA → tab *Apps atribuídos / Assigned apps* → add your app.

**2.6 Payment method.** *Gerenciador do WhatsApp › Visão geral › (account) ⋯ › Gerenciar configurações da conta › Configurações › Configurações de pagamento*. Is there a card? If not, template messages will not send; the CLIENT must add one (Visa/Mastercard, not prepaid; choose Brasil and **BRL** if offered — currency locks forever). Tell me and I'll draft the pt-BR ask.

---

## 3. Hostinger — when his invite email arrives (15 min) — YOU
**3.1** Click the link in the email → login hPanel → Home → *Gerenciar / Manage* next to his account (impersonate mode; *Sair / Exit* in the top banner returns to yours). https://hpanel.hostinger.com/profile/account-access/account-sharing (verified) lists it under *Contas às quais eu tenho acesso*.

**3.2 DNS.** https://hpanel.hostinger.com/domains (verified) → *jornadaanjo.cloud › Gerenciar › DNS / Nameservers › Gerenciar registros DNS*:
*Tipo* `A` · *Nome* `api` (only "api", not the full domain) · *Aponta para* `179.199.147.30` · *TTL* default → *Adicionar registro*.
Verify in PowerShell: `nslookup api.jornadaanjo.cloud 8.8.8.8` → should answer 179.199.147.30 within minutes.

**3.3 Firewall.** https://hpanel.hostinger.com/servers (verified) → *Gerenciar › Segurança › Firewall*.
- Group exists and is active → *⋯ › Editar › Adicionar regra de firewall*: Ação `aceitar / accept` · Protocolo `TCP` · Porta `22` · Origem `qualquer lugar / anywhere` → *Adicionar regra*. Repeat for `80` and `443`.
- No group → *Adicionar Firewall* → name `pix-api` → *Criar* → add the same three rules (**22 first**) → activate.
- Applies in ≤2 min. Never activate a group without the port-22 rule (it locks SSH out; browser terminal still works).

**3.4 SSH key.** *Gerenciar › Configurações › Chaves SSH › Adicionar chave SSH* → paste the `.pub` line from 0.2 → save.

**3.5 Root password.** *Configurações › Configurações principais › Senha root* → new strong password → *Atualizar*. Confirm in *Backup & Monitoring › Ações Recentes*: `ct_set_rootpasswd` = Success. → `secrets.env` → `VPS_ROOT_PASSWORD`. (He keeps control via hPanel *Terminal* and *Senha root*; you don't need to tell him the value.)

**3.6 Test + hand over.** PowerShell: `ssh -i $env:USERPROFILE\.ssh\id_ed25519_pix root@179.199.147.30`. Also note *Sistema operacional* in the VPS sidebar (plain Ubuntu 24.04, or a template with a panel?). Then tell me **"access ready"**.

---

## 4. Build — ME (you paste commands and report output)
Server hardening (keys-only SSH, ufw, fail2ban), PostgreSQL, the app, SSL via certbot, the `/p/<token>` PIX page (QR + copia-e-cola + copy button), both webhook endpoints, the panel (delay, on/off, inbox, replies), opt-out handling, tests.

---

## 5. Template — after `https://api.jornadaanjo.cloud/p/abc123` is live (10 min) — YOU
*Gerenciador do WhatsApp › Modelos de mensagem › Criar modelo / Criar modelo de mensagem*:
- *Categoria* **Utilidade / Utility** · *Nome* `pix_pendente_v2` · *Idioma* **Português (BR)**
- *Corpo / Body*:
  `Olá {{1}}, o pagamento do pedido {{2}} no valor de R$ {{3}} está pendente. Seu código PIX segue válido até {{4}}. Se já realizou o pagamento, desconsidere esta mensagem.`
- *Rodapé / Footer*: `Para não receber mais avisos, responda SAIR.`
- *Botões › Resposta rápida / Quick reply*: `Não quero receber`
- *Botões › Chamada para ação › Acessar o site / Visit website* · tipo **Dinâmico / Dynamic** · URL `https://api.jornadaanjo.cloud/p/{{1}}` · texto do botão `Ver código PIX`
- *Adicionar amostra / Add sample*: {{1}} `Maria` · {{2}} `D2RP8RQ7` · {{3}} `97,00` · {{4}} `15/09 às 18:00` · button suffix `abc123`
- *Enviar / Submit*.
- If the red "A categoria não corresponde" dialog appears → **Cancelar**, screenshot me. If a v2 created by him is *Em análise*, leave it (different name is fine).
- If Meta approves it as **Marketing**: https://business.facebook.com/business-support-home → *Template Category Updates › Available for Review* → tick → *Request Review / Solicitar análise* (within 60 days).

---

## 6. Webhooks — after SSL is live (10 min) — YOU
**6.1 Kirvano.** *Integrações › Webhooks › Criar Webhook*: *Nome* `Recuperação PIX` · *URL do Webhook* `https://api.jornadaanjo.cloud/webhooks/kirvano` · *Token* = `KIRVANO_WEBHOOK_TOKEN` from `secrets.env` · *Produto* Jornada com Meu Anjo · *Evento*: **PIX gerado, Compra aprovada, Pix expirado** → save. *Ver logs* on the webhook row shows every delivery.

**6.2 Meta.** https://developers.facebook.com/apps → your app › *WhatsApp › Configuração / Configuration › Webhook › Editar / Edit*: *Callback URL* `https://api.jornadaanjo.cloud/webhooks/meta` · *Verify token* = `META_VERIFY_TOKEN` from `secrets.env` → *Verificar e salvar / Verify and save*. Then *Gerenciar / Manage* webhook fields → *Assinar / Subscribe*: `messages`, `message_template_status_update`, `template_category_update`, `message_template_quality_update`, `phone_number_quality_update`, `phone_number_name_update`, `account_update`. Switch the app to **Live / Ativo**. (I run the `subscribed_apps` API call.)

---

## 7. Tests — real money, no sandbox exists (30 min) — YOU + ME
Kirvano › *Produtos › product › Ofertas › Adicionar Ofertas*: name `TESTE`, price **R$ 1,00** (platform minimum; Kirvano's fee on R$1 is R$1, net zero), status active. Three runs using your own phone at the checkout:
- a) generate PIX, don't pay → message arrives after the delay;
- b) generate PIX, pay within the delay → no message;
- c) generate PIX, wait past expiry → no message.
Deactivate the TESTE offer afterwards.

---

## 8. Client-side, later — CLIENT (I'll draft each pt-BR message when it's time)
- **Business verification**: *Configurações › Central de segurança › Iniciar verificação* (admin only). Documents: cartão CNPJ + conta de luz/extrato in the company name; razão social and address typed exactly as on the CNPJ card; website field wants https. Up to 14 business days.
- **Payment method** on the WABA if 2.6 shows none.
- **Website** showing "Jornada com Meu Anjo" for the display name (fallback name Meta itself suggests: "Jornada com Meu Anjo por Connect LT").
