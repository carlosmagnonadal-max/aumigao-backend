"""Background Check Fase 0 — regras de status agregado e links de validacao oficial.

Modelo (Carlos): o passeador emite certidoes oficiais GRATUITAS, faz upload (PDF) +
digita o numero; o admin valida semi-manualmente (clica no link da pagina oficial e
confere — o reCAPTCHA das fontes impede validacao 100% automatica). Tudo atras da flag
de tenant `background_checks` (default-OFF) => ZERO efeito em producao ate ligarem.

Certidoes (decisao fechada):
- OBRIGATORIAS:   PF (Policia Federal) + TJ estadual do domicilio.
- COMPLEMENTARES: TRF (Justica Federal) + TSE (crimes eleitorais) — opcionais.

Status agregado (background_check_status em walker_profiles):
- "none":      nenhuma certidao obrigatoria enviada.
- "submitted": obrigatorias enviadas mas ainda pending (nenhuma validada).
- "partial":   alguma obrigatoria validada, mas falta a outra (e sem rejeicao).
- "verified":  PF E TJ ambas "validated".
- "flagged":   qualquer obrigatoria "rejected".

Spec: docs/plano-background-check-fase0-2026-06-16.md
"""
from __future__ import annotations

from datetime import datetime

# Certidoes obrigatorias para o status "verified".
REQUIRED_CERT_TYPES = ("pf", "tj")
# Todas as certidoes aceitas (obrigatorias + complementares).
ALL_CERT_TYPES = ("pf", "tj", "trf", "tse")

# Validade padrao da certidao (dias). PF/TJ normalmente valem 90 dias.
DEFAULT_CERT_VALIDITY_DAYS = 90


def compute_background_status(profile, certificates) -> str:
    """Calcula o status agregado de antecedentes e PERSISTE no profile.

    `certificates` = iteravel de WalkerBackgroundCertificate (do passeador).
    Atualiza profile.background_check_status (sempre) e
    profile.background_verified_at (quando "verified").
    """
    # Mapa cert_type -> melhor status conhecido para as OBRIGATORIAS.
    by_type: dict[str, list[str]] = {}
    for cert in certificates or []:
        by_type.setdefault(cert.cert_type, []).append((cert.status or "pending"))

    required_present = [t for t in REQUIRED_CERT_TYPES if t in by_type]
    statuses_required = [s for t in REQUIRED_CERT_TYPES for s in by_type.get(t, [])]

    if any(s == "rejected" for s in statuses_required):
        status = "flagged"
    elif not required_present:
        status = "none"
    else:
        validated_required = [
            t for t in REQUIRED_CERT_TYPES
            if any(s == "validated" for s in by_type.get(t, []))
        ]
        if len(validated_required) == len(REQUIRED_CERT_TYPES):
            status = "verified"
        elif validated_required:
            status = "partial"
        else:
            status = "submitted"

    if profile is not None:
        profile.background_check_status = status
        if status == "verified":
            if not getattr(profile, "background_verified_at", None):
                profile.background_verified_at = datetime.utcnow()
        else:
            profile.background_verified_at = None

    return status


# --------------------------------------------------------------------------- links
# A validacao e SEMI-MANUAL: estas paginas oficiais exigem reCAPTCHA / preenchimento
# manual do numero, entao NAO ha como validar com zero toque. O admin abre o link e
# confere o numero/nome digitado pelo passeador.

_PF_VALIDATION_URL = "https://servicos.pf.gov.br/epol-sinic-publico/validar-cac"
_TSE_VALIDATION_URL = "https://www.tse.jus.br/servicos-eleitorais/certidoes"

# TJ estadual — paginas oficiais de validacao de certidao por UF (principais).
# Fallback generico para UFs nao mapeadas (busca por TJ do estado).
_TJ_VALIDATION_URLS: dict[str, str] = {
    "SP": "https://www.tjsp.jus.br/Certidao",
    "RJ": "https://www3.tjrj.jus.br/CJE/certidao/judicial/",
    "MG": "https://www.tjmg.jus.br/portal-tjmg/processos/certidao-judicial/",
}
_TJ_FALLBACK_URL = "https://www.cnj.jus.br/programas-e-acoes/certidao-negativa/"

# TRF — Justica Federal por regiao (mapa simples + fallback). A UF determina a regiao;
# como nem todo cliente envia regiao, deixamos um fallback nacional.
_TRF_VALIDATION_URLS: dict[str, str] = {
    "TRF1": "https://www.trf1.jus.br/trf1/certidao",
    "TRF2": "https://www.trf2.jus.br/certidoes",
    "TRF3": "https://web.trf3.jus.br/certidao",
    "TRF4": "https://www2.trf4.jus.br/trf4/processos/certidao/",
    "TRF5": "https://www.trf5.jus.br/certidao",
    "TRF6": "https://www.trf6.jus.br/certidao",
}
_TRF_FALLBACK_URL = "https://www.cjf.jus.br/cjf/certidoes"


def official_validation_url(cert_type: str | None, uf: str | None = None, number: str | None = None) -> str:
    """Link da pagina OFICIAL onde o admin valida a certidao (semi-manual).

    A validacao e semi-manual: estas paginas exigem reCAPTCHA / preenchimento do numero,
    entao nao ha como validar com zero toque. Este link leva o admin direto ao orgao.
    """
    normalized = (cert_type or "").strip().lower()
    uf_key = (uf or "").strip().upper()

    if normalized == "pf":
        return _PF_VALIDATION_URL
    if normalized == "tse":
        return _TSE_VALIDATION_URL
    if normalized == "tj":
        return _TJ_VALIDATION_URLS.get(uf_key, _TJ_FALLBACK_URL)
    if normalized == "trf":
        # Aceita tanto "TRF1".."TRF6" quanto fallback nacional.
        return _TRF_VALIDATION_URLS.get(uf_key, _TRF_FALLBACK_URL)
    return _TJ_FALLBACK_URL
