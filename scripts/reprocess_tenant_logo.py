"""reprocess_tenant_logo.py — Reprocessa a logo publicada de um tenant.

Contexto: a normalização de logo (app/lib/branding_image.py — trim de margem
embutida + limite de tamanho + saída em PNG) só passou a rodar no upload a
partir de 2026-07-16. Logos publicadas ANTES disso (ou que alguém queira
ré-normalizar) continuam cruas no storage. Este script pega a logo_url atual
de um tenant, baixa os bytes, normaliza pelo MESMO caminho do endpoint de
upload e publica o resultado como uma nova logo_url.

Uso (one-off; DATABASE_URL com role dona + PUBLIC_BACKEND_URL + (em produção)
R2_BUCKET/R2_ENDPOINT/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY no env):
    python scripts/reprocess_tenant_logo.py <tenant_slug>

O que faz:
  1. Busca o tenant pelo slug e a linha de tenant_branding (logo_url atual).
  2. Baixa os bytes da logo atual (HTTP GET na URL pública — mesmo arquivo
     servido hoje no app).
  3. Normaliza via app.lib.branding_image.normalize_logo_image (mesmo trim +
     resize + PNG do endpoint de upload em app/routes/tenant_branding.py).
  4. Sobe o resultado no storage (object_storage.save — R2 em produção, disco
     local senão) sob um novo nome de arquivo (não sobrescreve o antigo).
  5. Atualiza tenant_branding.logo_url para a nova URL e incrementa
     published_version (mesmo padrão de scripts/set_aumigao_logo.py) — o app
     pega a mudança no próximo boot / invalidação de cache.

⚠️ Produção: NÃO EXECUTAR sem confirmar o slug e revisar a logo resultante.
O arquivo antigo NÃO é apagado (troca só a referência em tenant_branding).
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import psycopg2

# scripts/ não é a raiz do pacote "app" — quando rodado como
# `python scripts/reprocess_tenant_logo.py`, sys.path[0] é o próprio
# diretório scripts/, então "import app...." falharia sem isto (funciona
# independente do cwd de onde o script é chamado).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.lib.branding_image import InvalidImageError, normalize_logo_image  # noqa: E402
from app.services import object_storage  # noqa: E402
from app.services.signed_uploads import UPLOAD_ROOT  # noqa: E402

if len(sys.argv) != 2:
    sys.exit("uso: reprocess_tenant_logo.py <tenant_slug>")
slug = sys.argv[1]

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    sys.exit("DATABASE_URL ausente")

conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = False
cur = conn.cursor()

cur.execute("SELECT id, name FROM tenants WHERE slug = %s", (slug,))
row = cur.fetchone()
if not row:
    sys.exit(f"tenant '{slug}' nao encontrado")
tenant_id, tenant_name = row
print(f"tenant: {tenant_name} ({tenant_id})")

cur.execute(
    "SELECT id, logo_url, published_version FROM tenant_branding WHERE tenant_id = %s",
    (tenant_id,),
)
branding = cur.fetchone()
if not branding:
    sys.exit(f"tenant '{slug}' nao tem linha de tenant_branding (admin nunca publicou)")
branding_id, logo_url, published_version = branding
if not logo_url:
    sys.exit(f"tenant '{slug}' nao tem logo_url definida — nada para reprocessar")
print(f"logo_url atual: {logo_url} | published_version: {published_version}")

# Baixa os bytes da logo atual (URL publica — o mesmo arquivo servido no app hoje).
try:
    with urlopen(logo_url, timeout=30) as resp:  # noqa: S310 - URL vem do proprio banco, nao de input externo
        content = resp.read()
        content_type = resp.headers.get_content_type() or "image/png"
except URLError as exc:
    sys.exit(f"falha ao baixar logo_url: {exc}")

print(f"baixado: {len(content)} bytes (content-type original: {content_type})")

try:
    normalized, normalized_content_type = normalize_logo_image(content, content_type)
except InvalidImageError as exc:
    sys.exit(f"falha ao normalizar a imagem: {exc}")

print(f"normalizado: {len(normalized)} bytes ({normalized_content_type})")

destination = UPLOAD_ROOT / "tenant-branding-images" / f"tenant_branding_logo-{uuid.uuid4().hex}.png"
object_storage.save(destination, normalized, normalized_content_type)
print(f"upload feito em: {destination}")

public_base = (os.getenv("PUBLIC_BACKEND_URL") or "").strip().rstrip("/")
if not public_base:
    sys.exit(
        "PUBLIC_BACKEND_URL ausente — nao foi possivel montar a nova URL publica. "
        f"O arquivo ja foi salvo em: {destination} (tenant_branding NAO foi alterado; "
        "rode de novo com PUBLIC_BACKEND_URL setado para concluir)."
    )
relative = destination.relative_to(UPLOAD_ROOT).as_posix()
new_logo_url = f"{public_base}/uploads/{relative}"

cur.execute(
    "UPDATE tenant_branding SET logo_url = %s, published_version = published_version + 1,"
    " updated_at = NOW() WHERE id = %s",
    (new_logo_url, branding_id),
)
conn.commit()

cur.execute(
    "SELECT logo_url, published_version FROM tenant_branding WHERE id = %s",
    (branding_id,),
)
novo_logo, nova_versao = cur.fetchone()
print(f"verificado: logo_url = {novo_logo} | published_version = {nova_versao}")
conn.close()
