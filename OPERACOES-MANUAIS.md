# Operações Manuais em Produção (Neon) — Fase 1

## 2026-06-06 — Saneamento de walkers
Desativados como walker (status='submitted', active_as_walker=false) por serem dados de teste:
- goku (goku@gmail.com)
- Agora (agora@gmail.com)
- Prometheus (prometenus@gmail.com)
- Maluco beleza (maluquinho@gmail.com)
- Luluzinha (magno@magno.comm)
Walkers reais mantidos ativos: Luiza Nunes, Natália.

## 2026-06-06 — Criação de tenant_walker_access
- Criada a tabela tenant_walker_access em produção (DDL manual, espelhando o modelo).
- Vinculados ao tenant 'aumigao' (status='active'): Luiza Nunes, Natália.
- Validação: 2 linhas, 2 ativas, 0 walkers órfãos.

NOTA: produção não tem migrations versionadas. Estas mudanças foram feitas
manualmente no SQL Editor do Neon. Se adotar Alembic no futuro, documentar
como baseline.

## 2026-06-06 — Fase 1: smoke test do matching (parcial)
Deploy ed932a9 ATIVO em produção (Railway), app Online.
Validação INDIRETA feita: Luiza e Natália satisfazem todas as condições do
matching fail-closed (status=active, active_as_walker=true, tenant_access=active no aumigao).
PENDENTE: teste direto ponta-a-ponta (criar passeio -> matching -> aceite -> efetivação),
não feito por causa da janela de horário dos passeios. Validar quando possível.

## Fase 2 - Performance Admin Dashboard

- /api/admin/dashboard deixou de executar _refresh_reliability_events no GET; deteccao de eventos fica com o scheduler operacional (limite ~100 walks por ciclo; revisar quando crescer).
- Dashboard passou a usar preload em lote (User, Pet, WalkerProfile via IN) para checagem real/fake de walks, removendo N+1. Logica real/fake preservada. Assume 1 WalkerProfile por usuario.
- PENDENTE (infra, nao codigo): dashboard ainda apresenta timeout INTERMITENTE (funciona 14:34 e 14:43, falha 14:38, mesmo codigo). Suspeita: Railway Degraded Storage Performance e/ou cold start do backend. Decisao de infraestrutura (plano Railway / manter backend quente), nao de codigo. Nao reverter as correcoes de performance.
