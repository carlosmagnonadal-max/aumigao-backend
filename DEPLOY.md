# Deploy do backend (Cloud Run + Neon) — runbook

> O backend roda no **Google Cloud Run** (serviço `aumigao-backend`, região
> `southamerica-east1`, projeto `aumigao-499206`). O banco é **Neon** (Postgres
> serverless). **NÃO há deploy automático** no push da `main` — o deploy é manual
> via `gcloud run deploy`. As **migrations também são manuais** e devem ser
> aplicadas no Neon ANTES de subir o código novo.

## Regra de ouro: migrar primeiro, deployar depois (padrão expand)
As migrations são **aditivas/tolerantes a drift** (`ADD COLUMN IF NOT EXISTS`,
`CREATE TABLE`/`CREATE INDEX IF NOT EXISTS`). Aplicá-las **antes** do código novo
é seguro: o código antigo no ar ignora colunas/tabelas novas. Subir código novo
contra um DB sem as colunas → risco de erro. Funcionalidades novas ficam atrás de
**feature flag** (default off), então o deploy do código não muda comportamento
até a flag ser ligada.

## Pré-requisitos
- `gcloud` autenticado na conta com acesso ao projeto (`gcloud auth list`).
- Acesso ao **Neon SQL Editor** como **dono** (`neondb_owner`) OU a URL do dono.
  ⚠️ O role de runtime da app (`aumigao_app`) é **non-owner + RLS** e **NÃO tem
  privilégio** para `ALTER TABLE` / `CREATE`/`DROP INDEX`. DDL de migration exige
  o dono.

## Passo 1 — Aplicar as migrations no Neon (dono)
Duas formas equivalentes:

**(A) Alembic com a URL do dono** (a partir de `app/backend/`, PowerShell):
```powershell
$env:DATABASE_URL = "<URL do neondb_owner>"
.venv/Scripts/python -m alembic current      # confere a head atual
.venv/Scripts/python -m alembic upgrade head  # aplica até a última
.venv/Scripts/python -m alembic current      # confirma a nova head
```

**(B) SQL puro no Neon SQL Editor** (quando não se quer expor a URL do dono):
traduza a migration nova para SQL idempotente e rode no editor, **estampando o
alembic** ao final para não bifurcar o histórico:
```sql
-- ... DDL da migration (ADD COLUMN IF NOT EXISTS, CREATE/DROP INDEX, backfill) ...
UPDATE alembic_version
  SET version_num = '<nova_revision>'
  WHERE version_num = '<revision_anterior>';
```
> Cole **apenas SQL** no editor — qualquer texto em prosa quebra (`syntax error`).
> `alembic heads` deve ser **uma única head**; se houver duas, a árvore bifurcou —
> reencadear antes de migrar.

## Passo 2 — Deploy do código no Cloud Run
A partir de `app/backend/` (tem o `Dockerfile`):
```bash
gcloud run deploy aumigao-backend --source . --region southamerica-east1
```
Faz build do `Dockerfile` via Cloud Build e cria uma nova revisão (`00NNN-xxx`),
**preservando env vars e secrets** já configurados no serviço. Roteia 100% do
tráfego para a nova revisão ao final.

> Deploy de produção é uma ação de alto impacto — confirme a intenção antes de
> rodar.

## Passo 3 — Validar
```bash
curl -s https://aumigao-backend-sso5ml3htq-rj.a.run.app/health
# esperado: {"status":"ok","db":"ok"}
```

## Rollback
- **Código:** roteie o tráfego de volta para a revisão anterior (instantâneo, sem
  rebuild):
  ```bash
  gcloud run revisions list --service aumigao-backend --region southamerica-east1
  gcloud run services update-traffic aumigao-backend \
    --region southamerica-east1 --to-revisions=<REV_ANTERIOR>=100
  ```
- **Migrations (aditivas):** `alembic downgrade <rev_anterior>` (com a URL do dono)
  ou o SQL inverso no editor. Como são aditivas e gated por flag (default off), o
  impacto de manter aplicadas costuma ser nulo — geralmente não precisa reverter o
  schema. ⚠️ Migrations que afrouxam unicidade (ex.: trocar um índice único por um
  mais amplo) podem falhar no downgrade se já houver dados que violem o índice
  antigo.

## Uploads (armazenamento de arquivos) — atenção
O filesystem do Cloud Run é **efêmero** (perde-se a cada revisão/instância) e o
serviço pode escalar para múltiplas instâncias. Uploads (fotos de pet, documentos
KYC, fotos de finalização) **não devem** depender do disco local em produção —
confirmar que estão em **object storage** (GCS/S3/R2) antes de tratar uploads como
duráveis. O caminho é centralizado em `app/services/signed_uploads.py`
(`UPLOAD_ROOT`).

---
_Histórico: o backend já rodou no Railway (auto-deploy da `main`); essa
infraestrutura foi aposentada. Este runbook descreve o fluxo atual (Cloud Run +
Neon)._
