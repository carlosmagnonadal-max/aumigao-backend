# Provisão fiscal + extrato de divisão por pagamento — Design (2026-06-26)

## Objetivo

Dar visibilidade contábil, no admin, da **divisão completa de cada pagamento** —
quanto é repasse do passeador, quanto é receita da Aumigão, e quanto de **imposto
provisionado** de cada lado — de modo que o saldo **sacável** já reflita o que está
reservado para o fisco. Os parâmetros fiscais ficam **editáveis no admin, por tenant**.

Decisão de produto (Carlos, 2026-06-26):
- Extrato de divisão **completo por pagamento** (passeador + Aumigão + imposto de cada).
- Camada **contábil/visível** — NÃO move/segrega dinheiro físico (1 conta Asaas).
  Reversível, baixo risco. O split real e o saque real seguem dormentes.
- Parâmetros **por tenant** (coerente com white-label).
- Provisão como **snapshot imutável por pagamento** (congela a divisão quando o dinheiro
  entra). Mudar alíquota afeta só pagamentos futuros.
- **Config fiscal unificada**: a mesma config por tenant alimenta a provisão (agora) e a
  emissão de NFS-e (quando ligar), aposentando os placeholders de env do `nfse_config`.
- **Escopo v1 = backend primeiro**; tela admin-web e fiação dos params no NFS-e são peças
  seguintes (planos próprios).

## Contexto do código existente

- `Payment` já grava o split contábil: `amount`, `commission_percent`, `platform_amount`
  (receita da Aumigão), `walker_amount` (repasse do passeador). Sprint 16.
- `payment_split_service` registra o split; o **repasse real ao passeador é dormente**.
- Confirmação de pagamento passa pelo `asaas_webhook` (`app/routes/payments.py`), nos
  ramos: passeio regular, mensalidade SaaS (`_handle_tenant_saas_subscription_webhook`),
  gorjeta (`_handle_tip_webhook`). Status confirmado = `_PAYMENT_CONFIRMED_STATUS`.
- NFS-e (scaffold dormente, commit `294ca47`): `nfse_config` lê params fiscais de env
  (placeholders). Esta feature substitui esses placeholders pela config por tenant.

## Modelo de dados (2 tabelas novas, additivas)

### `tenant_fiscal_config` (1 por tenant; editável no admin)
Campos de provisão (percentuais, default 0 → provisão zero até configurar):
- `commission_tax_percent` — imposto provisionado sobre a comissão de passeio (lado Aumigão).
- `subscription_tax_percent` — imposto sobre a mensalidade SaaS (lado Aumigão).
- `walker_tax_percent` — retenção/imposto provisionado sobre o repasse do passeador.

Campos estruturais NFS-e (usados só quando a emissão ligar; opcionais):
- `iss_percent`, `municipal_service_code`, `simples_nacional` (bool), `cnae`,
  `service_description`.

Meta: `id`, `tenant_id` (único, FK tenants.id), `active` (default true), `created_at`,
`updated_at`. Constraint de unicidade por `tenant_id`.

Resolução: se não houver linha para o tenant → tudo 0/None (provisão zero, NFS-e inerte).

### `payment_provision` (snapshot imutável; 1 por pagamento confirmado)
- `id` (PK), `tenant_id` (FK), `payment_id` (único — idempotência), `revenue_type`
  (`walk_commission` | `saas_subscription` | `tip`).
- Lado passeador: `walker_gross`, `walker_tax`, `walker_net`.
- Lado plataforma: `platform_gross`, `platform_tax`, `platform_net`.
- Alíquotas congeladas no momento: `walker_tax_percent_applied`,
  `platform_tax_percent_applied`.
- `created_at`.

Imutável: nunca reescrito após criado. Mudança de config NÃO altera linhas existentes.

## Cálculo (quando e como)

Gatilho: **na confirmação do pagamento** (`new_status == _PAYMENT_CONFIRMED_STATUS`), nos
ramos do webhook. Função central `compute_and_store_provision(db, payment, revenue_type)`:
1. Idempotência: se já existe `payment_provision` para `payment_id` → no-op.
2. Carrega `tenant_fiscal_config` do tenant (ou zeros se ausente).
3. Resolve as bases conforme `revenue_type`:
   - `walk_commission`: `platform_gross = payment.platform_amount`,
     `walker_gross = payment.walker_amount`; alíquota plataforma = `commission_tax_percent`.
   - `saas_subscription`: `platform_gross = payment.amount`, `walker_gross = 0`;
     alíquota plataforma = `subscription_tax_percent`.
   - `tip`: `walker_gross = payment.amount` (gorjeta é do passeador), `platform_gross = 0`;
     alíquota passeador = `walker_tax_percent`.
4. `*_tax = round(gross * percent/100, 2)`; `*_net = gross - tax`.
5. Grava a linha imutável com as alíquotas aplicadas.

Robustez: `platform_amount`/`walker_amount` podem ser nulos em pagamentos sem split
calculado → tratados como `0` (provisão zero daquele lado, sem erro). "Lado plataforma" =
a receita do **tenant/Aumigão** naquele pagamento (quem fica com a comissão depende do
modelo rede vs próprio; a config fiscal é do tenant, então a alíquota aplicada é a dele).

Execução: dentro da mesma transação do webhook (cálculo local puro, sem rede). Envolto em
guarda que **nunca quebra o processamento de pagamento** (loga e segue se falhar).

Backfill: script one-off que percorre `Payment` confirmados sem `payment_provision` e cria
o snapshot com a config atual do tenant. Idempotente.

## APIs admin (backend)

- `GET /admin/tenants/{tenant_id}/fiscal-config` — lê a config (gate `finance.read`).
- `PUT /admin/tenants/{tenant_id}/fiscal-config` — cria/atualiza (gate `finance.manage`),
  com `record_audit_log`. Usa `get_admin_tenant_scope` no topo (regra RLS de escrita admin).
- `GET /admin/tenants/{tenant_id}/financial-summary?from=&to=` — agregados no período:
  bruto total, reservado p/ imposto (plataforma e passeador), **líquido sacável** de cada
  lado, contagem. Soma sobre `payment_provision`.
- `GET /admin/payments/{payment_id}/provision` — extrato de um pagamento.

Escopo/segurança: super_admin vê qualquer tenant; admin de tenant só o próprio
(`get_admin_tenant_scope` + filtro), seguindo o padrão das demais rotas admin.

## Integração com saque (exibição)

Os saldos exibidos no admin (plataforma e passeador) passam a mostrar
`bruto → reservado imposto → líquido sacável`, lendo os agregados de `payment_provision`.
Como repasse/saque real é dormente, é camada de exibição; o saque real (quando construído)
consome o `*_net`.

## Segurança / comportamento

- Sem flag dedicada: provisão roda sempre, mas com alíquota **0% por default** → cria o
  extrato (imposto zero) sem efeito perceptível até o admin setar alíquotas.
- Migrations additivas (2 tabelas novas, sem RLS — segue o estilo das migrations 0058/0059).
- Zero mudança no fluxo de dinheiro; nada é movido.

## Unidades e fronteiras

- `tenant_fiscal_config` (model) + `fiscal_config_service` (resolução/CRUD + defaults zero).
- `payment_provision` (model) + `provision_service`
  (`compute_and_store_provision`, agregações para o summary).
- Hook no `asaas_webhook` chamando `compute_and_store_provision` (best-effort, idempotente).
- Rotas admin finas, delegando aos services.
- NFS-e: `nfse_service` passa a ler `tenant_fiscal_config` em vez de env (peça seguinte).

## Testes

- `fiscal_config_service`: defaults zero quando ausente; CRUD; unicidade por tenant.
- `provision_service`: cálculo por `revenue_type`; arredondamento; idempotência;
  imutabilidade (mudar config não altera linha existente); agregação do summary.
- Webhook: confirmação cria provisão; com config ausente → tudo zero; não quebra pagamento.
- Rotas admin: escopo (super_admin vs tenant), gates de permissão, audit log.

## Fora de escopo (peças seguintes, planos próprios)

- Tela admin-web (editar alíquotas + ver extrato/summary).
- Fiação dos params do `tenant_fiscal_config` na emissão de NFS-e (substituir env).
- Segregação física de dinheiro / split real / saque real (dependem do bloco de
  roteamento de caixa por tenant — dormente).
