"""Testes de ROTA (camada HTTP) do modulo app/routes/tenant_branding.py.

Padrao do projeto (ver tests/test_routes_onda1.py e tests/test_routes_auth.py):
monta um FastAPI MINIMO com apenas os routers de branding, SQLite em memoria
(StaticPool), overrides de get_db / get_current_user. NAO importa app.main (que
conecta no banco de PROD).

Cobre:
- GET publico /tenants/current/branding-runtime (defaults quando nao ha branding,
  resolucao para o tenant default).
- GET publico /tenants/{tenant_id}/branding-runtime (por id e por slug; fallback
  para default quando id desconhecido).
- PATCH admin /api/admin/tenants/current/branding: happy path (persiste, incrementa
  version), 401 (sem auth), 403 (sem permissao RBAC).
- POST /api/admin/tenants/current/branding/upload-image: happy path (mock storage),
  kind invalido 422, sem RBAC 403.
- Enforcement powered_by: free com powered_by_enabled=False -> 422; pro -> aceito.
"""
import io
from unittest.mock import patch as mock_patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant, TenantBranding
from app.models.user import User
from app.routes import tenant_branding
from app.services.tenant_branding_service import DEFAULT_PRIMARY_COLOR, DEFAULT_SECONDARY_COLOR
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
ADMIN_ID = "admin-test"


def build(*, branding: dict | None = None, admin_role: str = "super_admin"):
    """Monta app minimo com os routers de branding e um SQLite em memoria isolado.

    O admin e seedado com role super_admin: a rede de seguranca do RBAC
    (user_has_permission) sempre concede permissao a super_admin sem precisar
    seedar papeis/permissoes.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # slug = DEFAULT para resolve_current_tenant/get_default_tenant resolver este
    # tenant sem criar outro (request.state.tenant_id nao existe no TestClient).
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role=admin_role, tenant_id=TENANT_ID))
    if branding is not None:
        db.add(TenantBranding(tenant_id=TENANT_ID, **branding))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(tenant_branding.router)
    test_app.include_router(tenant_branding.api_router)
    test_app.include_router(tenant_branding.admin_api_router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app), db


def as_admin(client, db, user_id=ADMIN_ID):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


# ---------------------------------------------------- GET current (publico) ---
def test_get_current_branding_defaults_when_no_branding():
    """Sem registro de branding: usa nome do tenant e cores default."""
    client, _ = build()
    r = client.get("/tenants/current/branding-runtime")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == TENANT_ID
    assert body["display_name"] == "Aumigao"  # cai no tenant.name
    assert body["app_name"] == "Aumigao"
    assert body["logo_url"] == ""
    assert body["primary_color"] == DEFAULT_PRIMARY_COLOR
    assert body["secondary_color"] == DEFAULT_SECONDARY_COLOR
    assert body["accent_color"] == ""
    assert body["powered_by_enabled"] is True
    assert body["version"] == 1


def test_get_current_branding_no_auth_required():
    """A rota publica nao exige Authorization header."""
    client, _ = build()
    r = client.get("/tenants/current/branding-runtime")
    assert r.status_code == 200


def test_get_current_branding_reflects_stored_values():
    client, _ = build(branding={
        "display_name": "Pet Lovers",
        "app_name": "PetLoversApp",
        "logo_url": "https://cdn/logo.png",
        "primary_color": "#ff0000",
        "secondary_color": "#00ff00",
        "accent_color": "#0000ff",
        "powered_by_enabled": False,
        "published_version": 7,
    })
    body = client.get("/tenants/current/branding-runtime").json()
    assert body["display_name"] == "Pet Lovers"
    assert body["app_name"] == "PetLoversApp"
    assert body["logo_url"] == "https://cdn/logo.png"
    assert body["primary_color"] == "#ff0000"
    assert body["accent_color"] == "#0000ff"
    assert body["powered_by_enabled"] is False
    assert body["version"] == 7


def test_get_current_branding_via_api_prefix():
    """O api_router expoe o mesmo endpoint sob /api/tenants."""
    client, _ = build()
    r = client.get("/api/tenants/current/branding-runtime")
    assert r.status_code == 200
    assert r.json()["tenant_id"] == TENANT_ID


# -------------------------------------------------- GET by tenant_id (pub) ----
def test_get_branding_by_id():
    client, _ = build(branding={"display_name": "Marca X"})
    body = client.get(f"/tenants/{TENANT_ID}/branding-runtime").json()
    assert body["tenant_id"] == TENANT_ID
    assert body["display_name"] == "Marca X"


def test_get_branding_by_slug():
    client, _ = build()
    body = client.get(f"/tenants/{DEFAULT_TENANT_SLUG}/branding-runtime").json()
    assert body["tenant_id"] == TENANT_ID


def test_get_branding_unknown_id_falls_back_to_default():
    """tenant_id inexistente cai no tenant default (_resolve_tenant)."""
    client, _ = build()
    r = client.get("/tenants/nao-existe-123/branding-runtime")
    assert r.status_code == 200
    assert r.json()["tenant_id"] == TENANT_ID


# ------------------------------------------------------ PATCH admin (RBAC) ----
def test_patch_branding_requires_auth_401():
    """Sem Authorization header e sem override -> get_current_user real -> 401."""
    client, _ = build()
    r = client.patch("/api/admin/tenants/current/branding", json={"display_name": "X"})
    assert r.status_code == 401


def test_patch_branding_forbidden_without_permission_403():
    """Usuario comum (sem permissao branding.*) -> 403."""
    client, db = build(admin_role="cliente")
    as_admin(client, db)
    r = client.patch("/api/admin/tenants/current/branding", json={"display_name": "X"})
    assert r.status_code == 403
    assert "permiss" in r.json()["detail"].lower()


def test_patch_branding_happy_path_persists_and_bumps_version():
    client, db = build()
    as_admin(client, db)
    r = client.patch("/api/admin/tenants/current/branding", json={
        "display_name": "Nova Marca",
        "app_name": "NovaApp",
        "logo_url": "https://cdn/novo.png",
        "primary_color": "#123456",
        "secondary_color": "#654321",
        "accent_color": "#abcdef",
        "powered_by_enabled": False,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_name"] == "Nova Marca"
    assert body["app_name"] == "NovaApp"
    assert body["logo_url"] == "https://cdn/novo.png"
    assert body["primary_color"] == "#123456"
    assert body["accent_color"] == "#abcdef"
    assert body["powered_by_enabled"] is False
    # cria o registro e incrementa published_version (0 -> 1)
    assert body["version"] == 1

    stored = db.query(TenantBranding).filter(TenantBranding.tenant_id == TENANT_ID).first()
    assert stored is not None
    assert stored.display_name == "Nova Marca"
    assert stored.published_version == 1


def test_patch_branding_increments_existing_version():
    client, db = build(branding={"display_name": "Antiga", "published_version": 4})
    as_admin(client, db)
    r = client.patch("/api/admin/tenants/current/branding", json={"display_name": "Atualizada"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 5  # 4 -> 5


def test_patch_branding_then_get_reflects_update():
    """Apos publicar, o GET publico devolve os novos valores."""
    client, db = build()
    as_admin(client, db)
    client.patch("/api/admin/tenants/current/branding", json={
        "display_name": "Publicada", "primary_color": "#aa0011",
    })
    body = client.get("/tenants/current/branding-runtime").json()
    assert body["display_name"] == "Publicada"
    assert body["primary_color"] == "#aa0011"


# ─────────────────────────────────────────────────────────── Upload de imagem ──


# Imagem PNG minima valida (magic bytes reais: 8 bytes de assinatura PNG).
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


def _png_file(name: str = "logo.png") -> tuple[str, tuple]:
    """Prepara o tuple multipart para o TestClient (magic bytes crus)."""
    return ("file", (name, io.BytesIO(_PNG_MAGIC), "image/png"))


def _real_png_file(name: str = "logo.png", size: tuple = (40, 20)) -> tuple[str, tuple]:
    """PNG de verdade (decodificável), pois kind=logo passa por normalize_logo_image
    (app/lib/branding_image.py) — os magic-bytes crus de _png_file não sobrevivem
    ao Image.load() e derrubariam o teste com 422."""
    img = Image.new("RGBA", size, (10, 20, 30, 255))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return ("file", (name, buffer, "image/png"))


def test_upload_branding_image_happy_logo(tmp_path):
    """Upload happy path: kind=logo, PNG valido -> 201 + url retornada (normalizada)."""
    client, db = build()
    as_admin(client, db)

    # Mocka storage.save (evita IO real) e _branding_image_url (desacopla da
    # resolucao de caminho relativo ao UPLOADS_BASE, que nao se aplica ao tmp_path).
    with mock_patch("app.services.object_storage.save") as mock_save, \
         mock_patch("app.routes.tenant_branding._branding_image_url",
                    return_value="https://cdn/tenant_branding_logo-abc.png") as mock_url:
        r = client.post(
            "/api/admin/tenants/current/branding/upload-image?kind=logo",
            files=[_real_png_file()],
        )

    assert r.status_code == 201, r.text
    body = r.json()
    assert "url" in body
    assert "logo" in body["url"]
    mock_save.assert_called_once()
    mock_url.assert_called_once()
    # object_storage.save recebeu bytes normalizados (PNG) e content-type PNG,
    # mesmo a entrada já sendo PNG.
    saved_content_type = mock_save.call_args.args[2]
    assert saved_content_type == "image/png"


def test_upload_branding_image_logo_invalid_image_returns_422():
    """kind=logo com bytes que não decodificam como imagem -> 422 amigável."""
    client, db = build()
    as_admin(client, db)

    with mock_patch("app.services.object_storage.save"):
        r = client.post(
            "/api/admin/tenants/current/branding/upload-image?kind=logo",
            files=[_png_file()],  # só magic bytes, não é PNG decodificável de verdade
        )

    assert r.status_code == 422, r.text
    assert "processar a imagem" in r.json()["detail"].lower()


def test_upload_branding_image_happy_icon():
    """Upload happy path: kind=icon."""
    client, db = build()
    as_admin(client, db)

    with mock_patch("app.services.object_storage.save"), \
         mock_patch("app.routes.tenant_branding._branding_image_url",
                    return_value="https://cdn/tenant_branding_icon-abc.png"):
        r = client.post(
            "/api/admin/tenants/current/branding/upload-image?kind=icon",
            files=[_png_file("icon.png")],
        )

    assert r.status_code == 201, r.text
    assert "icon" in r.json()["url"]


def test_upload_branding_image_happy_splash():
    """Upload happy path: kind=splash."""
    client, db = build()
    as_admin(client, db)

    with mock_patch("app.services.object_storage.save"), \
         mock_patch("app.routes.tenant_branding._branding_image_url",
                    return_value="https://cdn/tenant_branding_splash-abc.png"):
        r = client.post(
            "/api/admin/tenants/current/branding/upload-image?kind=splash",
            files=[_png_file("splash.png")],
        )

    assert r.status_code == 201, r.text
    assert "splash" in r.json()["url"]


def test_upload_branding_image_invalid_kind():
    """kind invalido retorna 422."""
    client, db = build()
    as_admin(client, db)

    r = client.post(
        "/api/admin/tenants/current/branding/upload-image?kind=favicon",
        files=[_png_file()],
    )
    assert r.status_code == 422, r.text
    assert "kind" in r.json()["detail"].lower() or "favicon" in r.json()["detail"].lower()


def test_upload_branding_image_no_rbac_403():
    """Usuario sem permissao branding.update -> 403."""
    client, db = build(admin_role="cliente")
    as_admin(client, db)

    r = client.post(
        "/api/admin/tenants/current/branding/upload-image?kind=logo",
        files=[_png_file()],
    )
    assert r.status_code == 403, r.text


def test_upload_branding_image_no_auth_401():
    """Sem autenticacao -> 401."""
    client, _ = build()
    r = client.post(
        "/api/admin/tenants/current/branding/upload-image?kind=logo",
        files=[_png_file()],
    )
    assert r.status_code == 401, r.text


# ──────────────────────────────────────────────── Enforcement powered_by_required ──


def test_patch_branding_free_powered_by_false_returns_422():
    """Plano free nao pode desligar o powered_by (plano exige o selo)."""
    client, db = build(admin_role="super_admin")
    # Muda o tenant para plano free.
    tenant = db.query(Tenant).first()
    tenant.plan = "free"
    db.commit()

    as_admin(client, db)
    r = client.patch("/api/admin/tenants/current/branding", json={
        "display_name": "Teste",
        "powered_by_enabled": False,
    })
    assert r.status_code == 422, r.text
    assert "powered by" in r.json()["detail"].lower() or "plano" in r.json()["detail"].lower()


def test_patch_branding_starter_powered_by_false_returns_422():
    """Plano starter tambem exige o selo (powered_by_required=True em v1)."""
    client, db = build(admin_role="super_admin")
    tenant = db.query(Tenant).first()
    tenant.plan = "starter"
    db.commit()

    as_admin(client, db)
    r = client.patch("/api/admin/tenants/current/branding", json={
        "display_name": "Teste",
        "powered_by_enabled": False,
    })
    assert r.status_code == 422, r.text


def test_patch_branding_business_powered_by_false_allowed():
    """Plano business/pro nao exige o selo — pode desligar."""
    client, db = build(admin_role="super_admin")
    # build() ja cria com plan="business"
    as_admin(client, db)
    r = client.patch("/api/admin/tenants/current/branding", json={
        "display_name": "Pro Marca",
        "powered_by_enabled": False,
    })
    assert r.status_code == 200, r.text
    assert r.json()["powered_by_enabled"] is False


def test_patch_branding_free_powered_by_true_allowed():
    """Plano free pode ligar o powered_by (apenas desligar e bloqueado)."""
    client, db = build(admin_role="super_admin")
    tenant = db.query(Tenant).first()
    tenant.plan = "free"
    db.commit()

    as_admin(client, db)
    r = client.patch("/api/admin/tenants/current/branding", json={
        "display_name": "Free Marca",
        "powered_by_enabled": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["powered_by_enabled"] is True
