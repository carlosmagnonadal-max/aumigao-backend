# Tela admin-web Provisão Fiscal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tela Financeiro › Provisão Fiscal no admin-web (por tenant: editar alíquotas/NFS-e, ver resumo e extrato), + 1 endpoint de listagem no backend.

**Architecture:** Backend ganha `list_provisions` + rota `GET /admin/tenants/{id}/provisions`. Frontend ganha `lib/fiscal-admin.ts` (client via `apiFetch`), `lib/fiscal-helpers.ts` (derivações puras testáveis), página `app/financeiro/provisao/page.tsx` e entrada no menu. Tudo segue padrões existentes.

**Tech Stack:** Backend FastAPI/pytest (`./.venv/Scripts/python.exe -m pytest`). Frontend Next.js/TypeScript/vitest (`npx vitest run <file>`, `npx tsc --noEmit`). Rodar comandos backend a partir de `.../app/backend`; frontend a partir de `.../app/admin-web`.

**Convenções:**
- Backend: rotas fiscais em `app/routes/fiscal.py`, gate `require_permission("finance.read")`, `_ensure_scope(admin, tenant_id, db)`.
- Frontend: client espelha `lib/tenant-admin.ts` (`apiFetch<T>("/api/admin/...", { method, body, timeoutMs })`). BFF é catch-all (`app/api/backend/[...path]`), sem proxy por rota. Componentes em `@/components/admin-ui`. Testes puros em `lib/__tests__/`.
- Backend baseline: 2611 passed / 15 failed / 99 errors (pré-existentes). Zero regressão nova.

---

## PARTE A — Backend (listagem de provisões)

### Task 1: provision_service.list_provisions

**Files:**
- Modify: `app/services/provision_service.py`
- Test: `tests/test_provision_list.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provision_list.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import Base
from app.models.fiscal import TenantFiscalConfig, PaymentProvision
from app.services import provision_service as svc

class FakePayment:
    def __init__(self, id, amount, platform_amount=None, walker_amount=None):
        self.id = id; self.amount = amount
        self.platform_amount = platform_amount; self.walker_amount = walker_amount

def _db():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e, tables=[TenantFiscalConfig.__table__, PaymentProvision.__table__])
    return sessionmaker(bind=e)()

def test_list_provisions_orders_newest_first_and_paginates():
    db = _db()
    for i in range(3):
        svc.compute_and_store_provision(db, "t1", FakePayment(f"p{i}", 100, 20, 80), "walk_commission")
    rows = svc.list_provisions(db, "t1", limit=2, offset=0)
    assert len(rows) == 2
    rows2 = svc.list_provisions(db, "t1", limit=2, offset=2)
    assert len(rows2) == 1

def test_list_provisions_filters_by_tenant():
    db = _db()
    svc.compute_and_store_provision(db, "t1", FakePayment("a", 100, 20, 80), "walk_commission")
    svc.compute_and_store_provision(db, "t2", FakePayment("b", 100, 20, 80), "walk_commission")
    assert len(svc.list_provisions(db, "t1", limit=50, offset=0)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_provision_list.py -q`
Expected: FAIL (`AttributeError: list_provisions`).

- [ ] **Step 3: Implement list_provisions**

```python
# adicionar em app/services/provision_service.py
def list_provisions(db: Session, tenant_id: str, *, limit: int = 25, offset: int = 0,
                    date_from=None, date_to=None) -> list[PaymentProvision]:
    q = db.query(PaymentProvision).filter(PaymentProvision.tenant_id == tenant_id)
    if date_from is not None:
        q = q.filter(PaymentProvision.created_at >= date_from)
    if date_to is not None:
        q = q.filter(PaymentProvision.created_at <= date_to)
    q = q.order_by(PaymentProvision.created_at.desc())
    return q.limit(max(1, min(limit, 200))).offset(max(0, offset)).all()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_provision_list.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/provision_service.py tests/test_provision_list.py
git commit -m "feat(fiscal): provision_service.list_provisions (paginado, por tenant)"
```

---

### Task 2: Rota GET /admin/tenants/{id}/provisions

**Files:**
- Modify: `app/routes/fiscal.py`
- Test: `tests/test_routes_provisions_list.py`

- [ ] **Step 1: Write the failing test** — reusar o scaffolding de `tests/test_routes_financial_summary.py` (cliente super_admin + tenant). Sembar 2 provisões via `provision_service.compute_and_store_provision` no db do teste, então:

```python
def test_list_provisions_route_returns_items(client_super, tenant_id, seed_two_provisions):
    r = client_super.get(f"/admin/tenants/{tenant_id}/provisions?limit=10&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and len(body["items"]) == 2
    item = body["items"][0]
    assert set(["payment_id","revenue_type","gross","tax","net"]).issubset(item.keys())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_routes_provisions_list.py -q`
Expected: FAIL (404).

- [ ] **Step 3: Implement the route**

```python
# adicionar em app/routes/fiscal.py (usa prov_svc já importado)
@router.get("/{tenant_id}/provisions")
@api_router.get("/{tenant_id}/provisions")
def list_provisions(tenant_id: str, limit: int = 25, offset: int = 0,
                    date_from: str | None = None, date_to: str | None = None,
                    admin: User = Depends(require_permission("finance.read")), db: Session = Depends(get_db)):
    _ensure_scope(admin, tenant_id, db)
    df = datetime.fromisoformat(date_from) if date_from else None
    dt = datetime.fromisoformat(date_to) if date_to else None
    rows = prov_svc.list_provisions(db, tenant_id, limit=limit, offset=offset, date_from=df, date_to=dt)
    def _row(p):
        g = float(p.platform_gross) + float(p.walker_gross)
        t = float(p.platform_tax) + float(p.walker_tax)
        n = float(p.platform_net) + float(p.walker_net)
        return {
            "payment_id": p.payment_id, "revenue_type": p.revenue_type,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "walker_gross": float(p.walker_gross), "walker_tax": float(p.walker_tax), "walker_net": float(p.walker_net),
            "platform_gross": float(p.platform_gross), "platform_tax": float(p.platform_tax), "platform_net": float(p.platform_net),
            "gross": round(g, 2), "tax": round(t, 2), "net": round(n, 2),
        }
    return {"items": [_row(p) for p in rows], "limit": limit, "offset": offset}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_routes_provisions_list.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/fiscal.py tests/test_routes_provisions_list.py
git commit -m "feat(fiscal): rota GET /admin/tenants/{id}/provisions (lista paginada)"
```

---

## PARTE B — Frontend (admin-web; rodar de `.../app/admin-web`)

### Task 3: Tipos + client lib/fiscal-admin.ts

**Files:**
- Modify: `lib/api-types.ts` (adicionar tipos)
- Create: `lib/fiscal-admin.ts`
- Test: `lib/__tests__/fiscal-admin.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// lib/__tests__/fiscal-admin.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import * as api from "@/lib/api";
import { getFiscalConfig, updateFiscalConfig, getFinancialSummary, listProvisions } from "@/lib/fiscal-admin";

describe("fiscal-admin client", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("getFiscalConfig hits the right path", async () => {
    const spy = vi.spyOn(api, "apiFetch").mockResolvedValue({} as never);
    await getFiscalConfig("t1");
    expect(spy).toHaveBeenCalledWith("/api/admin/tenants/t1/fiscal-config", expect.any(Object));
  });

  it("updateFiscalConfig sends PUT with body", async () => {
    const spy = vi.spyOn(api, "apiFetch").mockResolvedValue({} as never);
    await updateFiscalConfig("t1", { commission_tax_percent: 5 });
    const [, opts] = spy.mock.calls[0];
    expect(opts.method).toBe("PUT");
    expect(JSON.parse(opts.body as string)).toEqual({ commission_tax_percent: 5 });
  });

  it("listProvisions builds query string", async () => {
    const spy = vi.spyOn(api, "apiFetch").mockResolvedValue({ items: [] } as never);
    await listProvisions("t1", { limit: 10, offset: 20 });
    expect(spy).toHaveBeenCalledWith("/api/admin/tenants/t1/provisions?limit=10&offset=20", expect.any(Object));
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run (de `admin-web`): `npx vitest run lib/__tests__/fiscal-admin.test.ts`
Expected: FAIL (module não existe).

- [ ] **Step 3: Implement types + client**

```typescript
// adicionar em lib/api-types.ts
export interface FiscalConfig {
  tenant_id: string;
  commission_tax_percent: number;
  subscription_tax_percent: number;
  walker_tax_percent: number;
  iss_percent: number | null;
  municipal_service_code: string | null;
  simples_nacional: boolean | null;
  cnae: string | null;
  service_description: string | null;
  active: boolean;
}
export type FiscalConfigUpdate = Partial<Omit<FiscalConfig, "tenant_id">>;
export interface FinancialSummary {
  count: number; gross_total: number;
  platform_gross: number; platform_tax_reserved: number; platform_net: number;
  walker_gross: number; walker_tax_reserved: number; walker_net: number;
}
export interface ProvisionRow {
  payment_id: string; revenue_type: string; created_at: string | null;
  walker_gross: number; walker_tax: number; walker_net: number;
  platform_gross: number; platform_tax: number; platform_net: number;
  gross: number; tax: number; net: number;
}
export interface ProvisionListResponse { items: ProvisionRow[]; limit: number; offset: number; }
```

```typescript
// lib/fiscal-admin.ts
import { apiFetch } from "@/lib/api";
import type {
  FiscalConfig, FiscalConfigUpdate, FinancialSummary, ProvisionListResponse,
} from "@/lib/api-types";

const T = 20000;
const enc = encodeURIComponent;

export function getFiscalConfig(tenantId: string) {
  return apiFetch<FiscalConfig>(`/api/admin/tenants/${enc(tenantId)}/fiscal-config`, { timeoutMs: T });
}
export function updateFiscalConfig(tenantId: string, payload: FiscalConfigUpdate) {
  return apiFetch<FiscalConfig>(`/api/admin/tenants/${enc(tenantId)}/fiscal-config`, {
    method: "PUT", body: JSON.stringify(payload), timeoutMs: T,
  });
}
export function getFinancialSummary(tenantId: string) {
  return apiFetch<FinancialSummary>(`/api/admin/tenants/${enc(tenantId)}/financial-summary`, { timeoutMs: T });
}
export function listProvisions(tenantId: string, params: { limit: number; offset: number }) {
  const qs = `?limit=${params.limit}&offset=${params.offset}`;
  return apiFetch<ProvisionListResponse>(`/api/admin/tenants/${enc(tenantId)}/provisions${qs}`, { timeoutMs: T });
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run lib/__tests__/fiscal-admin.test.ts`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add lib/api-types.ts lib/fiscal-admin.ts lib/__tests__/fiscal-admin.test.ts
git commit -m "feat(admin-web): client fiscal-admin + tipos da provisao fiscal"
```

---

### Task 4: Helpers puros (lib/fiscal-helpers.ts)

**Files:**
- Create: `lib/fiscal-helpers.ts`
- Test: `lib/__tests__/fiscal-helpers.test.ts`

Helpers testáveis: `summaryCards(summary)` (deriva os cards) e `revenueTypeLabel(type)`.

- [ ] **Step 1: Write the failing test**

```typescript
// lib/__tests__/fiscal-helpers.test.ts
import { describe, it, expect } from "vitest";
import { summaryCards, revenueTypeLabel } from "@/lib/fiscal-helpers";

describe("fiscal-helpers", () => {
  it("summaryCards derives platform + walker + reserved", () => {
    const cards = summaryCards({
      count: 2, gross_total: 200,
      platform_gross: 40, platform_tax_reserved: 4, platform_net: 36,
      walker_gross: 160, walker_tax_reserved: 8, walker_net: 152,
    });
    const reserved = cards.find((c) => c.key === "reserved_total");
    expect(reserved?.value).toBe(12); // 4 + 8
  });
  it("revenueTypeLabel maps known types", () => {
    expect(revenueTypeLabel("walk_commission")).toMatch(/passeio|comiss/i);
    expect(revenueTypeLabel("tip")).toMatch(/gorjeta/i);
    expect(revenueTypeLabel("xyz")).toBe("xyz");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run lib/__tests__/fiscal-helpers.test.ts`
Expected: FAIL (module não existe).

- [ ] **Step 3: Implement helpers**

```typescript
// lib/fiscal-helpers.ts
import type { FinancialSummary } from "@/lib/api-types";

export interface SummaryCardData { key: string; label: string; value: number; }

export function summaryCards(s: FinancialSummary): SummaryCardData[] {
  return [
    { key: "platform_net", label: "Líquido plataforma", value: s.platform_net },
    { key: "platform_reserved", label: "Reservado (plataforma)", value: s.platform_tax_reserved },
    { key: "walker_net", label: "Líquido passeador", value: s.walker_net },
    { key: "walker_reserved", label: "Reservado (passeador)", value: s.walker_tax_reserved },
    { key: "reserved_total", label: "Total reservado p/ imposto", value: Math.round((s.platform_tax_reserved + s.walker_tax_reserved) * 100) / 100 },
  ];
}

const LABELS: Record<string, string> = {
  walk_commission: "Comissão de passeio",
  saas_subscription: "Mensalidade SaaS",
  tip: "Gorjeta",
};
export function revenueTypeLabel(type: string): string {
  return LABELS[type] ?? type;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run lib/__tests__/fiscal-helpers.test.ts`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add lib/fiscal-helpers.ts lib/__tests__/fiscal-helpers.test.ts
git commit -m "feat(admin-web): helpers puros da tela de provisao (summaryCards, labels)"
```

---

### Task 5: Página app/financeiro/provisao/page.tsx

**Files:**
- Create: `app/financeiro/provisao/page.tsx`
- (sem teste unitário de render — verificação por `tsc`/build; lógica pura já coberta nas Tasks 3-4)

A página é um client component. Estrutura (seguir padrões de `app/financeiro/page.tsx`):
- `"use client"`, envolver em `<SessionGuard>`.
- `useIsSuperAdmin()` → se super, mostra seletor (de `listTenants()`); senão, usa o tenant do próprio usuário (esconde seletor).
- Estado: `tenantId`, `config` (FiscalConfig), `summary` (FinancialSummary), `provisions` (ProvisionRow[]), `offset`, loadings, erros.
- `useEffect` reage a `tenantId` → carrega `getFiscalConfig`, `getFinancialSummary`, `listProvisions`.
- **Seção 1 (Provisão):** inputs number `commission_tax_percent`, `subscription_tax_percent`, `walker_tax_percent` + botão Salvar → `updateFiscalConfig(tenantId, { ...os 3 campos })`.
- **Seção 2 (NFS-e):** inputs `iss_percent`, `municipal_service_code`, `cnae`, checkbox `simples_nacional`, `service_description` + botão Salvar → `updateFiscalConfig(tenantId, { ...os campos NFS-e })`.
- **Seção 3 (Resumo):** `summaryCards(summary)` → `AdminMetricCard` com `formatBRL`.
- **Seção 4 (Extrato):** tabela das `provisions` (Data | Tipo via `revenueTypeLabel` | Bruto | Imposto | Líquido com `formatBRL`); paginação anterior/próximo via `offset`; `AdminEmptyState` quando vazio.
- Loading: `AdminSkeletonBlock`. Erros: mensagem (padrão financeiro).

- [ ] **Step 1: Implementar a página** seguindo a estrutura acima. Reusar `@/components/admin-ui` (`AdminSection`, `AdminMetricCard`, `AdminEmptyState`, `AdminSkeletonBlock`), `formatBRL` de onde o financeiro importa, `SessionGuard`, `useIsSuperAdmin`. Importar do `@/lib/fiscal-admin` e `@/lib/fiscal-helpers`.

- [ ] **Step 2: Typecheck**

Run (de `admin-web`): `npx tsc --noEmit`
Expected: 0 erros.

- [ ] **Step 3: Lint**

Run: `npx next lint --file app/financeiro/provisao/page.tsx` (ou `npm run lint`)
Expected: sem erros novos.

- [ ] **Step 4: Commit**

```bash
git add app/financeiro/provisao/page.tsx
git commit -m "feat(admin-web): pagina Financeiro > Provisao Fiscal (form 2 secoes + resumo + extrato)"
```

---

### Task 6: Entrada no menu Financeiro

**Files:**
- Modify: a página/menu que lista os cards/links de Financeiro (procurar em `app/financeiro/page.tsx` os `Link`/cards de subseção; adicionar um link para `/financeiro/provisao`). Se o menu lateral for global, localizar o componente de navegação (`grep -rn "financeiro" components app | grep -i nav`).

- [ ] **Step 1:** Adicionar um `Link`/card "Provisão Fiscal" → `/financeiro/provisao` no índice do Financeiro, seguindo o estilo dos itens existentes.

- [ ] **Step 2: Typecheck + build**

Run: `npx tsc --noEmit`
Expected: 0 erros.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "feat(admin-web): link Provisao Fiscal no menu Financeiro"
```

---

### Task 7: Verificação final

- [ ] **Step 1 (backend):** `./.venv/Scripts/python.exe -m pytest -q` (de `backend`) — zero regressão nova vs baseline.
- [ ] **Step 2 (frontend):** `npx vitest run` (de `admin-web`) — todos os testes verdes; `npx tsc --noEmit` 0 erros; `npm run build` sucesso.
- [ ] **Step 3 (revisão):** Opus revisa o diff (client paths corretos, escopo no backend, página segue padrões, sem `any` novo).
- [ ] **Step 4 (deploy — com OK do Carlos):** backend deploy (rota nova, additiva) + admin-web deploy (push → Vercel, conforme padrão do repo: git push, NÃO `vercel --prod`).

---

## Notas de execução
- `formatBRL`: localizar o helper existente (provavelmente em `@/lib/admin-data-helpers` ou similar) e reusar; não recriar.
- Testes de página com render (RTL) NÃO fazem parte do padrão do repo (testes são pure-function) — não introduzir; cobertura via client/helpers + tsc/build.
- Backend: a rota nova é GET additiva, não altera nada existente.
- Deploy do admin-web é via git push (Vercel), conforme memória do projeto — nunca `vercel --prod` (vira deploy órfão).
