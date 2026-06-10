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
   As migrations da Onda 1 são **idempotentes/tolerantes a drift**: cada `create_table`
   só cria se a tabela faltar e as colunas usam `ADD COLUMN IF NOT EXISTS`. Logo, mesmo
   que o `schema ensure` (create_all) já tenha criado tabelas, o `upgrade head` roda
   limpo (pula o que existe, adiciona o que falta). Seguro até re-rodar.
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

## Armazenamento de uploads (volume persistente) — IMPORTANTE
O backend grava uploads (fotos de pet, documentos KYC do passeador, fotos de
finalização) em **disco** sob a raiz `UPLOAD_ROOT`. O filesystem do Railway é
**efêmero**: sem um volume, **todo arquivo é perdido a cada deploy/restart** (o
banco mantém o `storage_path`, mas o arquivo some → imagem quebrada).

**Correção (volume persistente):**
1. No Railway, no serviço do backend, crie um **Volume** e monte num caminho, ex. `/data`.
2. Defina a env **`UPLOADS_DIR=/data/uploads`**.
3. Redeploy. O backend cria o diretório e passa a gravar lá
   (`pet-photos/`, `walker-documents/`, `walk-completions/`).

Sem `UPLOADS_DIR`, o default é `./uploads` na raiz do backend (efêmero — só dev/local).
O caminho é centralizado em `app/services/signed_uploads.py` (`UPLOAD_ROOT`); pets.py,
walker.py e o `serve_upload` derivam dele. Arquivos gravados ANTES do volume (no FS
efêmero) não são migrados — provavelmente já se perderam.

> Evolução futura (opcional): migrar para object storage com CDN (Cloudinary/S3/R2)
> se precisar de múltiplas instâncias do backend, CDN ou transformação de imagem.
> O front já tem scaffolding de Cloudinary (`lib/walkerDocumentStorage.ts`, hoje só demo).
