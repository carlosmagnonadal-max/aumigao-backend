# Pesquisa tributária/contábil — Ledger de crédito pré-pago (Aumigão)
_Pesquisa multi-fonte (deep-research, 6 agentes) realizada em 2026-06-28 para embasar a validação do contador. NÃO é parecer contábil formal._

## TL;DR
No **Simples Nacional, a venda do crédito não gera tributo**: o DAS incide no consumo (passeio realizado) ou no breakage (expiração), base CGSN 140/2018 art. 2º §8-9. No **Lucro Presumido** (futuro), mesma lógica no regime de competência (SC COSIT 295/2023); risco de antecipação só se optar pelo caixa. **ISS** não incide no carregamento, incide no consumo e no breakage (SF/DEJUG SP 41/2013 e 01/2017). **Breakage**: reconhecer na virada do ciclo (Método B do CPC 47 §B47) enquanto não há histórico; migrar pro proporcional (Método A §B46) após 12-18 meses. Passivo = **"passivo de contrato"** (não "adiantamento de clientes"). **Gap crítico: cada renovação mensal precisa registrar novo passivo** — hoje só o 1º ciclo registra → antecipa receita. Tudo exige aval do contador antes de escala.

---

## Pergunta 1 — PIS/COFINS: fato gerador na venda ou no consumo/breakage?
**Resposta:** venda do crédito NÃO gera; incide no consumo/breakage.
- **Simples (Anexo V hoje):** DAS no consumo/breakage. CGSN 140/2018 art. 2º §8º (receita no faturamento/entrega/prestação, o que ocorrer primeiro) e §9º (mesmo no caixa, adiantamento não compõe base no recebimento — compõe quando prestado). Teto: receita diferida entra na base até o fim do ano-calendário subsequente (irrelevante p/ ciclo mensal).
- **Presumido competência (padrão):** PIS/COFINS cumulativos 3,65%, na receita auferida. SC COSIT 295/2023 (vinculante).
- **Presumido caixa (opção):** antecipa no recebimento — pior; competência é melhor p/ Aumigão.
- **Real:** não-cumulativo 9,25%, receita auferida.
**Confiança:** Alta (Simples e Presumido/competência).
**Código:** manter — passivo na venda sem cálculo de tributo; mover passivo→receita no consumo/breakage e só então apurar. Verificar que a apuração mensal do DAS agrega só as movimentações passivo→receita do período.
Fontes: CGSN 140/2018; SC COSIT 295/2023; Lei 9.718/1998; PGFN.

## Pergunta 2 — IRPJ/CSLL só no consumo/breakage?
**Resposta:** Sim. CTN art. 43 (disponibilidade econômica/jurídica): adiantamento = passivo, não disponibilidade. Disponibilidade no consumo ou na extinção sem contraprestação (breakage = acréscimo patrimonial, art. 43 II). Simples via CGSN 140/2018 §9º; Presumido via SC COSIT 295/2023.
**Confiança:** Alta (princípio); Média (breakage — sem SC específica).
**Código:** base de IRPJ/CSLL sobre receita reconhecida (consumo+breakage), nunca sobre o recebido na renovação.
Fontes: CTN art. 43; SC COSIT 295/2023; PN CST 73/1973 e 75/1972.

## Pergunta 3 — Breakage: momento, carência, proporcional vs total, CDC
**Resposta:**
- **Método B (CPC 47 §B47)** agora: reconhecer integral na **virada do ciclo mensal** (créditos não acumulam). Migrar p/ **Método A (§B46) proporcional** após 12-18 meses de dados.
- **Carência:** contábil não exige além da expiração; boa prática = **D+30 após vencimento** antes do lançamento definitivo (margem p/ disputa).
- **Validade legal:** não há lei federal de validade mínima p/ crédito closed-loop de serviço (só telecom 30d). Vencimento ao fim do ciclo é defensável SE destacado no contrato antes da contratação (CDC arts. 6º III, 31); saldo parcial deve ser reembolsado/convertido, não retido (CDC art. 39); prazo curto sem justificativa = risco de cláusula abusiva (art. 51 IV).
- **Prescrição CDC:** 5 anos (art. 27) → provisão p/ contingência em nota explicativa (fora do ledger).
**Confiança:** Alta (Método B; ausência de prazo legal); Média (carência 30d, boa prática).
**Código:** job ao fim do ciclo lança Débito Passivo / Crédito Receita de Breakage; parâmetro `breakage_grace_days=30`; flag Método B↔A no roadmap.
Fontes: IFRS 15 / CPC 47 Rev.14 §§B44-B47; CDC arts. 27/31/39/51; DPU-PR; jurisprudência vale-presente.

## Pergunta 4 — Base do passivo / unit_value
**Resposta:** **valor cheio (gross), rateio linear** (CPC 47 §106). `unit_value = preço ÷ qtd créditos`. Impostos sobre receita são obrigação separada no reconhecimento, não reduzem o passivo. Se houver créditos de pesos diferentes → standalone selling price (§73). Se o plano embute **acesso + créditos**, CPC 47 §73 exige alocar o preço entre os componentes (2º ajuste mais importante).
**Confiança:** Alta (gross/linear); Média (alocação — depende do contrato).
**Código:** manter `unit_value = plan_price/credits_qty` (bruto); **verificar se está bruto** (não líquido); futuro: campo `access_component_value`.
Fontes: CPC 47 Rev.14 §§73/106; IFRS 15 §§74-77.

## Pergunta 5 — Renovação de ciclo = novo passivo?  ⚠️ GAP
**Resposta:** **SIM** — cada cobrança mensal é nova venda de créditos → novo passivo de contrato. Créditos expiram ao fim do ciclo (sem carryover). **Hoje o sistema só registra passivo no 1º ciclo** → nas renovações a receita é reconhecida no pagamento (caixa indevido), antecipando receita e subestimando passivo.
**Confiança:** Alta. É o gap mais relevante.
**Código (P1 crítico):** evento de cobrança mensal confirmada deve lançar Débito Caixa / Crédito Passivo pelo valor do plano, SEM reconhecer receita. Distinguir `subscription_created` vs `subscription_renewed` — ambos geram passivo. Associar o job de consumo/breakage ao ciclo correto.
Fontes: CPC 47 §§35/106; CGSN 140/2018 §8-9.

## Pergunta 6 — ISS
**Resposta:**
- **Carregamento: sem ISS** (SF/DEJUG SP 41/2013 — depósito, não serviço; LC 116 art. 2º §2º).
- **Consumo: ISS incide**, base **valor cheio** do crédito (Aumigão é prestadora no closed-loop; repasse ao passeador é custo). Enquadramento item **17.09 LC 116/2003** (cuidados/higiene de animais) ou similar municipal.
- **Breakage: ISS incide** (SF/DEJUG SP 01/2017, subitem 10.05) — **confirmar em Salvador** (precedente é só SP).
- ISS devido no local da prestação (LC 116 art. 3º) → parametrizar alíquota por município.
**Confiança:** Alta (carregamento/consumo); Média (breakage entre municípios).
**Código:** sem ISS na venda; ISS no consumo (base cheia); ISS no breakage (após confirmar Salvador); tabela de alíquota por município.
Fontes: SF/DEJUG 41/2013 e 01/2017; LC 116/2003; COSIT 170/2021.

## Pergunta 7 — CPC 47: nomenclatura + breakage proporcional vs expiração
**Resposta:** termo correto = **"passivo de contrato"** (contract liability); "receita diferida" aceitável como subtítulo. Breakage: **Método B** agora, **Método A** (proporcional) só com histórico confiável (12-18 meses). No **Simples** vale a **NBC TG 1002** (microentidade — competência sem os 5 passos formais do CPC 47); ao migrar p/ Presumido, adotar "passivo de contrato" formalmente.
**Confiança:** Alta.
**Código:** expor "Passivo de Contrato" nos relatórios; manter Método B; planejar Método A; nota explicativa da política.
Fontes: CPC 47 Rev.14 Apêndice A §§B44-B47/106; NBC TG 1002; IFRS 15.

---

## ✅ Resolvido pela pesquisa
Venda do crédito não tributa (DAS/PIS/COFINS/IRPJ/CSLL) · consumo é o fato gerador · carregamento sem ISS · ISS no consumo base cheia · breakage na expiração (fase atual) · passivo gross · "passivo de contrato" · closed-loop fora do BCB (Lei 12.865/2013 art.6 §3) · Simples compatível (SC COSIT 46/2014) · renovação = novo passivo (princípio).

## ⚖️ Precisa do "de acordo" do contador
ISS do breakage em Salvador · breakage no DAS (sem SC específica) · alocação acesso×créditos (depende do contrato) · migração p/ Método A · impacto LC 214/2025 (receita bruta ampliada) · provisão contingência CDC (5 anos) · ISS de passeios em outros municípios.

## 🔧 Ajustes de código priorizados
- **P1 crítico** — registrar passivo a cada renovação mensal (hoje só 1º ciclo).
- **P2 crítico** — job de breakage ao fim do ciclo com carência D+30 + ISS do breakage.
- **P3 alto** — verificar `unit_value` em valor bruto.
- **P4 alto** — lançamento de ISS no consumo (alíquota por município).
- **P5 médio** — expor "passivo de contrato" nos relatórios.
- **P6 médio** — separar componente de acesso dos créditos (`access_component_value`).
- **P7 baixo (roadmap)** — Método A de breakage por coorte.
- **P8 baixo** — alerta de teto temporal do Simples (passivo >12 meses sem movimento).

> Os P1 e P2 devem ser validados com o contador antes de produção em escala. O ledger é camada de ESTIMATIVA atrás de flag (`CREDIT_LEDGER_ENABLED`) e NÃO alimenta escrituração fiscal hoje — corrigir o P1 não afeta cobrança real.
