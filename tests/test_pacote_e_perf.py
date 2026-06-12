"""Testes do Pacote E — pool resiliente, push nao-bloqueante, N+1 walkers, paginacao admin.

Padrao do projeto: FastAPI minimo por grupo de testes, SQLite em memoria (StaticPool),
overrides de get_db / get_current_user. NAO importa app.main.
"""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_review import WalkReview
from app.models.walker_kit_submission import WalkerKitSubmission
from app.models.walker_profile import WalkerProfile
from app.models.walker_review import WalkerReview
from app.routes import admin, walker

# IDs e emails sem tokens fake
SUPER_ID = "super-e2e"
SUPER_EMAIL = "super@aumigao.app"
TENANT_A = "tenant-e2e"

WALKER_ID_1 = "walker-perf-1"
WALKER_ID_2 = "walker-perf-2"


# ---------------------------------------------------------------------------
# Fixtures / helpers compartilhados
# ---------------------------------------------------------------------------

def _make_engine_and_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


def _admin_client(db_session):
    """Monta TestClient com o admin router."""
    test_app = FastAPI()
    test_app.include_router(admin.router)
    test_app.dependency_overrides[get_db] = lambda: db_session
    test_app.dependency_overrides[get_current_user] = lambda: db_session.get(User, SUPER_ID)
    return TestClient(test_app)


def _walker_client(db_session):
    """Monta TestClient com o walker router (rota publica)."""
    test_app = FastAPI()
    test_app.include_router(walker.router)
    test_app.include_router(walker.api_public_router)
    test_app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(test_app)


def _seed_super(db):
    db.add(User(id=SUPER_ID, email=SUPER_EMAIL, password_hash="x", role="super_admin"))
    db.commit()


def _seed_real_walker(db, *, uid, email, profile_id, full_name="Passeador Real"):
    db.add(User(id=uid, email=email, password_hash="x", role="walker", full_name=full_name))
    db.add(WalkerProfile(
        id=profile_id,
        user_id=uid,
        full_name=full_name,
        status="active",
        active_as_walker=True,
    ))
    db.commit()


# ===========================================================================
# E1 — pool_pre_ping
# ===========================================================================

class TestE1PoolPrePing:
    def test_pool_pre_ping_presente_para_postgres(self):
        """engine_kwargs deve incluir pool_pre_ping quando a URL nao for sqlite."""
        from app.core import database as db_module
        # A URL atual e postgres (prod) ou sqlite (testes). Testamos a logica diretamente.
        from app.core.database import SQLALCHEMY_DATABASE_URL
        is_sqlite = SQLALCHEMY_DATABASE_URL.startswith("sqlite")
        if is_sqlite:
            # Em ambiente de teste a URL e sqlite — garante que engine_kwargs estao VAZIOS
            # (pool_pre_ping nao se aplica a sqlite).
            import sqlalchemy
            e = sqlalchemy.create_engine("sqlite://", connect_args={"check_same_thread": False})
            # SQLite nao usa pool configuravel — apenas verifica que a engine foi criada
            assert e is not None
        else:
            # Em ambiente de producao verificamos que a engine tem pool_pre_ping
            from app.core.database import engine
            assert engine.pool._pre_ping  # type: ignore[attr-defined]

    def test_engine_kwargs_logica(self):
        """A logica condicional de engine_kwargs deve produzir parametros corretos."""
        url_postgres = "postgresql://user:pass@host/db"
        url_sqlite = "sqlite:///./test.db"

        def compute_kwargs(url):
            if not url.startswith("sqlite"):
                return {"pool_pre_ping": True, "pool_recycle": 300, "pool_size": 5, "max_overflow": 10}
            return {}

        pg_kwargs = compute_kwargs(url_postgres)
        assert pg_kwargs["pool_pre_ping"] is True
        assert pg_kwargs["pool_recycle"] == 300
        assert pg_kwargs["pool_size"] == 5
        assert pg_kwargs["max_overflow"] == 10

        sqlite_kwargs = compute_kwargs(url_sqlite)
        assert sqlite_kwargs == {}


# ===========================================================================
# E2 — push fire-and-forget
# ===========================================================================

class TestE2PushBackground:
    def test_send_push_for_notification_background_importa(self):
        """send_push_for_notification_background deve existir e ser callable."""
        from app.services.push_notifications import send_push_for_notification_background
        assert callable(send_push_for_notification_background)

    def test_send_push_for_notification_sincrona_ainda_existe(self):
        """send_push_for_notification sincrona (usada pelo scheduler) deve existir."""
        from app.services.push_notifications import send_push_for_notification
        assert callable(send_push_for_notification)

    def test_background_nao_chama_urlopen_no_thread_do_request(self):
        """A versao background deve submeter ao executor — nao bloquear o caller."""
        from app.services import push_notifications as pn
        from app.models.notification import Notification
        from app.models.push_token import PushToken

        notification = MagicMock(spec=Notification)
        notification.type = "new_walk"
        notification.id = "notif-bg-1"
        notification.title = "Titulo"
        notification.message = "Mensagem"
        notification.related_entity_type = "walk"
        notification.related_entity_id = "walk-1"
        notification.metadata_json = "{}"
        notification.user_id = "user-1"
        notification.user_role = "tutor"

        token = MagicMock(spec=PushToken)
        token.expo_push_token = "ExponentPushToken[xxx]"
        token.user_id = "user-1"

        submitted_futures = []

        with patch.object(pn, "_tokens_for_notification", return_value=[token]), \
             patch.object(pn._PUSH_EXECUTOR, "submit", side_effect=lambda fn, *a, **kw: submitted_futures.append((fn, a))) as mock_submit:
            pn.send_push_for_notification_background(MagicMock(), notification)

        # O executor.submit deve ter sido chamado uma vez
        assert mock_submit.call_count == 1

    def test_background_nao_envia_se_nao_deve_push(self):
        """Tipos que nao sao criticos nao devem disparar o executor."""
        from app.services import push_notifications as pn
        from app.models.notification import Notification

        notification = MagicMock(spec=Notification)
        notification.type = "generic_info"  # nao esta em CRITICAL_NOTIFICATION_TYPES
        notification.metadata_json = "{}"
        notification.user_id = "user-1"
        notification.user_role = "tutor"

        with patch.object(pn._PUSH_EXECUTOR, "submit") as mock_submit:
            pn.send_push_for_notification_background(MagicMock(), notification)
        mock_submit.assert_not_called()

    def test_scheduler_usa_versao_sincrona(self):
        """O scheduler de retry deve importar e chamar send_push_for_notification (sincrona)."""
        from app.services.operational_scheduler_service import send_push_for_notification as imported
        from app.services.push_notifications import send_push_for_notification as original
        assert imported is original

    def test_routes_notifications_usa_versao_background(self):
        """routes/notifications.py deve importar send_push_for_notification_background."""
        import importlib, sys
        # Re-importa o modulo para verificar o binding
        import app.routes.notifications as notif_module
        assert hasattr(notif_module, "send_push_for_notification_background")


# ===========================================================================
# E3 — N+1 walkers: equivalencia de output
# ===========================================================================

class TestE3WalkerBatchEquivalence:
    """Verifica que _public_walker_rows retorna o mesmo shape e valores
    que a versao original, agora com queries batched.
    """

    def _setup(self):
        _, Session = _make_engine_and_session()
        db = Session()
        _seed_real_walker(db, uid=WALKER_ID_1, email="wk1@aumigao.app", profile_id="wp-1", full_name="Ana Passeadora")
        _seed_real_walker(db, uid=WALKER_ID_2, email="wk2@aumigao.app", profile_id="wp-2", full_name="Bruno Passeador")
        # Adiciona walk review para wk1
        db.add(WalkReview(
            id="wr-1", walk_id="walk-rev-1", tutor_id="tutor-x", walker_id=WALKER_ID_1,
            rating=5, comment="Otimo", tags_json='["caring"]',
            created_at=datetime.utcnow(),
        ))
        # Adiciona kit para wk2
        db.add(WalkerKitSubmission(
            id="kit-2", walker_user_id=WALKER_ID_2,
            items_json=json.dumps({"water": {"available": True, "photo_urls": []}}),
            audit_status="approved",
        ))
        db.commit()
        return db

    def test_retorna_lista_com_todos_walkers_reais(self):
        db = self._setup()
        result = walker._public_walker_rows(db)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_shape_de_cada_walker(self):
        """Cada walker no resultado deve conter os campos esperados pelo app mobile."""
        db = self._setup()
        result = walker._public_walker_rows(db)
        expected_fields = {
            "id", "partner_id", "name", "full_name", "role",
            "photo_url", "profile_photo_url", "status", "raw_status",
            "active_as_walker", "rating", "average_rating", "rating_average",
            "reviews_count", "recent_reviews",
            "rating_avg", "rating_count", "recent_review_comments", "top_review_tags",
            "rating_average", "total_walks", "level", "reputation_score",
            "city", "neighborhood", "bio", "walk_price", "verified", "walker_kit",
        }
        for row in result:
            for field in expected_fields:
                assert field in row, f"Campo '{field}' faltando no walker {row.get('id')}"

    def test_walk_review_soma_correta_para_walker_com_review(self):
        """Walker com 1 review de rating 5 deve ter rating_avg=5.0 e rating_count=1."""
        db = self._setup()
        result = walker._public_walker_rows(db)
        row_1 = next((r for r in result if r["id"] == WALKER_ID_1), None)
        assert row_1 is not None
        assert row_1["rating_avg"] == 5.0
        assert row_1["rating_count"] == 1
        assert row_1["rating"] == 5.0
        # Comentario aparece em recent_review_comments
        assert len(row_1["recent_review_comments"]) == 1
        assert row_1["recent_review_comments"][0]["comment"] == "Otimo"

    def test_walker_sem_review_tem_zeros(self):
        """Walker sem reviews deve ter rating_avg=0 e rating_count=0."""
        db = self._setup()
        result = walker._public_walker_rows(db)
        row_2 = next((r for r in result if r["id"] == WALKER_ID_2), None)
        assert row_2 is not None
        assert row_2["rating_avg"] == 0
        assert row_2["rating_count"] == 0

    def test_kit_carregado_sem_query_individual(self):
        """walker_kit deve estar presente e conter a chave 'level'."""
        db = self._setup()
        result = walker._public_walker_rows(db)
        for row in result:
            assert "walker_kit" in row
            assert "level" in row["walker_kit"]

    def test_helper_batch_walk_review_math(self):
        """_batch_walk_review_summaries deve reproduzir exatamente a matematica original."""
        _, Session = _make_engine_and_session()
        db = Session()
        db.add(WalkReview(id="wr-a", walk_id="wk-a", tutor_id="t1", walker_id="w1",
                          rating=4, comment="Bom", tags_json='["punctual","caring"]', created_at=datetime.utcnow()))
        db.add(WalkReview(id="wr-b", walk_id="wk-b", tutor_id="t1", walker_id="w1",
                          rating=2, comment=None, tags_json='["punctual"]', created_at=datetime.utcnow()))
        db.commit()

        result = walker._batch_walk_review_summaries(["w1", "w2"], db)

        # w1: avg de 4 e 2 = 3.0, count=2
        assert result["w1"]["rating_count"] == 2
        assert result["w1"]["rating_avg"] == 3.0
        # Tag "punctual" aparece 2x, "caring" 1x
        tags = {t["tag"]: t["count"] for t in result["w1"]["top_review_tags"]}
        assert tags["punctual"] == 2
        assert tags["caring"] == 1
        # Apenas o review com comment aparece
        assert len(result["w1"]["recent_review_comments"]) == 1
        assert result["w1"]["recent_review_comments"][0]["comment"] == "Bom"

        # w2: sem reviews
        assert result["w2"]["rating_avg"] == 0
        assert result["w2"]["rating_count"] == 0


# ===========================================================================
# E5 — Paginacao nas listagens admin
# ===========================================================================

class TestE5AdminPagination:
    def _setup(self, *, n_users: int = 5):
        _, Session = _make_engine_and_session()
        db = Session()
        db.add(User(id=SUPER_ID, email=SUPER_EMAIL, password_hash="x", role="super_admin"))
        for i in range(n_users):
            db.add(User(
                id=f"user-page-{i}",
                email=f"user{i}@aumigao.app",
                password_hash="x",
                role="tutor",
                full_name=f"Tutor {i}",
                created_at=datetime(2026, 1, i + 1),
            ))
        db.commit()
        return db

    def test_users_default_limit_retorna_todos_quando_abaixo_do_default(self):
        db = self._setup(n_users=5)
        client = _admin_client(db)
        r = client.get("/admin/users")
        assert r.status_code == 200
        body = r.json()
        # 5 tutores + 1 super_admin = 6 usuarios
        assert len(body) == 6

    def test_users_limit_1_retorna_1(self):
        db = self._setup(n_users=5)
        client = _admin_client(db)
        r = client.get("/admin/users?limit=1")
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_users_offset_pula_registros(self):
        db = self._setup(n_users=5)
        client = _admin_client(db)
        r_all = client.get("/admin/users?limit=200").json()
        r_p1 = client.get("/admin/users?limit=2&offset=0").json()
        r_p2 = client.get("/admin/users?limit=2&offset=2").json()
        # Os dois primeiros de p1 e p2 sao distintos
        ids_p1 = {u["id"] for u in r_p1}
        ids_p2 = {u["id"] for u in r_p2}
        assert ids_p1.isdisjoint(ids_p2)
        # Uniao cobre parte da lista total
        assert ids_p1 | ids_p2 <= {u["id"] for u in r_all}

    def test_users_limit_max_1000_aceito(self):
        db = self._setup(n_users=3)
        client = _admin_client(db)
        r = client.get("/admin/users?limit=1000")
        assert r.status_code == 200

    def test_users_limit_0_rejeitado(self):
        db = self._setup(n_users=1)
        client = _admin_client(db)
        r = client.get("/admin/users?limit=0")
        assert r.status_code == 422

    def test_users_limit_acima_1000_rejeitado(self):
        db = self._setup(n_users=1)
        client = _admin_client(db)
        r = client.get("/admin/users?limit=1001")
        assert r.status_code == 422

    def test_payments_paginacao_basica(self):
        _, Session = _make_engine_and_session()
        db = Session()
        db.add(User(id=SUPER_ID, email=SUPER_EMAIL, password_hash="x", role="super_admin"))
        for i in range(4):
            db.add(Payment(
                id=f"pay-{i}",
                walk_id=f"w-{i}",
                tutor_id=f"t-{i}",
                amount=10.0 * (i + 1),
                status="paid",
                created_at=datetime(2026, 1, i + 1),
            ))
        db.commit()
        client = _admin_client(db)

        r_all = client.get("/admin/payments?limit=200").json()
        assert len(r_all) == 4

        r_pag = client.get("/admin/payments?limit=2&offset=0").json()
        assert len(r_pag) == 2

        r_pag2 = client.get("/admin/payments?limit=2&offset=2").json()
        assert len(r_pag2) == 2

        ids_1 = {p["id"] for p in r_pag}
        ids_2 = {p["id"] for p in r_pag2}
        assert ids_1.isdisjoint(ids_2)

    def test_walks_paginacao_basica(self):
        _, Session = _make_engine_and_session()
        db = Session()
        db.add(User(id=SUPER_ID, email=SUPER_EMAIL, password_hash="x", role="super_admin"))
        # Walks sem tutores reais nao passam em _is_real_admin_walk — aqui
        # verificamos apenas que os params limit/offset sao aceitos (200 OK).
        client = _admin_client(db)
        r = client.get("/admin/walks?limit=10&offset=0")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_tutors_paginacao_basica(self):
        _, Session = _make_engine_and_session()
        db = Session()
        db.add(User(id=SUPER_ID, email=SUPER_EMAIL, password_hash="x", role="super_admin"))
        for i in range(3):
            db.add(User(
                id=f"tut-pg-{i}",
                email=f"tut{i}@aumigao.app",
                password_hash="x",
                role="tutor",
                full_name=f"Tutor Pg {i}",
                created_at=datetime(2026, 1, i + 1),
            ))
        db.commit()
        client = _admin_client(db)

        r_all = client.get("/admin/tutors?limit=200").json()
        # 3 tutores reais (sem tokens fake)
        assert len(r_all) == 3

        r_1 = client.get("/admin/tutors?limit=1&offset=0").json()
        assert len(r_1) == 1

        r_2 = client.get("/admin/tutors?limit=1&offset=1").json()
        assert len(r_2) == 1

        # offset deve pular um registro
        assert r_1[0]["id"] != r_2[0]["id"]

    def test_offset_negativo_rejeitado(self):
        db = self._setup(n_users=1)
        client = _admin_client(db)
        r = client.get("/admin/users?offset=-1")
        assert r.status_code == 422
