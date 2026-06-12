# Checklist de Ativação — Pagamentos em Produção (Asaas Live)

O código de pagamento live está **dormente por default**. O sandbox continua
ativo enquanto `PAYMENT_MODE` não for alterado. Siga este checklist antes de
ativar em Railway.

---

## 1. Pré-requisitos (Asaas)

- [ ] Criar conta Asaas em https://www.asaas.com (conta live, não sandbox)
- [ ] Completar verificação KYB (empresa/CPF do responsável)
- [ ] Gerar API Key live: Configurações → Integrações → API Key
- [ ] Anotar a URL do webhook para configurar no painel: `https://<seu-dominio>/payments/webhooks/asaas`
- [ ] Criar token de webhook no painel Asaas e anotar

---

## 2. Envs a configurar no Railway (serviço backend)

| Variável                  | Valor                                      | Obrigatória? |
|---------------------------|--------------------------------------------|--------------|
| `PAYMENT_MODE`            | `asaas_live`                               | Sim          |
| `ASAAS_LIVE_API_KEY`      | `$aact_...` (chave live do Asaas)          | Sim          |
| `ASAAS_WEBHOOK_TOKEN`     | Token gerado no painel Asaas               | Sim          |
| `ASAAS_LIVE_BASE_URL`     | `https://api.asaas.com/v3` (já é default) | Opcional     |

> **Atenção:** `ASAAS_SANDBOX_API_KEY` continua válida para testes locais com
> `PAYMENT_MODE=asaas_sandbox`. Não remova — o sandbox pode ser necessário
> para QA e testes de regressão.

---

## 3. Configurar webhook no painel Asaas

1. Acesse Asaas → Configurações → Webhooks
2. URL: `https://<seu-dominio>/payments/webhooks/asaas`
3. Eventos a habilitar (mínimo):
   - `PAYMENT_CONFIRMED`
   - `PAYMENT_RECEIVED`
   - `PAYMENT_OVERDUE`
   - `PAYMENT_REFUNDED`
   - `PAYMENT_CHARGEBACK_REQUESTED`
4. Token de autenticação: o mesmo que você configurou em `ASAAS_WEBHOOK_TOKEN`

---

## 4. Split real ao walker (opt-in duplo)

O split só ocorre quando **todas** as três condições forem verdadeiras
simultaneamente:

1. `PAYMENT_MODE=asaas_live`
2. `TenantPaymentConfig.split_enabled = True` (ligar via admin ou SQL)
3. Walker do walk possui `asaas_wallet_id` cadastrado

### 4a. Habilitar split_enabled por tenant

Via endpoint admin já existente (finance.manage):
```
PATCH /api/admin/payment-config
Body: {"split_enabled": true}
```

Ou via SQL (cuidado com ambiente):
```sql
UPDATE tenant_payment_configs SET split_enabled = true WHERE tenant_id = '<id>';
```

### 4b. Cadastrar wallet dos walkers

Para cada walker aprovado, obter o Wallet ID da conta Asaas dele e configurar:

```
PATCH /api/admin/walkers/<user_id>/wallet
Body: {"asaas_wallet_id": "wal_XXXXXXXXXXX"}
```

Requer permissão `finance.manage`.

Para limpar (desabilitar split para um walker):
```
PATCH /api/admin/walkers/<user_id>/wallet
Body: {"asaas_wallet_id": null}
```

### 4c. Matemática do split

A percentagem enviada ao Asaas é calculada automaticamente como:
```
walker_percent = 100 - commission_percent
```
onde `commission_percent` vem de `TenantPaymentConfig` (default 20%).
A fonte de verdade é `payment_split_service.build_payment_split`.

---

## 5. Migrations pendentes

Antes de subir em produção (apenas quando Railway puder rodar migration):

```bash
alembic upgrade 0022_payment_invoice_url_walker_wallet
```

Ou do head:
```bash
alembic upgrade head
```

As migrações adicionam:
- `payments.invoice_url` (VARCHAR nullable) — URL da fatura/checkout Asaas
- `walker_profiles.asaas_wallet_id` (VARCHAR nullable) — carteira Asaas do walker

---

## 6. Teste com pagamento real

Após ativar:
1. Criar um pagamento PIX pequeno (R$ 1,00) via app com conta de teste real
2. Pagar via PIX e aguardar confirmação do webhook
3. Verificar no admin que o status mudou para `pagamento_confirmado_sandbox`
   (o nome do status é histórico — funciona igual no modo live)
4. Verificar notificação de pagamento confirmado chegou ao tutor
5. Se split habilitado: verificar no painel Asaas que o repasse ao walker ocorreu

---

## 7. Rollback

Para reverter para sandbox sem perder dados:
```
PAYMENT_MODE=asaas_sandbox   (no Railway)
```
Nenhuma migration de rollback necessária — as colunas novas são nullable
e retrocompatíveis.

---

## 8. Notas sobre status internos

Os identificadores de status (ex.: `pagamento_confirmado_sandbox`,
`pagamento_sandbox_criado`) têm sufixo `_sandbox` por razão histórica.
Eles são reutilizados no modo live intencionalmente para não exigir
migração de dados nem atualização dos clients mobile/admin-web.
Não renomear esses valores.
