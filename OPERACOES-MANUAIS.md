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

## Fase 2 - PII: vazamentos criticos confirmados em producao (auditoria + testes em aba anonima)

### ESTANCADO
- /api/walkers e /walker/public: removidos cpf, phone, email da resposta publica + removido fallback de avatar para selfie_url. Commit fix(backend): remove PII from public walker endpoints. Teste vivo tests/test_public_walker_pii.py com allowlist (falha se qualquer campo fora da lista publica aparecer). Validado em aba anonima.

### CRITICO AINDA ABERTO - PRIORIDADE MAXIMA PROXIMA SESSAO

#### 1. /uploads/* - documentos sensiveis publicos (CONFIRMADO abrindo URL sem login)
- StaticFiles montado em app/main.py:294 servindo TODO o diretorio uploads/ sem auth.
- DOIS vetores:
  - LEITURA: documentos (identity_front/back, selfie, address_proof) em uploads/walker-documents/{owner_id}/ acessiveis por URL publica sem login.
  - ESCRITA: endpoint upload_partner_application_document (walker.py:739) SEM autenticacao; owner_id vem do FORM do cliente, nao de token. Qualquer um faz upload e escolhe a pasta.
- Estrutura: documentos sensiveis e imagens publicas (foto de perfil, foto de pet) no MESMO diretorio raiz uploads/. profile_photo fica junto com os documentos em walker-documents/.
- CONSUMO: mobile usa /uploads para foto de perfil e foto de pet (sem token, via <Image src>). admin-web exibe documentos sensiveis via URL publica direta (sem token, via <img src>) em _serialize_walker_profile (admin.py:584).
- CORRECAO exige: mover documentos sensiveis para fora do StaticFiles + endpoint autenticado para o admin ver documentos + autenticar o upload (CUIDADO: pode ser cadastro pre-login, owner_id=anonymous sugere isso - confirmar antes) + MIGRATION de arquivos ja em producao + reescrita das URLs no banco + MUDANCA NO ADMIN-WEB (carregar imagem com token, nao trivial em <img>).
- NATUREZA: NAO e correcao so de backend nem de uma sessao. E mini-sprint coordenada backend + admin-web + migration manual em producao com PII. Planejar, nao improvisar.

#### 2. GET /payments/{payment_id} - pagamento publico sem auth
- payments.py:284, sem get_current_user. Retorna tutor_id, walk_id, valor, status, provider payment id.
- Confirmar PORQUE e publico (link de pagamento? callback Asaas?) antes de exigir auth, para nao quebrar fluxo de pagamento real.

#### 3. GET /walker/dashboard next_request - endereco/notas antes do aceite
- walker.py dashboard + _walk_payload: passeador ve address_snapshot e notas de passeio disponivel ANTES de aceitar. Permite coleta de dados sem assumir a corrida.
- Correcao: mascarar endereco/notas ate o aceite. E logica de negocio - entender fluxo de aceite antes de mexer.

### POTENCIAL VAZAMENTO MULTI-TENANT (revisar depois dos 3 criticos)
- Varias rotas admin (complaints, occurrences, walker_operations, referrals) listam PII SEM filtro de tenant. Complaint/Occurrence parecem nao ter coluna tenant_id. Tenant A pode ver PII do Tenant B. Quebra do modelo white-label, nao so PII.

### HIGIENE (baixa prioridade)
- walker_operations (admin.py:1841) retorna ORM direto sem response_model.
- get_profile usa **profile.__dict__.
- Logs de candidatura/pagamento ainda com nome/email (tokens ja mascarados).
