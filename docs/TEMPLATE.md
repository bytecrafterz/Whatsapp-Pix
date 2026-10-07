# Modelo de mensagem `pix_pendente_v2` — campos exatos

Este é o texto que deve ser criado no **Gerenciador do WhatsApp › Modelos de mensagem ›
Criar modelo**. O código só *referencia* o modelo pelo nome: nada aqui é gerado
automaticamente, e qualquer diferença de nome, idioma ou número de variáveis faz o envio
falhar (erros `#132001` ou `#132000`).

> Depois de criado, confira com `uv run python scripts/check_template.py`.

---

## 1. Cabeçalho do formulário

| Campo | Valor exato |
|---|---|
| **Categoria** | `Utilidade` / *Utility* |
| **Nome** | `pix_pendente_v2` |
| **Idioma** | `Português (BR)` — código `pt_BR` |

O nome só aceita minúsculas, números e `_`. Se precisar de uma variação, crie
`pix_pendente_v3` e troque o nome **no painel** (Configurações › *Nome do modelo*): não é
preciso novo deploy.

## 2. Corpo (Body)

Copie e cole exatamente:

```
Olá {{1}}, o pagamento do pedido {{2}} no valor de R$ {{3}} está pendente. Seu código PIX segue válido até {{4}}. Se já realizou o pagamento, desconsidere esta mensagem.
```

Repare que o `R$` está **no texto fixo**: a variável `{{3}}` recebe só `97,00`.

## 3. Rodapé (Footer)

```
Para não receber mais avisos, responda SAIR.
```

O rodapé é obrigatório para a nossa política de opt-out: quem responde `SAIR`, `PARAR`,
`STOP`, `CANCELAR`, `NÃO QUERO` (com ou sem acento) entra na lista de descadastro e nunca
mais recebe lembrete.

## 4. Botões — nesta ordem

| Índice | Tipo | Configuração |
|---|---|---|
| **0** | Resposta rápida / *Quick reply* | texto: `Não quero receber` |
| **1** | Chamada para ação › Acessar o site, tipo **Dinâmico** | URL: `https://api.jornadaanjo.cloud/p/{{1}}` · texto do botão: `Ver código PIX` |

A **ordem importa**: o código manda o parâmetro do link para o botão de índice `1`
(configurável no painel em *Índice do botão de URL*; `-1` = modelo sem botão de URL).
Se você inverter os botões no formulário da Meta, mude o índice no painel para `0`.

O botão dinâmico recebe apenas o **sufixo** da URL — o `page_token` do pedido, não o link
inteiro.

## 5. Amostras (Adicionar amostra)

A Meta exige um exemplo para cada variável:

| Variável | Amostra | O que o sistema envia de verdade |
|---|---|---|
| `{{1}}` | `Maria` | primeiro nome do cliente, higienizado; `cliente` quando não há nome |
| `{{2}}` | `D2RP8RQ7` | `sale_id` da Kirvano (8 caracteres maiúsculos) — é o "pedido" que o cliente vê |
| `{{3}}` | `97,00` | valor em reais, sem o `R$`, com vírgula decimal |
| `{{4}}` | `15/09 às 18:00` | validade do PIX em `dd/mm às HH:MM`, fuso America/Sao_Paulo |
| botão URL | `abc123` | `page_token` do pedido (link final: `https://api.jornadaanjo.cloud/p/abc123`) |

A ordem das variáveis vem da configuração `TEMPLATE_PARAMS`
(padrão `first_name,sale_id,amount,expiry`). Trocar a ordem no modelo **exige** trocar essa
lista também, senão a Meta devolve `#132000` (quantidade/ordem de parâmetros).

## 6. Regras que a Meta aplica nos parâmetros

O sistema já higieniza os valores antes de enviar, mas vale saber:

- nenhum parâmetro pode conter **quebra de linha**, **tabulação** ou **4 espaços seguidos**
  (erro `#131009`, subcódigo 2494073);
- caracteres de controle são removidos e o nome é limitado a 60 caracteres;
- o corpo renderizado (texto + variáveis) precisa ficar abaixo de 1024 caracteres;
- a variável não pode ficar vazia — por isso o nome cai para `cliente` quando falta.

## 7. Depois de enviar para análise

1. Aprovação costuma sair em minutos. Acompanhe com
   `uv run python scripts/check_template.py`.
2. Se aparecer o aviso vermelho **"A categoria não corresponde"**, cancele e mande um print:
   o texto acima é transacional e deve passar como *Utilidade*.
3. Se a Meta aprovar como **Marketing**, o preço sobe (~R$0,32 contra ~R$0,035 por mensagem)
   e passam a valer o limite por usuário (`#131049`) e o opt-out de marketing (`#131050`).
   Peça revisão em `business.facebook.com/business-support-home` ›
   *Template Category Updates* › *Available for Review* (prazo de 60 dias).
4. Status `PAUSED` (`#132015`) ou `DISABLED` (`#132016`) significam qualidade ruim: o painel
   mostra o estado em **Modelo** e os lembretes param com o motivo *modelo indisponível*.
   É exatamente por isso que o sistema envia **um único lembrete por pedido, para sempre** —
   uma segunda cobrança é o caminho mais rápido para a Meta pausar o modelo.

## 8. Trocar de modelo sem deploy

No painel, em **Configurações**:

| Campo | Chave | Observação |
|---|---|---|
| Nome do modelo | `template_name` | precisa existir e estar `APPROVED` na WABA |
| Idioma do modelo | `template_language` | `pt_BR` |
| Índice do botão de URL | `url_button_index` | `-1` se o novo modelo não tiver botão de link |
| Ordem dos parâmetros | `template_params` | chaves válidas: `first_name`, `customer_name`, `sale_id`, `amount`, `amount_full`, `expiry`, `product`, `page_url` |

Vale a mudança no próximo envio, sem reiniciar nada.

---

## 9. Modelo do carrinho abandonado — `carrinho_abandonado_v1`

Usado pela recuperação de carrinho (painel › **Carrinho**). Diferente do modelo do PIX,
este é **Marketing**: oferece um cupom para quem nem chegou a gerar o PIX. A Meta cobra mais
por mensagem (~R$ 0,32) e aplica o limite de marketing por usuário (`#131049`); o sistema não
reenvia nesse caso.

| Campo | Valor exato |
|---|---|
| **Categoria** | `Marketing` |
| **Nome** | `carrinho_abandonado_v1` (é o padrão do painel; outro nome → troque em Carrinho) |
| **Idioma** | `Português (BR)` — código `pt_BR` |

**Corpo** (sugestão — o texto é do cliente; o que importa é a ORDEM das variáveis):

```
Olá {{1}}, vimos que você começou a garantir {{2}} e não finalizou. Separamos um cupom especial para você: {{3}}. É só usar no checkout, ele vale por pouco tempo.
```

| Variável | Amostra | O que o sistema envia | Chave no painel |
|---|---|---|---|
| `{{1}}` | `Maria` | primeiro nome; `cliente` quando não há nome | `first_name` |
| `{{2}}` | `A Jornada com meu Anjo` | nome do produto do carrinho | `product` |
| `{{3}}` | `VOLTA10` | o campo **Cupom** do painel (troca sem nova aprovação) | `coupon` |

Ordem configurada por padrão: `first_name,product,coupon`. Outras chaves aceitas: `customer_name`,
`amount` (`97,00`), `amount_full` (`R$ 97,00`), `link` (URL completa, para modelo sem botão).

**Rodapé:** `Para não receber mais mensagens, responda SAIR.`

**Botão** — *Chamada para ação › Acessar o site*, tipo **Dinâmico**:

| Campo | Valor |
|---|---|
| Texto do botão | `Finalizar compra` |
| URL | `https://api.jornadaanjo.cloud/c/{{1}}` |
| Amostra | `abc123` |

O botão leva ao **nosso** endereço `/c/…`, que conta o clique e redireciona para o checkout do
próprio carrinho (ou para o *Link do checkout (reserva)* do painel quando a Kirvano não manda o
link). Por isso o modelo não depende do formato de link da Kirvano. Com um único botão, o
**índice do botão de link** é `0`. Se acrescentar o botão de opt-out de marketing da Meta
(“Parar promoções”) ANTES do link, o índice passa a ser `1`; tocar nele já descadastra o número.

Para uma 2ª ou 3ª mensagem, crie outro modelo (ex.: `carrinho_lembrete_v1`) e preencha a
Mensagem 2/3 no painel. Duas mensagens do mesmo carrinho nunca saem com menos de 60 minutos
entre elas.
