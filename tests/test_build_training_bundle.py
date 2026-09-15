"""S2 — build do bundle da Capacitação a partir do manual em markdown.

Os módulos de teste são escritos em tmp_path (o repo docs/ não existe no CI do backend).
Cobre: front-matter, seções, marcações VERIFICAR, caixas, cartões rápidos (1 ou vários),
quiz (lista inline e em bloco, fonte null), fontes, módulo local (uf/cidade), CRLF,
arquivos-lixo ignorados, regras do vet_approved e CLI.
"""
import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_training_bundle.py"
_spec = importlib.util.spec_from_file_location("build_training_bundle", _SCRIPT)
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)

M01 = """---
id: m01
titulo: Passeio seguro
versao: 0.1-rascunho
tempo_leitura_min: 12
objetivos:
  - Checar o equipamento
---

# 1. Passeio seguro

Abertura curta.

## 1.1 Coleira, peitoral e guia

- **Peitoral** é mais seguro. [F1]
- Guia curta. ⚠️ VERIFICAR-VET: comprimento sem fonte.
- ⚠️ **VERIFICAR-JURÍDICO**: norma municipal.

> [!NUNCA] Não use enforcador. [F2] ⚠️ VERIFICAR-JURÍDICO: lei de SP.

## Cartão rápido
1. Peitoral ajustado.
2. Guia curta.

## Quiz
```yaml
- pergunta: "Qual equipamento?"
  opcoes: ["Enforcador", "Peitoral", "Retrátil", "Coleira apertada"]
  correta: 1
  explicacao: "O peitoral distribui a força."
  fonte: F1
- pergunta: "Cão solto, o que fazer?"
  opcoes:
    - "Correr atrás"
    - "Chamar com calma"
  correta: 1
  explicacao: "Correr vira 'brincadeira'."
  fonte: null
```

## Fontes
- **[F1]** Autor. *Título*. 2006.
- **[F2]** AVSAB. *Position*. 2021.

## Para o veterinário revisar
- ⚠️ VERIFICAR-VET: segredo interno do revisor.
"""

M08 = """---
id: m08
titulo: Primeiros socorros
versao: 0.1-rascunho
tempo_leitura_min: 35
objetivos:
  - Reconhecer emergência
---

# 8. Primeiros socorros

Abertura.

## 8.4 Sangramento e feridas

| Tipo | Onde |
|---|---|
| Pequeno | Abrace |

---

**Lembre sempre: em dúvida, é emergência.**

## Cartão rápido

### 8.4 Sangramento
1. Aperte firme.

### 8.7 Insolação
1. Resfrie primeiro.

## Quiz

```yaml
- pergunta: "Gaze encharcou?"
  opcoes: ["Tirar", "Pôr mais por cima", "Garrote", "Lavar"]
  correta: 1
  explicacao: "Não arranque o coágulo."
  fonte: F6
```

## Fontes

- **[F6]** VIN. *Bleeding*. 2026.
"""

M10B = """---
id: m10b-salvador
titulo: "Regras da sua cidade: Salvador (BA)"
versao: 0.2-rascunho
tempo_leitura_min: 8
cidade: Salvador
uf: BA
verificado_em: 2026-09-15
objetivos:
  - Saber quando a lei exige focinheira
---

# 10B. Regras da sua cidade: Salvador (BA)

Complementa o 10A.

## 10B.1 Guia e focinheira

- Critério é peso e comportamento. [F1]

## Quiz
```yaml
- pergunta: "Qual o critério?"
  opcoes: ["Raça", "Peso e comportamento"]
  correta: 1
  explicacao: "A lei usa porte e comportamento."
  fonte: F1
```
"""


def _write(directory: Path, name: str, text: str, *, crlf: bool = False) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    data = text.replace("\n", "\r\n") if crlf else text
    path.write_bytes(data.encode("utf-8"))
    return path


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "modulos"
    _write(src, "01-passeio-seguro.md", M01)
    _write(src, "08-primeiros-socorros.md", M08, crlf=True)
    _write(src, "10b-regras-da-cidade-salvador.md", M10B)
    return src


def test_national_module_strips_review_markers_and_vet_section(tmp_path):
    path = _write(tmp_path, "01-passeio-seguro.md", M01)
    module = builder.build_module(path, order=1)

    assert module["id"] == "m01"
    assert module["title"] == "Passeio seguro"
    assert module["scope"] == "national"
    assert module["uf"] is None
    assert module["reading_minutes"] == 12
    assert module["objectives"] == ["Checar o equipamento"]
    assert module["intro_markdown"] == "Abertura curta."
    assert [s["id"] for s in module["sections"]] == ["1.1"]
    assert module["sections"][0]["title"] == "Coleira, peitoral e guia"
    section_md = module["sections"][0]["markdown"]
    assert "> [!NUNCA] Não use enforcador. [F2]" in section_md
    assert "- Guia curta." in section_md
    assert "\n-\n" not in section_md and not section_md.endswith("-")
    assert module["review_markers"] == 3

    dumped = json.dumps(module, ensure_ascii=False)
    assert "VERIFICAR" not in dumped
    assert "segredo interno" not in dumped

    assert module["quick_cards"] == [{"title": "Passeio seguro", "markdown": "1. Peitoral ajustado.\n2. Guia curta."}]
    assert module["quiz"][0] == {
        "question": "Qual equipamento?",
        "options": ["Enforcador", "Peitoral", "Retrátil", "Coleira apertada"],
        "correct_index": 1,
        "explanation": "O peitoral distribui a força.",
        "source": "F1",
    }
    assert module["quiz"][1]["options"] == ["Correr atrás", "Chamar com calma"]
    assert module["quiz"][1]["source"] is None
    assert [s["id"] for s in module["sources"]] == ["F1", "F2"]


def test_crlf_module_with_multiple_quick_cards_and_table(tmp_path):
    path = _write(tmp_path, "08-primeiros-socorros.md", M08, crlf=True)
    module = builder.build_module(path, order=2)

    assert [c["title"] for c in module["quick_cards"]] == ["8.4 Sangramento", "8.7 Insolação"]
    assert module["quick_cards"][1]["markdown"] == "1. Resfrie primeiro."
    section_md = module["sections"][0]["markdown"]
    assert "| Pequeno | Abrace |" in section_md
    assert section_md.endswith("**Lembre sempre: em dúvida, é emergência.**")
    assert "\r" not in json.dumps(module)
    assert len(module["quiz"]) == 1
    assert module["sources"] == [{"id": "F6", "text": "VIN. *Bleeding*. 2026."}]


def test_local_module_scope_uf_and_city(tmp_path):
    path = _write(tmp_path, "10b-regras-da-cidade-salvador.md", M10B)
    module = builder.build_module(path, order=3)
    assert module["scope"] == "local"
    assert module["uf"] == "BA"
    assert module["cidade"] == "Salvador"
    assert module["sections"][0]["id"] == "10B.1"
    assert module["quick_cards"] == []


def test_bundle_orders_modules_and_ignores_junk_files(tmp_path):
    src = _source(tmp_path)
    for junk in ("[!FACA]", "0){var", "notas.txt"):
        _write(src, junk, "lixo")
    _write(src / "_arquivo", "01-antigo.md", M01)

    bundle = builder.build_bundle(src, "0.2", built_at="2026-09-15T00:00:00Z")

    assert [m["id"] for m in bundle["modules"]] == ["m01", "m08", "m10b-salvador"]
    assert [m["order"] for m in bundle["modules"]] == [1, 2, 3]
    assert bundle["status"] == "draft"
    assert bundle["vet_signature"] is None
    assert bundle["version"] == "0.2"
    assert bundle["pass_threshold"] == 80
    assert bundle["quick_guide_modules"] == ["m08"]


def test_vet_approved_requires_signature_and_zero_markers(tmp_path):
    src = _source(tmp_path)
    signature = {"nome": "Dra. Teste", "crmv": "CRMV-BA 0000", "data": "2026-10-01"}

    with pytest.raises(builder.BundleError, match="exige"):
        builder.build_bundle(src, "1.0", status="vet_approved")
    with pytest.raises(builder.BundleError, match="m01=3"):
        builder.build_bundle(src, "1.0", status="vet_approved", vet_signature=signature)

    (src / "01-passeio-seguro.md").unlink()
    bundle = builder.build_bundle(src, "1.0", status="vet_approved", vet_signature=signature)
    assert bundle["status"] == "vet_approved"
    assert bundle["vet_signature"] == signature


def test_invalid_quiz_index_and_duplicate_ids_raise(tmp_path):
    bad = tmp_path / "bad"
    _write(bad, "01-a.md", M01.replace("correta: 1\n  explicacao: \"O peitoral", "correta: 7\n  explicacao: \"O peitoral"))
    with pytest.raises(builder.BundleError, match="correta"):
        builder.build_bundle(bad, "0.2")

    dup = tmp_path / "dup"
    _write(dup, "01-a.md", M01)
    _write(dup, "02-b.md", M01)
    with pytest.raises(builder.BundleError, match="duplicados"):
        builder.build_bundle(dup, "0.2")


def test_invalid_version_raises(tmp_path):
    with pytest.raises(builder.BundleError, match="versão"):
        builder.build_bundle(_source(tmp_path), "../0.2")


def test_main_writes_json_and_returns_2_on_error(tmp_path):
    src = _source(tmp_path)
    out = tmp_path / "out"
    assert builder.main(["--versao", "0.2", "--source", str(src), "--output", str(out)]) == 0
    data = json.loads((out / "manual-0.2.json").read_text(encoding="utf-8"))
    assert len(data["modules"]) == 3
    assert builder.main(["--versao", "x", "--source", str(src), "--output", str(out)]) == 2
