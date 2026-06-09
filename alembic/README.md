# Migrations (Alembic) — Aumigão backend

O banco de produção (Neon) foi criado por **DDL manual** antes do Alembic. Esta
pasta introduz versionamento de schema de forma **reversível e sem quebrar nada**
(ver protocolo em `docs/RECONCILIACAO-SPEC-CODIGO.md §8`).

## Estado atual
- `0001_baseline` — migration vazia (no-op) que representa o schema já existente.
- A URL do banco vem de `app.core.database._database_url()` (env `DATABASE_URL`);
  **não** está hardcoded no `alembic.ini`.
- `env.py` importa `app.models` → todos os 32 modelos entram em `Base.metadata`.

## Ativação (passo que TOCA o banco — fazer com rede de proteção)
> ⚠️ Antes de qualquer comando que escreva no banco, **criar um branch no Neon**
> (ou backup) para rollback imediato. Não há staging; o banco é compartilhado.

```bash
# 1. Marca o baseline SEM rodar DDL de schema (cria só a tabela alembic_version):
alembic stamp 0001_baseline

# 2. Conferir:
alembic current        # deve apontar 0001_baseline
```

## Fluxo de uma nova migration (ex.: M5 — tenant_id)
```bash
# Gera o diff entre os modelos e o banco (apenas LÊ o schema):
alembic revision --autogenerate -m "add tenant_id to tenant-scoped tables"
# -> REVISAR o arquivo gerado à mão antes de aplicar.
# Regras de não-quebra:
#   - coluna nova entra nullable; backfill; só então NOT NULL/constraint.
#   - toda migration tem downgrade() testado.
#   - migration destrutiva (drop/rename) é PROIBIDA nesta fase.
alembic upgrade head   # aplica (com branch Neon ativo)
alembic downgrade -1   # rollback se necessário
```

## Próximas migrations previstas (Fase 1 / Sprint 13)
1. **M5** — `tenant_id` (nullable + index) em: `payments`, `walk_reviews`,
   `walker_reviews`, `walk_tips`, `walk_completion_reviews`, `complaints`
   (+ tabelas filhas). Backfill a partir do `walk`/`tutor` pai. **Só adicionar o
   campo aos modelos SQLAlchemy DEPOIS que a coluna existir no banco** — senão o
   ORM referencia coluna inexistente e quebra.
2. Índices faltantes (M3): `walk.walker_id`, `walk.status`, `walker_profile.status`.
