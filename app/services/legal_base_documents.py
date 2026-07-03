from __future__ import annotations
BASE_VERSION = "base-2026-07"
_TENANT_PLACEHOLDER = "{{TENANT_NAME}}"
_DISCLAIMER = (
    "\n\n---\n"
    "Modelo base fornecido pela plataforma sem responsabilidade; o estabelecimento "
    "deve adequar e validar este documento com seu advogado."
)

_BASE_DOCUMENTS: dict[str, tuple[str, str]] = {
    "service_terms": (
        "Contrato de Prestacao de Servico - {{TENANT_NAME}}",
        (
            "Este documento rege a prestacao de servicos de passeio e cuidado de pets "
            "contratada diretamente entre o tutor e o estabelecimento {{TENANT_NAME}} "
            "(o Estabelecimento), por meio da plataforma. A plataforma atua apenas como "
            "meio tecnico de intermediacao, agendamento, comunicacao e registro operacional; "
            "a relacao contratual do servico e do Tutor com o Estabelecimento.\n\n"
            "1. OBJETO. O Estabelecimento se compromete a executar os passeios contratados com "
            "zelo, seguranca e respeito ao bem-estar do animal, conforme as informacoes "
            "prestadas pelo Tutor no agendamento (porte, saude, temperamento, rotina, ponto de "
            "retirada e horario), que compoem as especificacoes do servico.\n\n"
            "2. OBRIGACOES DO TUTOR. O Tutor deve manter dados completos e verdadeiros sobre o "
            "animal; entregar o pet em condicoes adequadas para o passeio (guia, coleira ou "
            "peitoral seguros, identificacao quando disponivel); informar restricoes, medicacoes "
            "e contatos de emergencia; e usar a plataforma de forma licita e respeitosa.\n\n"
            "3. RISCOS INERENTES. O Tutor reconhece que passeios com animais envolvem riscos "
            "inerentes (fuga, reacao a ruidos, brigas, mal-estar, acidentes, intercorrencias de "
            "saude, comportamento imprevisivel). O Estabelecimento adota medidas razoaveis de "
            "seguranca, mas nao garante ausencia de intercorrencias.\n\n"
            "4. EVIDENCIAS E ACOMPANHAMENTO. A execucao pode envolver check-in, check-out, "
            "horarios, geolocalizacao quando permitida, fotos, relatos e ocorrencias, usados "
            "para seguranca, suporte, auditoria e resolucao de disputas.\n\n"
            "5. PAGAMENTO. Os valores, formas de pagamento e eventuais creditos ou planos seguem "
            "as condicoes informadas no ato da contratacao. Salvo indicacao em contrario, a "
            "plataforma nao custodia valores do Tutor: a medicao do servico nao se confunde com "
            "custodia financeira.\n\n"
            "6. RESPONSABILIDADE. Na maxima extensao permitida pela lei, a responsabilidade do "
            "Estabelecimento limita-se ao servico efetivamente contratado, sem prejuizo dos "
            "direitos do consumidor. A plataforma responde apenas pelo funcionamento do meio "
            "tecnico, nao pela prestacao do servico em si.\n\n"
            "7. VIGENCIA E ALTERACOES. Este contrato vigora enquanto durar a relacao de "
            "prestacao. Alteracoes materiais sao comunicadas com antecedencia razoavel e valem "
            "para contratacoes futuras."
        ),
    ),
    "service_cancellation": (
        "Politica de Cancelamento e Reembolso - {{TENANT_NAME}}",
        (
            "Esta politica rege cancelamentos, atrasos, no-show e reembolsos dos servicos "
            "contratados entre o Tutor e o estabelecimento {{TENANT_NAME}}, pelos fluxos "
            "oficiais da plataforma.\n\n"
            "1. CANCELAMENTO PELO TUTOR. O cancelamento pode gerar reagendamento, credito, "
            "reembolso total ou parcial, retencao de valores ou analise manual, conforme o "
            "momento do cancelamento, o deslocamento ja realizado, a recorrencia e as evidencias "
            "disponiveis.\n\n"
            "2. CANCELAMENTO OU ATRASO PELO ESTABELECIMENTO. Se o passeador cancelar, atrasar de "
            "forma relevante ou ficar indisponivel, o Estabelecimento podera propor novo horario, "
            "substituir o profissional, cancelar a solicitacao ou orientar reembolso conforme o "
            "caso.\n\n"
            "3. NO-SHOW. Ausencia de responsavel, endereco incorreto, falta de acesso ao pet ou "
            "pet sem condicoes minimas de seguranca podem ser tratados como servico prejudicado "
            "por falha do Tutor, sujeito a cobranca, reagendamento limitado ou analise manual. "
            "No-show do passeador gera cancelamento, reembolso ao Tutor e ajuste operacional.\n\n"
            "4. PLANOS E CREDITOS. Quando houver plano de assinatura, os creditos do ciclo ja "
            "pago permanecem disponiveis ate o fim do ciclo; direitos de arrependimento (7 dias, "
            "CDC art. 49) e reembolso por falha imputavel ao Estabelecimento sao observados na "
            "forma da lei.\n\n"
            "5. ANALISE DE EVIDENCIAS. Reembolsos, quando cabiveis, podem depender de analise de "
            "pagamento, antifraude, fotos, geolocalizacao, mensagens e status de "
            "check-in/check-out. Casos sensiveis e emergencias podem receber analise individual "
            "para preservar seguranca e boa-fe."
        ),
    ),
    "walker_agreement": (
        "Termo de Prestacao Autonoma do Passeador - {{TENANT_NAME}}",
        (
            "Este termo rege a relacao entre o passeador, na qualidade de prestador AUTONOMO e "
            "INDEPENDENTE, e o estabelecimento {{TENANT_NAME}} (o Estabelecimento), com uso "
            "da plataforma como meio tecnico de intermediacao.\n\n"
            "1. AUSENCIA DE VINCULO. O uso da plataforma e a prestacao ao Estabelecimento NAO "
            "geram vinculo empregaticio, sociedade, representacao, exclusividade, subordinacao "
            "juridica, controle de jornada ou garantia de demanda minima. O Passeador define sua "
            "disponibilidade e pode aceitar ou recusar solicitacoes.\n\n"
            "2. AUTONOMIA REFORCADA. O Passeador NAO e penalizado por recusar solicitacoes nem "
            "por ficar indisponivel. Recusa e indisponibilidade nao reduzem reputacao nem "
            "condicionam o acesso a novas solicitacoes. Nao ha jornada, meta obrigatoria ou tempo "
            "minimo de conexao. A remuneracao e por passeio efetivamente realizado, e nao por "
            "hora ou jornada.\n\n"
            "3. CONDUTA E SEGURANCA. O Passeador deve tratar tutores, pets e terceiros com "
            "respeito; respeitar as informacoes e condicoes do passeio informadas no agendamento; "
            "usar guia, coleira e equipamentos adequados; nao abandonar o pet; nao realizar "
            "contratacao por fora; e comunicar imediatamente atrasos, riscos, incidentes ou "
            "emergencias pelos canais oficiais. A seguranca do animal e prioridade operacional.\n\n"
            "4. EXECUCAO PESSOAL (KYC). A exigencia de que o passeio seja executado pela propria "
            "pessoa verificada decorre de seguranca animal e prevencao a fraude (KYC), NAO "
            "configurando subordinacao nem vinculo empregaticio.\n\n"
            "5. TRIBUTACAO E REGIME. Recomenda-se atuar como Microempreendedor Individual (MEI) "
            "ou outro regime regular de prestador autonomo, com emissao dos proprios documentos "
            "fiscais. Cada parte e responsavel por suas obrigacoes tributarias.\n\n"
            "6. PAGAMENTO. Conforme a operacao, o Passeador e pago pelo Estabelecimento (modelo "
            "tenant) na qualidade de fornecedor, por passeio medido, sem que a plataforma "
            "custodie valores do Tutor.\n\n"
            "7. EVIDENCIAS. Check-in, check-out, geolocalizacao quando permitida, fotos e relatos "
            "podem ser usados para pagamento, reputacao, resolucao de disputas e auditoria."
        ),
    ),
}

DOC_TYPES_BY_ROLE: dict[str, tuple[str, ...]] = {
    "tutor": ("service_terms", "service_cancellation"),
    "passeador": ("walker_agreement",),
}

ALL_DOC_TYPES: tuple[str, ...] = ("service_terms", "service_cancellation", "walker_agreement")


def is_valid_doc_type(doc_type: str) -> bool:
    return doc_type in _BASE_DOCUMENTS


def doc_types_for_role(role: str) -> tuple[str, ...]:
    return DOC_TYPES_BY_ROLE.get(role, ())


def _render(text: str, tenant_name: str | None) -> str:
    return text.replace(_TENANT_PLACEHOLDER, tenant_name or "o estabelecimento")


def base_document(doc_type: str, tenant_name: str | None) -> dict:
    title, content = _BASE_DOCUMENTS[doc_type]
    return {
        "doc_type": doc_type,
        "title": _render(title, tenant_name),
        "content": _render(content, tenant_name) + _DISCLAIMER,
        "version": BASE_VERSION,
        "is_custom": False,
    }
