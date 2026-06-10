import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.legal_acceptance import LegalAcceptance
from app.models.user import User


router = APIRouter(prefix="/legal", tags=["legal"])
api_router = APIRouter(prefix="/api/legal", tags=["legal"])

LEGAL_VERSION = "beta-2026-05-21"

LEGAL_DOCUMENTS_BY_ROLE = {
    "tutor": [
        {
            "type": "terms",
            "title": "Termos de Uso do Tutor",
            "summary": "Regras para contratação de passeios no beta fechado do Aumigão.",
            "content": (
                "O Aumigão, também apresentado como Passeio com seu Pet, atua como plataforma de intermediação "
                "entre tutores e passeadores independentes. Ao usar o app, o Tutor entende que a plataforma organiza "
                "cadastro, matching, solicitação, comunicação, acompanhamento, evidências operacionais, pagamento, "
                "gorjetas, notificações e suporte, sem substituir a responsabilidade direta do Tutor pelas informações "
                "fornecidas sobre o animal. O Tutor deve manter dados completos e verdadeiros sobre porte, saúde, "
                "temperamento, medicações, restrições, comportamento com pessoas e outros animais, endereço, ponto de "
                "retirada e contatos de emergência. Omissões ou dados incorretos podem gerar risco animal, falha de "
                "matching, cancelamento, rematch, cobrança, suspensão de uso ou análise manual pela equipe.\n\n"
                "O Tutor reconhece que passeios com animais envolvem riscos inerentes, inclusive fuga, reação a ruídos, "
                "brigas, mordidas, mal-estar, acidentes, chuva, trânsito, falhas de guia, intercorrências de saúde e "
                "comportamento imprevisível. O Tutor deve entregar o pet em condição adequada para o passeio, com guia, "
                "coleira ou peitoral seguros, identificação quando disponível e orientações essenciais. A conduta do Tutor "
                "deve ser respeitosa, lícita e compatível com a operação do beta, sendo proibido usar o app para assédio, "
                "fraude, contratação por fora, exposição indevida de terceiros ou envio de informações falsas.\n\n"
                "O matching considera informações disponíveis no app, disponibilidade, região, perfil operacional, "
                "reputação, histórico, segurança e critérios de beta. Em caso de atraso, cancelamento, indisponibilidade, "
                "falha operacional ou risco, o Aumigão poderá acionar recovery, rematch, substituição de passeador, suporte "
                "ou análise manual. A finalização do passeio é auditável e pode envolver check-in, check-out, horários, "
                "geolocalização quando permitida, fotos, relatos, ocorrências, mensagens e outras evidências. O Tutor aceita "
                "que fotos e evidências do pet, do passeio e de incidentes sejam usadas para segurança, suporte, auditoria, "
                "disputa, qualidade e proteção da comunidade.\n\n"
                "Cancelamentos, no-show, reembolsos e reagendamentos seguem a Política de Cancelamento vigente no app. "
                "Gorjetas são opcionais, destinadas ao passeador quando processadas pelo fluxo oficial e podem estar sujeitas "
                "a regras operacionais, fiscais, antifraude e de processamento. Avaliações e sinais de reputação ajudam a "
                "qualidade da rede e devem refletir fatos reais. O Aumigão pode limitar, revisar ou suspender contas em caso "
                "de risco, fraude, abuso, incidentes, recorrência de cancelamentos, violação destes termos ou necessidade de "
                "proteção da operação. Na máxima extensão permitida pela lei, a responsabilidade do Aumigão é limitada ao papel "
                "de plataforma intermediadora e aos valores efetivamente pagos pelo serviço afetado, sem prejuízo de direitos "
                "legais do consumidor. Em emergências envolvendo o pet, o Tutor autoriza contato por canais informados, medidas "
                "razoáveis de contenção, deslocamento para local seguro, acionamento de suporte e orientação para atendimento "
                "veterinário quando necessário. O beta fechado pode ter disponibilidade limitada, análise manual e ajustes "
                "operacionais para segurança e melhoria do serviço."
            ),
        },
        {
            "type": "privacy",
            "title": "Política de Privacidade",
            "summary": "Tratamento de dados do Tutor, pets e operações de passeio.",
            "content": (
                "O Aumigão trata dados pessoais do Tutor, dados cadastrais, contato, endereço, credenciais, preferências, "
                "dados de pagamento quando aplicável, registros de uso, notificações, mensagens de suporte, dados do pet, "
                "fotos, evidências, avaliações, localização quando autorizada e logs operacionais relacionados a matching, "
                "recovery, rematch, cancelamentos, incidentes, segurança e finalização auditável. Dados do pet podem incluir "
                "nome, espécie, porte, idade, comportamento, restrições, saúde, medicações, fotos e instruções de cuidado.\n\n"
                "As bases legais incluem execução de contrato ou procedimentos preliminares, legítimo interesse para segurança, "
                "prevenção a fraude, suporte e melhoria do serviço, consentimento quando exigido, exercício regular de direitos, "
                "proteção da vida ou incolumidade física em emergências e cumprimento de obrigação legal ou regulatória. "
                "As informações podem ser compartilhadas de forma limitada com passeadores envolvidos no passeio, provedores "
                "de hospedagem, autenticação, notificações, mapas, pagamentos, antifraude, suporte, auditoria, atendimento "
                "jurídico ou autoridades quando houver obrigação ou necessidade legítima.\n\n"
                "Imagens, geolocalização e logs operacionais são usados para acompanhamento, segurança, resolução de disputas, "
                "qualidade, reputação, prevenção de abuso e análise de incidentes. Notificações podem ser enviadas por push, "
                "e-mail, SMS, WhatsApp ou canais disponíveis para comunicações transacionais, alertas de passeio, suporte, "
                "segurança e avisos do beta. Dados financeiros e de gorjetas são tratados para processamento, conciliação, "
                "comprovantes, prevenção de fraude e obrigações legais. Os dados são retidos pelo tempo necessário para operar "
                "o serviço, cumprir obrigações legais, resolver disputas, proteger direitos e manter histórico operacional "
                "auditável. O titular pode solicitar acesso, correção, confirmação de tratamento, portabilidade quando aplicável, "
                "anonimização, eliminação, informação sobre compartilhamento, revisão de decisões e oposição ou revogação de "
                "consentimento, observadas limitações legais e operacionais."
            ),
        },
        {
            "type": "cancellation",
            "title": "Política de Cancelamento e Reembolso",
            "summary": "Cancelamentos, atrasos, no-show, recovery, rematch e gorjetas.",
            "content": (
                "Cancelamentos devem ser feitos pelos fluxos oficiais do Aumigão. O cancelamento pelo Tutor pode gerar "
                "reagendamento, rematch, crédito, reembolso total ou parcial, retenção de valores ou análise manual conforme "
                "momento do cancelamento, deslocamento do passeador, recorrência, risco operacional e evidências disponíveis. "
                "Quando o Passeador cancelar, atrasar de forma relevante ou ficar indisponível, o Aumigão poderá acionar "
                "recovery, buscar rematch, propor novo horário, substituir o profissional, cancelar a solicitação ou orientar "
                "reembolso conforme o caso.\n\n"
                "No-show do Tutor, ausência de pessoa responsável, endereço incorreto, falta de acesso ao pet, pet sem condições "
                "mínimas de segurança ou recusa injustificada de entrega podem ser tratados como serviço prejudicado por falha "
                "do Tutor, sujeito a cobrança, retenção, reagendamento limitado ou análise manual. No-show do Passeador, abandono "
                "de fluxo, falha de check-in, falha de comunicação ou descumprimento de segurança pode gerar cancelamento, rematch, "
                "reembolso ao Tutor, ajuste de reputação, suspensão ou bloqueio do Passeador.\n\n"
                "Atrasos devem ser comunicados no app ou canais oficiais. Reembolsos, quando cabíveis, podem depender de análise "
                "de pagamento, antifraude, evidências, fotos, geolocalização, mensagens, status de check-in/check-out e histórico "
                "da operação. Gorjetas são voluntárias; quando o passeio for cancelado antes da prestação ou houver falha relevante, "
                "a gorjeta poderá ser cancelada, estornada ou analisada manualmente. Durante o beta fechado, casos sensíveis, "
                "incidentes, emergências, divergências de evidências e exceções operacionais podem ser avaliados individualmente "
                "pela equipe para preservar segurança, equilíbrio e boa-fé."
            ),
        },
        {
            "type": "lgpd-consent",
            "title": "Consentimento LGPD",
            "summary": "Aceite explícito para tratamento de dados necessários ao beta.",
            "content": (
                "Ao marcar o aceite, o Tutor consente de forma livre, informada e inequívoca com o tratamento dos dados pessoais "
                "e dados relacionados ao pet necessários para cadastro, autenticação, matching, contratação de passeios, "
                "comunicação com passeadores, acompanhamento operacional, fotos e evidências, geolocalização quando autorizada, "
                "notificações, suporte, segurança, prevenção de fraude, pagamentos, gorjetas, reputação, recovery, rematch, "
                "auditoria, atendimento de incidentes e cumprimento de obrigações legais.\n\n"
                "O tratamento também poderá ocorrer com base em execução de contrato, legítimo interesse, proteção da vida, "
                "exercício regular de direitos e obrigação legal, conforme a finalidade. O Tutor pode exercer seus direitos de "
                "titular, incluindo acesso, correção, informação, oposição, eliminação quando aplicável e revogação do consentimento. "
                "A revogação pode limitar ou impedir funcionalidades que dependem desses dados, especialmente segurança, matching, "
                "acompanhamento, suporte, evidências e auditoria operacional."
            ),
        },
        {
            "type": "geolocation-consent",
            "title": "Consentimento de Geolocalização",
            "summary": "Uso de localização para matching, passeio, segurança e auditoria.",
            "content": (
                "O Tutor autoriza o uso de geolocalização quando permitir o acesso no dispositivo ou informar endereço e pontos "
                "de retirada no app. A localização pode ser usada para matching por região, cálculo de disponibilidade, apoio ao "
                "passeador, acompanhamento do passeio, suporte, segurança, recovery, rematch, investigação de incidentes, "
                "auditoria operacional, prevenção de fraude e melhoria da experiência.\n\n"
                "Durante o passeio, dados de localização do Passeador e registros associados ao fluxo podem indicar deslocamento, "
                "check-in, check-out, rota aproximada, tempo e evidências de finalização. A negativa ou revogação da permissão de "
                "geolocalização no aparelho pode limitar recursos de acompanhamento, precisão do matching, suporte e segurança. "
                "A permissão pode ser alterada nas configurações do dispositivo, sem prejuízo de registros já gerados licitamente "
                "para operação, auditoria, disputa ou cumprimento de obrigação legal."
            ),
        },
    ],
    "passeador": [
        {
            "type": "terms",
            "title": "Termos e Condições do Passeador",
            "summary": "Regras para prestação autônoma de passeios no beta fechado.",
            "content": (
                "O Passeador atua como prestador autônomo e independente, usando o Aumigão, também apresentado como Passeio com "
                "seu Pet, como plataforma de intermediação com Tutores. O uso do app não gera vínculo empregatício, sociedade, "
                "representação, exclusividade, subordinação jurídica, controle de jornada ou garantia de demanda mínima. O Passeador "
                "define sua disponibilidade, pode aceitar ou recusar solicitações conforme regras do beta e é responsável por sua "
                "conduta profissional, veracidade cadastral, documentos, capacidade de atendimento, segurança animal e cumprimento "
                "das leis aplicáveis.\n\n"
                "O Passeador deve tratar Tutores, pets, equipe e terceiros com respeito; seguir instruções essenciais do Tutor; "
                "avaliar condições de segurança antes e durante o passeio; usar guia, coleira, peitoral e equipamentos adequados; "
                "evitar rotas perigosas; não abandonar o pet; não realizar contratação por fora; não manipular reputação, evidências "
                "ou pagamentos; e comunicar imediatamente atrasos, riscos, incidentes, fuga, acidente, mordida, mal-estar ou emergência. "
                "A segurança do animal é prioridade operacional. O Passeador deve recusar ou interromper o passeio quando houver risco "
                "grave, falta de condições mínimas, orientação insegura ou emergência, acionando o suporte pelos canais oficiais.\n\n"
                "O fluxo pode exigir check-in, check-out, geolocalização quando permitida, fotos, relatos, mensagens, evidências de "
                "retirada e devolução, confirmação de status e finalização auditável. Esses registros podem ser usados para suporte, "
                "pagamento, reputação, resolução de disputas, análise de cancelamento, recovery, rematch, segurança, prevenção de fraude "
                "e auditoria. O Passeador aceita que fotos e evidências do passeio sejam registradas no app, respeitando privacidade, "
                "finalidade operacional e exposição mínima necessária.\n\n"
                "Avaliações, reputação, no-show, atrasos, cancelamentos, incidentes, reclamações, qualidade de comunicação, aceitação, "
                "conclusão de passeio, evidências e recorrência de falhas podem impactar visibilidade, convites, acesso a solicitações, "
                "programas, bloqueios temporários, suspensão ou desativação da conta. Cancelamentos pelo Passeador devem ser justificados "
                "e comunicados com antecedência sempre que possível. No-show, abandono do fluxo, tentativa de burlar a plataforma, risco "
                "ao pet, fraude, assédio, exposição indevida de dados, cobrança externa ou descumprimento destes termos podem gerar medidas "
                "imediatas de proteção da operação. Gorjetas recebidas pelo fluxo oficial são voluntárias e podem depender de regras de "
                "processamento, antifraude, repasse e obrigações legais. O beta fechado pode ter análise manual, critérios de elegibilidade, "
                "limites de agenda, testes de produto e ajustes operacionais para segurança, qualidade e estabilidade."
            ),
        },
        {
            "type": "privacy",
            "title": "Política de Privacidade",
            "summary": "Tratamento de dados do Passeador e registros operacionais.",
            "content": (
                "O Aumigão trata dados pessoais do Passeador, documentos, foto de perfil, contato, endereço, dados bancários ou de "
                "repasse quando aplicável, experiência, disponibilidade, localização quando autorizada, registros de aceite, histórico "
                "de solicitações, check-in, check-out, fotos, evidências, avaliações, reputação, notificações, pagamentos, gorjetas, "
                "mensagens de suporte, logs operacionais, incidentes, cancelamentos, no-show, recovery, rematch e ações antifraude.\n\n"
                "As bases legais incluem execução de contrato ou procedimentos preliminares, legítimo interesse para segurança, "
                "qualidade, prevenção de fraude, auditoria e melhoria da plataforma, consentimento quando exigido, exercício regular "
                "de direitos, proteção da vida ou incolumidade física e cumprimento de obrigações legais ou regulatórias. Dados "
                "necessários podem ser compartilhados de forma limitada com Tutores vinculados ao passeio, provedores de tecnologia, "
                "mapas, notificações, pagamento, repasse, antifraude, hospedagem, suporte, auditoria, atendimento jurídico ou autoridades "
                "quando aplicável.\n\n"
                "Imagens, localização e logs operacionais são usados para segurança animal, acompanhamento, finalização auditável, "
                "resolução de disputas, reputação, análise de qualidade, repasses, prevenção de abuso e investigação de incidentes. "
                "Notificações podem ser enviadas por push, e-mail, SMS, WhatsApp ou canais disponíveis para solicitações, alertas, "
                "suporte, segurança e comunicações do beta. Os dados são retidos pelo tempo necessário para operação, obrigações legais, "
                "comprovação de repasses, disputas, prevenção de fraude e proteção de direitos. O Passeador pode exercer direitos de "
                "titular previstos na LGPD, observadas limitações legais e registros necessários à segurança e auditoria da operação."
            ),
        },
        {
            "type": "cancellation",
            "title": "Política de Cancelamento e Reembolso",
            "summary": "Regras para cancelamento, atraso, no-show e recovery.",
            "content": (
                "O Passeador deve cancelar apenas quando necessário e sempre pelos canais oficiais. Cancelamentos próximos ao horário, "
                "atrasos relevantes, falta de comunicação, no-show, falha de check-in, abandono de atendimento ou descumprimento de "
                "instruções essenciais podem acionar recovery, rematch, substituição do profissional, reembolso ao Tutor, ajuste de "
                "reputação, retenção ou revisão de repasse, suspensão temporária ou bloqueio. Justificativas e evidências podem ser "
                "solicitadas para análise operacional.\n\n"
                "Se o Tutor cancelar, não comparecer, informar endereço incorreto, impedir acesso ao pet, apresentar o animal sem "
                "condições mínimas de segurança ou alterar o combinado de forma relevante, o caso poderá ser analisado para reagendamento, "
                "cobrança, compensação operacional ou cancelamento. O Passeador deve registrar a ocorrência no app e aguardar orientação "
                "quando houver risco, divergência ou necessidade de prova.\n\n"
                "Reembolsos ao Tutor, créditos, estornos, repasses ao Passeador e tratamento de gorjetas dependem do status real do "
                "serviço, evidências, fotos, geolocalização, mensagens, horários, check-in/check-out, histórico e análise antifraude. "
                "Gorjetas são voluntárias e podem ser canceladas, estornadas ou analisadas quando houver falha relevante, cancelamento "
                "antes da prestação ou disputa. Durante o beta fechado, incidentes, emergências, divergências de evidências e exceções "
                "operacionais podem receber análise manual para preservar segurança, equilíbrio e boa-fé."
            ),
        },
        {
            "type": "lgpd-consent",
            "title": "Consentimento LGPD",
            "summary": "Aceite explícito para tratamento de dados do Passeador.",
            "content": (
                "Ao marcar o aceite, o Passeador consente de forma livre, informada e inequívoca com o tratamento de seus dados pessoais "
                "e operacionais para cadastro, verificação, elegibilidade, autenticação, matching, comunicação com Tutores, recebimento "
                "de solicitações, check-in, check-out, fotos e evidências, geolocalização quando autorizada, notificações, suporte, "
                "segurança, prevenção de fraude, reputação, pagamentos, gorjetas, repasses, recovery, rematch, auditoria, incidentes e "
                "cumprimento de obrigações legais.\n\n"
                "O tratamento também poderá ocorrer com base em execução de contrato, legítimo interesse, proteção da vida, exercício "
                "regular de direitos e obrigação legal. O Passeador pode solicitar acesso, correção, informação, oposição, eliminação "
                "quando aplicável e revogação do consentimento. A revogação pode limitar ou impedir funcionalidades essenciais, inclusive "
                "recebimento de solicitações, segurança, geolocalização operacional, finalização auditável, repasses, reputação, suporte "
                "e manutenção da conta no beta."
            ),
        },
        {
            "type": "geolocation-consent",
            "title": "Consentimento de Geolocalização",
            "summary": "Uso de localização para operação, segurança e auditoria do passeio.",
            "content": (
                "O Passeador autoriza o uso de geolocalização quando permitir o acesso no dispositivo. A localização pode ser usada "
                "para matching por região, disponibilidade, estimativa de chegada, check-in, acompanhamento do passeio, check-out, "
                "recovery, rematch, suporte, segurança animal, prevenção de fraude, reputação, auditoria operacional, análise de "
                "incidentes e comprovação de prestação do serviço.\n\n"
                "Durante o passeio, registros de localização podem ser associados a horários, fotos, mensagens, evidências e status de "
                "finalização auditável. A negativa ou revogação da permissão no aparelho pode limitar convites, acompanhamento, suporte, "
                "prova de execução, segurança e repasses relacionados à operação. A permissão pode ser alterada nas configurações do "
                "dispositivo, sem prejuízo de registros já gerados licitamente para operação, auditoria, disputa, pagamento ou cumprimento "
                "de obrigação legal."
            ),
        },
    ],
}


class LegalAcceptanceCreate(BaseModel):
    role: str = Field(default="tutor")
    accepted: bool = Field(default=False)


def _normalize_role(role: str | None, user: User | None = None) -> str:
    # Role explicito (ex.: ?role= no GET) tem prioridade; sem ele, cai no role do
    # usuario autenticado. (Corrige a precedencia de operador do codigo original,
    # que avaliava `(role or user.role) if user else ...` e ignorava o role recebido.)
    raw = (role or (user.role if user else None) or "").strip().lower()
    if raw in {"walker", "passeador"}:
        return "passeador"
    if raw in {"admin", "super_admin"}:
        return raw
    return "tutor"


def _versions() -> dict[str, str]:
    return {
        "terms_version": LEGAL_VERSION,
        "privacy_version": LEGAL_VERSION,
        "cancellation_version": LEGAL_VERSION,
        "lgpd_version": LEGAL_VERSION,
        "geolocation_version": LEGAL_VERSION,
    }


def _documents_for_role(role: str) -> list[dict[str, Any]]:
    audience = "Passeador" if role == "passeador" else "Tutor"
    documents = LEGAL_DOCUMENTS_BY_ROLE.get(role, LEGAL_DOCUMENTS_BY_ROLE["tutor"])
    return [
        {
            **document,
            "version": LEGAL_VERSION,
            "audience": audience,
        }
        for document in documents
    ]


def _serialize_acceptance(acceptance: LegalAcceptance | None) -> dict[str, Any] | None:
    if not acceptance:
        return None
    return {
        "id": acceptance.id,
        "user_id": acceptance.user_id,
        "user_role": acceptance.user_role,
        "terms_version": acceptance.terms_version,
        "privacy_version": acceptance.privacy_version,
        "cancellation_version": acceptance.cancellation_version,
        "lgpd_version": acceptance.lgpd_version,
        "geolocation_version": acceptance.geolocation_version,
        "accepted_at": acceptance.accepted_at,
    }


def _is_current(acceptance: LegalAcceptance | None) -> bool:
    if not acceptance:
        return False
    versions = _versions()
    return (
        acceptance.terms_version == versions["terms_version"]
        and acceptance.privacy_version == versions["privacy_version"]
        and acceptance.cancellation_version == versions["cancellation_version"]
        and acceptance.lgpd_version == versions["lgpd_version"]
        and acceptance.geolocation_version == versions["geolocation_version"]
    )


def _latest_acceptance(db: Session, user_id: str, role: str) -> LegalAcceptance | None:
    return (
        db.query(LegalAcceptance)
        .filter(LegalAcceptance.user_id == user_id, LegalAcceptance.user_role == role)
        .order_by(LegalAcceptance.accepted_at.desc())
        .first()
    )


def list_documents(role: str = Query(default="tutor")):
    normalized_role = _normalize_role(role)
    return {
        "role": normalized_role,
        "version": LEGAL_VERSION,
        "documents": _documents_for_role(normalized_role),
        "requires_acceptance": True,
    }


def acceptance_status(
    role: str = Query(default="tutor"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    normalized_role = _normalize_role(role, current_user)
    acceptance = _latest_acceptance(db, current_user.id, normalized_role)
    return {
        "role": normalized_role,
        "version": LEGAL_VERSION,
        "accepted": _is_current(acceptance),
        "versions": _versions(),
        "acceptance": _serialize_acceptance(acceptance),
    }


def accept_documents(
    payload: LegalAcceptanceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not payload.accepted:
        raise HTTPException(status_code=400, detail="Aceite explicito obrigatorio para continuar.")

    # O aceite e do usuario logado: o role DELE e a fonte de verdade (payload.role
    # tem default "tutor", entao nao serve para distinguir passeador de tutor).
    normalized_role = _normalize_role(getattr(current_user, "role", "") or payload.role)
    versions = _versions()
    acceptance = LegalAcceptance(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        user_role=normalized_role,
        accepted_at=datetime.utcnow(),
        **versions,
    )
    db.add(acceptance)
    db.commit()
    db.refresh(acceptance)
    return {
        "ok": True,
        "role": normalized_role,
        "version": LEGAL_VERSION,
        "accepted": True,
        "acceptance": _serialize_acceptance(acceptance),
    }


for legal_router in (router, api_router):
    legal_router.add_api_route("/documents", list_documents, methods=["GET"])
    legal_router.add_api_route("/acceptance", acceptance_status, methods=["GET"])
    legal_router.add_api_route("/acceptance", accept_documents, methods=["POST"])
