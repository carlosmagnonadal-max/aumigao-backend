# Tela admin-web: Financeiro › Provisão Fiscal — Design (2026-06-26)

## Objetivo

Tela no admin-web para, por tenant: editar as alíquotas de provisão + os parâmetros
fiscais de NFS-e, ver o resumo financeiro (bruto → reservado imposto → líquido sacável) e
o extrato de divisão por pagamento. Consome o backend de provisão fiscal já no ar
(rev `00093-zs7`).

Decisões (Carlos, 2026-06-26):
- Mora em **Financeiro › Provisão Fiscal** (entrada no menu) com **seletor de tenant**.
- v1 inclui **alíquotas + resumo + extrato por pagamento**.
- Form com **duas seções SEPARADAS**: "Provisão (impostos)" e "NFS-e (emissão)".

## Contexto do código existente (admin-web)

- `lib/api.ts`: `apiFetch<T>(path, options)` — BFF proxy, só paths relativos `/api/...`.
- `lib/tenant-admin.ts`: `listTenants()`, `getTenant()`, `updateTenant()` (padrão de client).
- `app/financeiro/page.tsx`: client component com `SessionGuard`, componentes `admin-ui`
  (`AdminSection`, `AdminMetricCard`, `AdminEmptyState`, `AdminSkeletonBlock`,
  `AdminStatusChip`), `useIsSuperAdmin`, padrão resilient-api (`getResult`).
- BFF: `app/api/...` (proxy). Confirmar no plano se há catch-all ou se cada rota precisa
  de um handler de proxy próprio.

## Adição no backend (necessária para o extrato)

Hoje existem `GET /admin/tenants/{id}/financial-summary` (agregado) e
`GET /admin/payments/{id}/provision` (um pagamento). Falta **listagem**:
- `provision_service.list_provisions(db, tenant_id, *, limit, offset, date_from, date_to)
  -> list[PaymentProvision]` (mais recentes primeiro).
- Rota `GET /admin/tenants/{id}/provisions?limit=&offset=&from=&to=` em `app/routes/fiscal.py`
  (gate `finance.read`, `_ensure_scope`), retorna `{items: [...], limit, offset}`.
  Cada item: payment_id, revenue_type, created_at, walker_gross/tax/net,
  platform_gross/tax/net, e derivados `gross = walker_gross+platform_gross`,
  `tax = walker_tax+platform_tax`, `net = walker_net+platform_net`.

## Frontend

### Arquivos
- `app/financeiro/provisao/page.tsx` — página `"use client"` com `SessionGuard`.
- `lib/fiscal-admin.ts` — client de API:
  - `getFiscalConfig(tenantId)` → `GET /api/admin/tenants/{id}/fiscal-config`
  - `updateFiscalConfig(tenantId, payload)` → `PUT .../fiscal-config`
  - `getFinancialSummary(tenantId)` → `GET .../financial-summary`
  - `listProvisions(tenantId, {limit, offset})` → `GET .../provisions`
- BFF proxy: rotas de proxy para os 4 paths (ou catch-all — confirmar no plano).
- Entrada no menu Financeiro apontando para `/financeiro/provisao`.
- Tipos em `lib/api-types.ts` (FiscalConfig, FinancialSummary, ProvisionRow).

### Layout (uma página, seções empilhadas)
```
Financeiro › Provisão Fiscal          Tenant: [ Aumigão ▼ ]

[ Seção 1 — Provisão (impostos) ]   editar
  Comissão %   Mensalidade %   Passeador %        [ Salvar ]

[ Seção 2 — NFS-e (emissão) ]   editar
  ISS %   Cód serviço municipal   CNAE
  Simples Nacional [x]   Descrição do serviço      [ Salvar ]

[ Seção 3 — Resumo do período ]
  PLATAFORMA  bruto · reservado · líquido
  PASSEADOR   bruto · reservado · líquido
  Total reservado p/ imposto

[ Seção 4 — Extrato por pagamento ]  (tabela paginada, empty state)
  Data | Tipo | Bruto | Imposto | Líquido
```

As duas seções do form salvam pela MESMA rota PUT `/fiscal-config` (campos parciais via
`exclude_unset`), mas são blocos visuais e de submit independentes — editar provisão não
exige mexer em NFS-e e vice-versa.

### Estado / fluxo
- Seletor de tenant: `listTenants()`; super_admin escolhe; tenant-admin travado no próprio
  (via `useIsSuperAdmin` — se não for super, usa o tenant do próprio usuário e esconde o
  seletor). Ao trocar de tenant → recarrega config + summary + extrato.
- Form: estado controlado; "Salvar" chama `updateFiscalConfig` (só os campos da seção),
  toast de sucesso/erro; recarrega após salvar.
- Resumo: `getFinancialSummary`; cards `AdminMetricCard` com `formatBRL`.
- Extrato: `listProvisions` com paginação simples (limit 25, botões anterior/próximo);
  `AdminEmptyState` quando vazio.
- Resiliência/erros: seguir o padrão de `financeiro/page.tsx` (resilient-api/getResult,
  skeleton no loading, mensagem de erro).

### Validação
- Percentuais: `0..100`, número. Campos NFS-e opcionais (texto/checkbox). Reusar máscara/
  helper de número existente se houver; senão input number simples.

## Testes (vitest)
- `lib/fiscal-admin.ts`: cada função chama o path certo com o método/body certo (mock de apiFetch).
- Página: render com mock — troca de tenant recarrega; salvar provisão chama PUT só com os
  campos da seção; resumo exibe valores; extrato vazio mostra empty state.
- Backend (pytest): `list_provisions` (ordem, paginação, filtro de tenant/escopo) + rota.

## Fora de escopo
- Fiação dos params do `tenant_fiscal_config` na emissão de NFS-e (peça separada).
- Filtro de período avançado (v1 = tudo; paginação no extrato basta).
- Edição de provisão de pagamento individual (read-only no extrato).
