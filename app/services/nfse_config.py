"""Configuração da emissão de NFS-e via Asaas.

Todos os parâmetros são lidos de variáveis de ambiente em RUNTIME (via funções,
não constantes de import) para que os testes possam alternar valores via
monkeypatch/os.environ sem precisar reimportar o módulo.

ATENÇÃO: os placeholders de parâmetros fiscais (ISS, código municipal, etc.)
dependem de definição do contador antes de serem usados em produção.
"""
import os


def nfse_enabled() -> bool:
    """Retorna True quando NFS_E_ENABLED=1/true/yes no ambiente (case-insensitive).

    Padrão: False — gated off. ZERO mudança de comportamento em produção enquanto
    a flag não for explicitamente ligada.
    """
    val = os.environ.get("NFS_E_ENABLED", "false").strip().lower()
    return val in {"1", "true", "yes"}


# ---- Parâmetros fiscais (placeholders até o contador definir) ---------------

def get_municipal_service_code() -> str | None:
    """Código do serviço municipal (ex.: '1.07').

    TODO: preencher com o código correto após definição com o contador.
    Sem este código o Asaas pode rejeitar a emissão.
    """
    return os.environ.get("NFSE_MUNICIPAL_SERVICE_CODE") or None


def get_iss_rate() -> float:
    """Alíquota ISS em percentual (ex.: 2.0 = 2%).

    TODO: confirmar alíquota com o contador — varia por município e regime tributário.
    """
    raw = os.environ.get("NFSE_ISS_RATE", "0.0")
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 0.0


def get_service_description() -> str:
    """Descrição do serviço que aparece na nota fiscal.

    TODO: ajustar com o contador conforme o CNAE/regime da empresa.
    """
    return os.environ.get(
        "NFSE_SERVICE_DESCRIPTION",
        "Servico de assinatura mensal de plataforma",
    )


def get_deductions() -> float:
    """Deduções da base de cálculo do ISS (em R$).

    TODO: confirmar com o contador (algumas prefeituras aceitam deduções específicas).
    """
    raw = os.environ.get("NFSE_DEDUCTIONS", "0.0")
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 0.0
