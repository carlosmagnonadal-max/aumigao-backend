# Relatório de Auditoria — Backend Aumigão
**Data:** 2026-06-05  
**Auditor:** Claude Code (leitura estática + análise lógica)  
**Escopo:** FastAPI + SQLAlchemy, SaaS multi-tenant, beta fechado  
**Status:** Somente leitura — nenhuma correção implementada

---

## Sumário Executivo

O backend apresenta **5 achados críticos** que precisam ser endereçados antes de qualquer expansão de tenants ou exposição pública. Os mais graves são endpoints de administração que retornam dados de todos os tenants sem filtragem, rotas de candidatura de walker sem autenticação alguma, e um webhook de pagamento que pode ser falsificado por qualquer requisição HTTP. Há também um `.env` com credenciais em texto puro que, se estiver em repositório git compartilhado, compromete integralmente a aplicação.

---

## Índice de Achados

| ID | Severidade | Título | Arquivo(s) principal |
|----|-----------|--------|----------------------|
| C1 | CRÍTICO | Admin endpoints sem filtro de tenant | `app/routes/admin.py` |
| C2 | CRÍTICO | Rotas partner_router sem autenticação | `app/routes/walker.py` |
| C3 | CRÍTICO | Webhook Asaas sem validação de assinatura | `app/routes/payments.py` |
| C4 | CRÍTICO | Credentials hardcoded / .env exposto | `.env`, `app/core/security.py` |
| C5 | CRÍTICO | Race condition na aceitação de walk | `app/routes/walker.py` |
| A1 | ALTO | Aprovação de walker não atômica | `app/routes/walker.py` |
| A2 | ALTO | CORS wildcard + credentials=True | `app/main.py` |
| A3 | ALTO | JWT fraco sem rotação | `app/core/security.py` |
| A4 | ALTO | Sem rate limiting | toda a aplicação |
| A5 | ALTO | Duas implementações conflitantes de get_current_user | `app/core/deps.py`, `app/dependencies/auth.py` |
| A6 | ALTO | Limits de plano não enforçados via admin | `app/services/tenant_plan_service.py` |
| A7 | ALTO | walks.py sem filtro de tenant para admins | `app/routes/walks.py` |
| M1 | MÉDIO | N+1 massivo em listagem de walkers | `app/routes/walker.py` |
| M2 | MÉDIO | Admin dashboard carrega tabelas inteiras | `app/routes/admin.py` |
| M3 | MÉDIO | Índices faltando em colunas críticas | `app/models/walk.py`, `app/models/walker_profile.py` |
| M4 | MÉDIO | Dados sensíveis em resposta de erro | `app/routes/payments.py` |
| M5 | MÉDIO | Modelos sem tenant_id | múltiplos `app/models/` |
| M6 | MÉDIO | Tenant default como fallback | `app/middleware/tenant_resolver.py` |
| B1 | BAIXO | Sem índice composto em WalkMatchingAttempt | `app/models/walk.py` |
| B2 | BAIXO | PII em texto puro (CPF, telefone) | modelos de User/WalkerProfile |
| B3 | BAIXO | Seed de admin com senha fraca | `app/services/admin_seed_service.py` |
| B4 | BAIXO | admin.py com 1800+ linhas | `app/routes/admin.py` |

---

## CRÍTICOS

### C1 — Admin endpoints sem filtro de tenant

**Arquivo:** `app/routes/admin.py`  
**Linhas afetadas:** 888, 891, 974, 1032, 1070, 1194, 1491, 1784 (entre outras)

**Problema:**  
A maioria dos endpoints administrativos executa queries sem filtrar por `tenant_id`. Exemplos diretos:

```python
# Linha 974 — rota GET /admin/users
def users(db: Session = Depends(get_db)):
    return db.query(User).all()               # retorna TODOS os usuários da plataforma

# Linha 888–894 — dashboard
real_clients = [user for user in db.query(User).all() ...]
real_pets    = [pet  for pet  in db.query(Pet).all()  ...]
real_walks   = [walk for walk in db.query(Walk).all() ...]

# Linha 1491 — pagamentos
db.query(Payment).all()
```

O middleware `tenant_resolver.py` resolve o tenant e o armazena em `request.state.tenant_id`, mas as rotas de admin simplesmente não o utilizam.

**Risco concreto:**  
Um admin do Tenant A (ex: petshop "Rex") que souber a URL pode chamar `/admin/users` e receber nome, e-mail, telefone e endereço de todos os clientes do Tenant B (ex: petshop "Patinhas"). Em uma auditoria LGPD isso é violação direta. Em um cenário de ataque, basta comprometer um único admin.

**Correção recomendada:**  
Extrair `tenant_id` do usuário autenticado (ou de `request.state`) e adicionar `.filter(User.tenant_id == tenant_id)` em todas as queries de admin. Criar um helper `admin_tenant_filter(request)` para evitar que o erro se repita.

---

### C2 — Rotas partner_router sem autenticação

**Arquivo:** `app/routes/walker.py`  
**Linhas afetadas:** 862, 867, 875, 892

**Problema:**  
O `partner_router` monta rotas de gerenciamento de candidaturas de walkers sem nenhuma dependency de autenticação:

```python
@partner_router.get("")                                    # ln 862
def list_partner_applications(db: Session = Depends(get_db)):
    # Nenhum Depends(get_current_user) — público!
    return [_serialize_partner_application(p, db) for p in db.query(WalkerProfile).all()]

@partner_router.patch("/{candidate_id}/status")           # ln 875
def update_partner_application_status(candidate_id: str, payload: ..., db: ...):
    # Qualquer POST aqui aprova ou rejeita um walker
    ...

@partner_router.patch("/{candidate_id}/admin-fields")     # ln 892
def update_partner_application_admin_fields(...):
    # Campos internos de admin, sem autenticação
    ...
```

**Risco concreto:**  
1. `GET /api/partner-applications` expõe nomes completos, e-mails, CPF, telefone, fotos e histórico de todos os candidatos a walker (violação de PII / LGPD).  
2. `PATCH /api/partner-applications/{id}/status` com `{"status": "active"}` ativa qualquer walker sem passar pelo fluxo de aprovação — um agente malicioso pode criar um perfil, aprovar a si mesmo e começar a atender passeios.  
3. Qualquer competidor pode enumerar e rejeitar todos os walkers pendentes, paralisando operações.

**Correção recomendada:**  
Adicionar `Depends(require_admin)` (ou `Depends(require_super_admin)`) em todas as rotas do `partner_router`. O padrão já existe em outras partes do código.

---

### C3 — Webhook Asaas sem validação de assinatura

**Arquivo:** `app/routes/payments.py`  
**Linhas:** 291–301

**Problema:**  
```python
@router.post("/webhooks/asaas")
def asaas_webhook(payload: dict, db: Session = Depends(get_db)):
    event = payload.get("event")
    provider_payment_id = (payload.get("payment") or {}).get("id")
    if provider_payment_id:
        payment = db.query(Payment).filter(
            Payment.provider_payment_id == provider_payment_id
        ).first()
        if payment:
            payment.status = STATUS_BY_WEBHOOK_EVENT.get(event, ...)
            db.commit()
    return {"ok": True}
```

Não há:
- Verificação de token de autorização no header
- Validação de assinatura HMAC (Asaas envia `asaas-access-token` no header)
- Checagem de IP de origem
- Rate limiting

**Risco concreto:**  
Qualquer pessoa que conheça o `provider_payment_id` de um pagamento pode enviar um POST forjado e mudar o status para "confirmado", fazendo o sistema liberar um passeio sem pagamento real. O `provider_payment_id` pode ser enumerado ou obtido via outros vazamentos.

**Correção recomendada:**  
Validar o header `asaas-access-token` contra o valor configurado em variável de ambiente:
```python
expected = os.getenv("ASAAS_WEBHOOK_TOKEN")
received = request.headers.get("asaas-access-token")
if not secrets.compare_digest(expected or "", received or ""):
    raise HTTPException(status_code=401)
```

---

### C4 — Credentials hardcoded / .env exposto

**Arquivos:** `.env`, `app/core/security.py` (ln 14), `app/services/admin_seed_service.py` (ln 16–27)

**Problema:**  
O arquivo `.env` na raiz do projeto contém:

```
DATABASE_URL=postgresql+psycopg2://...:<senha_real>@ep-bold-sea-acj401ed.sa-east-1.aws.neon.tech/...
JWT_SECRET="aumigao-super-secret-key-2026-strong-long"
ADMIN_EMAIL=admin@aumigao.com
ADMIN_PASSWORD=Admin@123
SUPER_ADMIN_EMAIL=superadmin@aumigao.com
SUPER_ADMIN_PASSWORD=SuperAdmin@123
STRIPE_API_KEY=sk_test_emergent
ASAAS_SANDBOX_API_KEY=<token>
```

Além disso, `app/core/security.py` ln 14 tem um valor default hardcoded caso a variável de ambiente não esteja definida:
```python
SECRET_KEY = os.getenv("JWT_SECRET", "aumigao-dev-secret-key-with-more-than-32-bytes")
```

E `admin_seed_service.py` cria usuários admin com as senhas acima a cada startup quando `RUN_STARTUP_ADMIN_SEED=true`.

**Risco concreto:**  
Se o `.env` estiver (ou já esteve) em qualquer repositório git, qualquer pessoa com acesso ao histórico pode:
1. Fazer login como super admin imediatamente
2. Conectar diretamente ao banco PostgreSQL
3. Assinar JWTs arbitrários (qualquer usuário, qualquer role)
4. Consumir créditos da conta Asaas

**Correção recomendada:**  
- Adicionar `.env` ao `.gitignore` e auditar o histórico git com `git log --all -- .env`
- Se já foi commitado: rodar `git filter-repo` para remover do histórico, e **rotacionar todas as credentials imediatamente**
- Remover o valor default de `SECRET_KEY` — se a variável não estiver definida, a aplicação deve falhar ao subir, não silenciosamente usar um secret previsível
- Substituir senhas fracas (`Admin@123`) por senhas geradas aleatoriamente via secrets manager

---

### C5 — Race condition na aceitação de walk sem lock pessimista

**Arquivo:** `app/routes/walker.py`  
**Linhas:** 1573–1585

**Problema:**  
```python
def accept_walk(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = db.get(Walk, walk_id)              # leitura sem lock
    # ...
    accepted = db.query(Walk).filter(         # checagem de conflito de agenda
        Walk.walker_id == user.id,
        Walk.status.in_([...])
    ).all()
    if _has_schedule_conflict(walk, accepted, 15):
        raise HTTPException(409, "Conflito de horário")
    accept_operational_walk(walk, user, db)   # atribui o walk ao walker
    db.commit()
```

Não há `SELECT ... FOR UPDATE` na leitura do walk nem transação que impeça leituras simultâneas.

**Risco concreto:**  
Se dois walkers enviarem `POST /walks/{id}/accept` ao mesmo tempo, ambos passam pela checagem de conflito (o walk ainda não está atribuído a ninguém), e ambos chegam a `accept_operational_walk()`. O resultado depende de timing de I/O — o walk pode ficar com dois walkers atribuídos, corrompendo o estado operacional.

**Correção recomendada:**  
Usar lock pessimista:
```python
walk = db.query(Walk).filter(Walk.id == walk_id).with_for_update().first()
```
Ou usar lock otimista com campo `version` e capturar `StaleDataError`.

---

## ALTOS

### A1 — Aprovação de walker não atômica

**Arquivo:** `app/routes/walker.py`  
**Linhas:** 883–889, 811, 915

**Problema:**  
O fluxo de aprovação de walker executa múltiplos `db.commit()` separados:

```python
# Commit 1: salva status do WalkerProfile
db.commit()                                   # ln 883

# Após o commit, side effects:
if profile.status == "active":
    mark_referral_approved(profile.user_id, db)    # ln 886 — pode falhar
    ...

# Em outro lugar, commit 2: role do User
existing_user.role = "walker"
db.commit()                                   # ln 811 / 915
```

**Risco concreto:**  
Se `mark_referral_approved()` lançar uma exceção (constraint, banco sobrecarregado), o WalkerProfile já está `"active"` mas o sistema de referral não registrou a aprovação. O walker pode começar a operar, mas promoções e comissões de indicação nunca serão processadas. Igualmente, se a mudança de `user.role` falhar, o User fica sem o papel de walker mas com o perfil ativo.

**Correção recomendada:**  
Envolver tudo em uma única transação usando `db.begin_nested()` ou garantindo que todos os side effects ocorram antes do `commit()` final.

---

### A2 — CORS wildcard com credentials=True

**Arquivo:** `app/main.py`  
**Linhas:** 284–290

**Problema:**  
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

A combinação `allow_origins=["*"]` + `allow_credentials=True` viola a especificação CORS (browsers devem rejeitar, mas o comportamento varia). Mais importante: permite que qualquer origem faça requisições autenticadas à API.

**Risco concreto:**  
Um site malicioso pode induzir um usuário logado a fazer requisições involuntárias à API (CSRF clássico). Também facilita ataques de exfiltração de dados via XSS em qualquer domínio.

**Correção recomendada:**  
```python
allow_origins=["https://app.aumigao.com", "https://admin.aumigao.com"]
```
Adicionar uma variável de ambiente `ALLOWED_ORIGINS` para configurar por ambiente.

---

### A3 — JWT fraco sem rotação

**Arquivo:** `app/core/security.py`

**Problema:**  
- Secret atual: `"aumigao-super-secret-key-2026-strong-long"` — predictable, já exposto no `.env` (ver C4)
- Fallback hardcoded em código: `"aumigao-dev-secret-key-with-more-than-32-bytes"` — um token assinado com o fallback é válido em qualquer ambiente que não tenha a variável definida
- Não há blacklist de tokens nem mecanismo de revogação
- Não há rotação periódica de chave

**Risco concreto:**  
Token de um usuário desligado ou comprometido permanece válido até o `exp` (que pode ser longo). Com a chave exposta, qualquer pessoa pode assinar um token com `{"sub": "admin@aumigao.com", "role": "super_admin"}`.

**Correção recomendada:**  
- Gerar secret com `openssl rand -hex 64` e armazenar em secrets manager
- Remover o fallback hardcoded — falha ruidosa é melhor que falha silenciosa
- Implementar blacklist de JIDs (JWT ID) em Redis para suportar logout e revogação

---

### A4 — Sem rate limiting

**Escopo:** Toda a aplicação

**Problema:**  
Nenhum uso de `slowapi`, `fastapi-limiter`, ou middleware equivalente foi encontrado. Os endpoints mais sensíveis estão completamente abertos para volume arbitrário de requisições.

**Risco concreto:**  
- `/auth/login`: brute force irrestrito — 1000 tentativas/segundo são possíveis
- `/webhooks/asaas`: flood de eventos forjados pode saturar o banco
- `/api/partner-applications` (sem auth): enumeração completa de candidatos

**Correção recomendada:**  
Adicionar `slowapi` com `limiter.limit("5/minute")` no endpoint de login, e `limiter.limit("60/minute")` nos demais endpoints sensíveis.

---

### A5 — Duas implementações conflitantes de get_current_user

**Arquivos:**  
- `app/core/deps.py` (ln 17–42)  
- `app/dependencies/auth.py` (ln 11–22)

**Problema:**  
```python
# app/core/deps.py — busca User por EMAIL no campo "sub"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")
def get_current_user(token: str = Depends(oauth2_scheme), ...):
    email = payload.get("sub")
    user = db.query(User).filter(User.email == email).first()

# app/dependencies/auth.py — busca User por ID no campo "sub"
security = HTTPBearer(auto_error=False)
def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security), ...):
    user_id = payload.get("sub")
    user = db.get(User, user_id)
```

**Risco concreto:**  
Dependendo de qual módulo é importado em cada rota, o mesmo token JWT pode identificar usuários diferentes. Se a lógica de geração de token mudou de "email no sub" para "user_id no sub" (ou vice-versa), rotas que importam a versão errada falharão silenciosamente retornando `None` ou levantando 401. Em cenários de borda, pode haver bypass de autenticação.

**Correção recomendada:**  
Manter uma única implementação canônica de `get_current_user` em `app/dependencies/auth.py` e deletar `app/core/deps.py`. Auditar todos os `from app.core.deps import get_current_user` e substituir pelo import correto.

---

### A6 — Limits de plano não enforçados via admin API

**Arquivo:** `app/services/tenant_plan_service.py`

**Problema:**  
O serviço define corretamente os limites por plano e expõe funções como `enforce_can_add_tenant_unit()`. Porém, as rotas de admin que criam unidades e modificam configurações de tenant não chamam essas funções:

```python
# Em app/routes/admin.py — criação de tenant unit sem checar limite do plano
# enforce_can_add_tenant_unit() não é referenciada em nenhuma rota de admin
```

**Risco concreto:**  
Um tenant no plano Starter (limite 1 unidade) que conheça o endpoint de admin pode criar múltiplas unidades sem pagar pelo plano superior. Viola a barreira comercial entre planos.

**Correção recomendada:**  
Chamar `enforce_can_add_tenant_unit(tenant, db)` antes de persistir qualquer nova unidade, tanto nas rotas de tenant quanto nas rotas de admin.

---

### A7 — walks.py sem filtro de tenant para admins/walkers

**Arquivo:** `app/routes/walks.py`  
**Linhas:** 230–244

**Problema:**  
```python
def list_walks(user: User, db: Session = Depends(get_db), limit: int = Query(50)):
    query = db.query(Walk)
    if user.role == "walker":
        query = query.filter(
            (Walk.walker_id == user.id) | (Walk.walker_id.is_(None))
        )
    elif user.role not in {"admin", "super_admin"}:
        query = query.filter(Walk.tutor_id == user.id)
    # Se role == "admin" ou "super_admin": nenhum filtro adicional de tenant
    walks = query.order_by(Walk.created_at.desc()).limit(limit).all()
```

Um walker que atua em múltiplos tenants (walkers são globais) vê walks disponíveis de todos os tenants — incluindo tenants que não contrataram esse walker.

**Risco concreto:**  
Walker de Tenant A pode ver (e potencialmente aceitar) walks do Tenant B, quebrando o isolamento operacional.

**Correção recomendada:**  
Adicionar filtro de tenant ao branch do walker e ao branch de admin:
```python
if user.role == "walker":
    query = query.filter(
        Walk.tenant_id == current_tenant_id,
        (Walk.walker_id == user.id) | (Walk.walker_id.is_(None))
    )
elif user.role in {"admin", "super_admin"}:
    query = query.filter(Walk.tenant_id == user.tenant_id)
```

---

## MÉDIOS

### M1 — N+1 massivo em listagem pública de walkers

**Arquivo:** `app/routes/walker.py`  
**Linhas:** 1313–1396

**Problema:**  
A função `_public_walker_rows()` carrega todos os perfis ativos e, para cada um, dispara queries individuais:

```python
profiles = db.query(WalkerProfile).filter(
    WalkerProfile.status == "active",
    WalkerProfile.active_as_walker.is_(True),
).all()                                          # 1 query → N resultados

for profile in profiles:
    user = db.get(User, profile.user_id)         # N queries para User
    summary = reputation_summary(profile.user_id, db)         # N queries
    walk_review_summary = _walk_review_reputation_summary(profile.user_id, db)  # N queries
```

**Risco concreto:**  
Com 100 walkers ativos: ~300 queries por requisição ao endpoint `/walkers` (ou `/api/public/walkers`). Com 500 walkers: ~1500 queries. Cada chamada pública a esse endpoint pode saturar o pool de conexões do banco.

**Correção recomendada:**  
Usar `joinedload` para carregar `User` junto com `WalkerProfile` em uma única query, e pré-computar/cachear as métricas de reputação (ex: coluna `avg_rating` atualizada por trigger ou job periódico).

---

### M2 — Admin dashboard carrega tabelas inteiras em Python

**Arquivo:** `app/routes/admin.py`  
**Linhas:** 887–969

**Problema:**  
```python
real_clients = [user for user in db.query(User).all() if _is_real_tutor(user)]
real_pets    = [pet  for pet  in db.query(Pet).all()  if _is_real_pet(pet, db)]
real_walks   = [walk for walk in db.query(Walk).all() if _is_real_admin_walk(walk, db)]
real_active_walkers_count = sum(
    1 for profile in db.query(WalkerProfile).all()
    if _is_real_active_walker_profile(profile, db)   # executa queries adicionais por profile
)
```

Toda a lógica de filtragem ("is_real") é feita em Python após carregar os registros do banco.

**Risco concreto:**  
Com 10k walks e 2k walkers, esta rota carrega 12k+ registros em memória + executa potencialmente 2k queries adicionais dentro dos loops. Em produção com volume real, vai estourar timeout antes de responder.

**Correção recomendada:**  
Mover os critérios de "real" para cláusulas SQL (`WHERE is_test_data IS FALSE`, `WHERE email NOT LIKE '%@test.%'`, etc.). Retornar apenas contagens agregadas (`SELECT COUNT(*) ...`), não listas completas.

---

### M3 — Índices faltando em colunas críticas

**Arquivos:** `app/models/walk.py`, `app/models/walker_profile.py`

**Problema:**  

| Coluna | Modelo | Indexada? | Queries afetadas |
|--------|--------|-----------|-----------------|
| `walker_id` | `Walk` | Não | `walker.py:512, 1132`, `walks.py:232` |
| `status` | `Walk` | Não | `walker.py:512, 1131, 1579`, `admin.py:870` |
| `status` | `WalkerProfile` | Não | `walker.py:1314`, `admin.py:919` |
| `assigned_walker_id` | `Walk` | Sim | — |
| `tenant_id` | `Walk` | A verificar | todas as queries multi-tenant |

**Risco concreto:**  
Queries como `Walk.status.in_(["Indo buscar o pet", "Passeando agora"])` fazem full table scan na tabela `walks`. Com 100k walks, cada checagem operacional de conflito de agenda (executada por todo walker que tenta aceitar um passeio) varre a tabela inteira.

**Correção recomendada:**  
```python
# app/models/walk.py
walker_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
status:    Mapped[str]         = mapped_column(String, default="Agendado", index=True)

# app/models/walker_profile.py  
status: Mapped[str] = mapped_column(String, default="pending", index=True)
```
Criar migration Alembic correspondente.

---

### M4 — Dados sensíveis em resposta de erro

**Arquivo:** `app/routes/payments.py`  
**Linhas:** 112–134

**Problema:**  
```python
def raise_asaas_error(step: str, response: httpx.Response, request_payload: dict | None = None):
    ...
    raise HTTPException(
        status_code=502,
        detail={
            "message": f"Falha Asaas em {step}.",
            "status_http": response.status_code,
            "asaas_code": asaas_error["code"],
            "asaas_description": asaas_error["description"],
            "request_payload": sanitize_for_log(request_payload or {}),  # ainda presente
            "asaas": sanitize_for_log(response_data),
        },
    )
```

Mesmo sanitizado, o payload é retornado ao cliente HTTP (e potencialmente logado em ferramentas de observabilidade do cliente mobile).

**Risco concreto:**  
Vazar informações internas do gateway de pagamento (códigos de erro, estrutura de request) facilita engenharia reversa do fluxo de pagamento por atacantes.

**Correção recomendada:**  
Logar internamente com `logger.error(...)` e retornar ao cliente apenas uma mensagem genérica:
```python
raise HTTPException(status_code=502, detail="Erro no processamento do pagamento")
```

---

### M5 — Modelos sem tenant_id que deveriam ter

**Modelos afetados:** `complaints`, `complaint_evidences`, `complaint_decisions`, `complaint_status_history`, `payments`, `walk_reviews`, `walk_tips`, `walker_reviews`, `walk_completion_reviews`

**Problema:**  
Esses modelos relacionam-se diretamente com `walks`, `pets` ou `users`, mas não possuem coluna `tenant_id`. Como resultado, queries nesses modelos nunca podem ser filtradas por tenant — a menos que se faça um JOIN com a tabela pai (o que não é feito nas rotas atuais).

**Risco concreto:**  
Um admin de Tenant A pode listar reclamações (`/admin/complaints`) e ver todas as reclamações de todos os tenants, incluindo detalhes confidenciais de disputas entre tutores e walkers de outros tenants.

**Correção recomendada:**  
Adicionar `tenant_id` nesses modelos via migration, populando o valor a partir do `walk.tenant_id` ou `user.tenant_id` correspondente. Adicionar filtro nas queries de admin.

---

### M6 — Tenant default como fallback no resolver

**Arquivo:** `app/middleware/tenant_resolver.py`

**Problema:**  
Quando nenhum header (`X-Tenant-Id`, `X-Tenant-Slug`) e nenhum subdomain identificam o tenant, o resolver retorna um "tenant padrão" buscado no banco. Isso significa que requisições sem contexto de tenant resolvem silenciosamente para o primeiro tenant cadastrado.

**Risco concreto:**  
Uma requisição autenticada sem header de tenant (ex: app desatualizado, cliente de testes) acessa e modifica dados do tenant padrão sem perceber. Em testes de integração, pode causar contaminação de dados.

**Correção recomendada:**  
Retornar HTTP 400 ou 422 quando o tenant não puder ser identificado, em vez de fazer fallback silencioso. Reservar o fallback apenas para endpoints genuinamente públicos (landing page, cadastro inicial).

---

## BAIXOS

### B1 — Sem índice composto em WalkMatchingAttempt

**Arquivo:** `app/models/walk.py` (tabela `walk_matching_attempts`)

**Problema:**  
Queries frequentes usam `WHERE walk_id = ? AND status = 'pending'`, mas apenas índices simples existem em `walk_id` e `status` separadamente.

**Correção recomendada:**  
```python
__table_args__ = (Index("ix_matching_walk_status", "walk_id", "status"),)
```

---

### B2 — PII em texto puro (CPF, telefone)

**Modelos:** `User`, `WalkerProfile`, `TutorProfile`

**Problema:**  
Campos `cpf`, `phone`, `address` são armazenados como `VARCHAR` sem encryption at rest. Senhas estão corretamente hasheadas com PBKDF2, mas demais dados pessoais identificáveis não estão.

**Risco concreto:**  
Comprometimento do banco de dados expõe CPF e telefone de todos os usuários — dado o contexto LGPD brasileiro, isso exige notificação à ANPD.

**Correção recomendada:**  
Criptografar esses campos com symmetric encryption (ex: Fernet/AES) no nível da aplicação usando uma chave separada da `JWT_SECRET`. Ou usar colunas encriptadas via extensão PostgreSQL (`pgcrypto`).

---

### B3 — admin_seed_service cria admin com senha fraca

**Arquivo:** `app/services/admin_seed_service.py`  
**Linhas:** 16–27

**Problema:**  
A cada startup com `RUN_STARTUP_ADMIN_SEED=true`, o serviço cria ou atualiza usuários admin com `Admin@123` e `SuperAdmin@123`. Sem complexidade mínima, sem MFA, sem expiração forçada na primeira autenticação.

**Correção recomendada:**  
Gerar senhas aleatórias na primeira execução (via `secrets.token_urlsafe(32)`), armazená-las em secrets manager, e desativar o seed em ambientes de produção via feature flag.

---

### B4 — admin.py com 1800+ linhas e alta coesão interna

**Arquivo:** `app/routes/admin.py`

**Problema:**  
Um único arquivo agrega rotas de dashboard, walks, walkers, pagamentos, reviews, alertas operacionais, onboarding, features de tenant e mais. Funções helpers como `_is_real_admin_walk()` chamam `db.query()` internamente, tornando difícil testar ou otimizar partes isoladas.

**Risco concreto:**  
Qualquer mudança neste arquivo pode ter efeitos colaterais inesperados. A ausência de testes isoláveis significa que regressões só aparecem em produção.

**Correção recomendada:**  
Decompor em módulos: `admin_walks.py`, `admin_walkers.py`, `admin_payments.py`, `admin_dashboard.py`. Mover lógica de filtragem para camada de serviço/repositório testável independentemente.

---

## Contexto Arquitetural: Walkers Globais

Por design, `WalkerProfile` não tem `tenant_id` — walkers são recursos globais da plataforma, compartilhados entre tenants. Isso é uma decisão de produto legítima (permite que um walker atenda múltiplos tenants), mas cria pontos de compartilhamento que precisam de atenção explícita:

- Avaliações de walkers (`walker_reviews`) sem `tenant_id` são vistas por todos os tenants
- O registro de acesso `tenant_walker_access` controla quais walkers estão disponíveis por tenant — esse mecanismo precisa ser consistentemente aplicado antes de qualquer query que retorne walkers disponíveis

Se a intenção é que walkers sejam realmente globais, isso deve ser documentado explicitamente e os filtros de `tenant_walker_access` devem ser aplicados em todas as queries de listagem de walkers.

---

## Prioridade de Ação

| Prioridade | Achados | Prazo sugerido |
|-----------|---------|----------------|
| P0 — Imediato | C1, C2, C3, C4 | Antes do próximo deploy |
| P1 — Esta sprint | C5, A1, A2, A3, A4, A5 | 1–2 semanas |
| P2 — Próxima sprint | A6, A7, M1, M2, M3 | 2–4 semanas |
| P3 — Backlog técnico | M4, M5, M6, B1, B2, B3, B4 | Planejamento trimestral |

---

*Relatório gerado por análise estática. Nenhuma modificação de código foi realizada.*
