"""Schemas do modelo de Confianca do passeador (selos + certificacoes + nivel).

Tudo COMPUTE-ONLY (calculado dos dados existentes pelo `walker_trust_service`).
Nao ha tabela/migracao: estes schemas apenas tipam o payload de leitura.

Spec: docs/CONFIANCA-PASSEADOR.md
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class WalkerTrustSeals(BaseModel):
    """Camada 1 — selos publicos (escada; o front mostra o mais alto)."""

    cadastro_verificado: bool = False
    identidade_verificada: bool = False
    passeador_verificado: bool = False


class WalkerTrustCertification(BaseModel):
    """Uma certificacao automatica calculada (Camada 2 — automaticas/MVP)."""

    key: str
    label: str
    icon: str
    granted: bool


class WalkerTrustResponse(BaseModel):
    """Payload completo de Confianca de um passeador (compute-on-read)."""

    walker_user_id: str
    seals: WalkerTrustSeals
    certifications: list[WalkerTrustCertification] = Field(default_factory=list)
    # Nivel ja com os rotulos Bronze/Prata/Ouro/Diamante (Camada 3).
    level: str
    # Insumos usados no calculo, uteis para o front/admin (informativo).
    metrics: dict = Field(default_factory=dict)
