-- 0047_missing_fk_indexes.sql
-- Equivalente SQL da migration Alembic 0047_missing_fk_indexes.
-- Cole e execute no Neon SQL Editor (role dono / neondb_owner).
-- Todas as instruções são idempotentes (IF NOT EXISTS).
--
-- Colunas verificadas vs. model ORM:
--   walks(pet_id)                     → SEM index=True no model → ADICIONADO
--   shared_walks(created_by_tutor_id) → index=True no model     → PULADO
--   complaints(target_pet_id)         → index=True no model     → PULADO
--   complaint_evidences(created_by_id)→ index=True no model     → PULADO
--   tutor_subscriptions(tutor_id)     → index=True no model     → PULADO
--   coupon_redemptions(user_id)       → index=True no model     → PULADO

CREATE INDEX IF NOT EXISTS ix_walks_pet_id ON walks (pet_id);
