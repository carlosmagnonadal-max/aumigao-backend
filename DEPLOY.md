# Deploy do backend (Railway) — runbook

> O Railway faz **auto-deploy da branch `main`** (push na main = deploy). Porém o
> **Procfile não roda migrations** (`web: uvicorn ...`) e o `schema ensure` do
> startup é **desligado em produção**. Logo: **migrations são MANUAIS**. Aplicar
> as migrations ANTES de subir o código é obrigatório.

## Regra de ouro: migrar primeiro, deployar depois (padrão expand)
As migrations do projeto são **aditivas** (CREATE TABLE de tabelas novas + `ADD
COLUMN IF NOT EXISTS`). Por serem aditivas, aplicá-las **antes** do código novo é
seguro: o código antigo que está no ar ignora colunas/tabelas novas e continua
funcionando. Se subir código novo contra um DB sem as colunas/tabelas → **500**.

## Passo a passo (a partir de `app/backend/`)
1. **Conferir onde prod está** (read-only):
   ```bash
   .venv/Scripts/python -m alembic current      # ex.: 0010_payment_split
   .venv/Scripts/python -m alembic heads         # DEVE ser uma única head
   ```
   ⚠️ Se aparecer **mais de uma head**, NÃO migre — a árvore bifurcou (duas
   migrations com o mesmo `down_revision`). Reencadear antes (lição 2026-06-09:
   `0011_recurring_plans` colidiu com `0011_upload_files`; corrigido apontando
   uma após a outra).
2. **Aplicar as migrations em produção**:
   ```bash
   .venv/Scripts/python -m alembic upgrade head
   .venv/Scripts/python -m alembic current       # deve mostrar a head esperada
   ```
3. **Deployar o código**: merge da branch de feature na `main` (fast-forward) e
   push. O Railway detecta o push e sobe.
   ```bash
   git checkout main && git merge --ff-only <branch> && git push origin main
   ```
4. **Validar pela API pública** (sem precisar dos logs do Railway):
   ```bash
   curl -s https://aumigao-backend-production.up.railway.app/health
   # endpoint novo deve EXISTIR (401/403 sem token é OK; 404 = não deployou)
   curl -s -o /dev/null -w "%{http_code}" \
     https://aumigao-backend-production.up.railway.app/api/recurring-plans
   ```

## Rollback
- **Código:** reverter o merge na `main` e push (Railway re-deploya a versão anterior).
- **Migrations (aditivas):** `alembic downgrade <rev_anterior>` derruba as tabelas/
  colunas novas. Como são aditivas e gated por feature flag (default off), o
  impacto de manter aplicadas é nulo — geralmente não precisa reverter o schema.

## Por que as features são seguras de subir
Toda feature nova (planos recorrentes, Pet Tour, passeios compartilhados) é
**gated por feature flag por tenant** (default **off**). Subir o código **não muda
comportamento** de nenhum tenant até alguém ligar a flag no admin-web.
