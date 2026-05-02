import os
import uuid
from datetime import timedelta
from datetime import datetime, timezone

import bcrypt
import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: admin system alerts list/action flows, alert engine triggers (walker/client/disintermediation), and auth CORS preflight regression.


def _mongo_db():
    backend_env = dotenv_values("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")
    if not mongo_url or not db_name:
        pytest.skip("MONGO_URL/DB_NAME não configurados")
    client = MongoClient(str(mongo_url).strip().strip('"'))
    db = client[str(db_name).strip().strip('"')]
    return client, db


def _admin_login(base_url: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": "admin@petpasso.com", "password": "Admin@123"},
        timeout=25,
    )
    assert response.status_code == 200, response.text
    token = response.json().get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


@pytest.fixture()
def iter41_cleanup_scope():
    scope = {
        "user_ids": [],
        "walk_ids": [],
        "alert_ids": [],
    }
    yield scope

    mongo_client, db = _mongo_db()
    try:
        if scope["walk_ids"]:
            db.walks.delete_many({"id": {"$in": scope["walk_ids"]}})
        if scope["user_ids"]:
            db.users.delete_many({"id": {"$in": scope["user_ids"]}})
            db.notifications.delete_many({"user_id": {"$in": scope["user_ids"]}})
            db.operational_occurrences.delete_many({"user_id": {"$in": scope["user_ids"]}})
        if scope["alert_ids"]:
            db.system_alerts.delete_many({"alert_id": {"$in": scope["alert_ids"]}})
    finally:
        mongo_client.close()


def _insert_user(db, *, user_id: str, email: str, role: str, full_name: str):
    now_iso = datetime.now(timezone.utc).isoformat()
    password_hash = bcrypt.hashpw("Iter41@123".encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    db.users.insert_one(
        {
            "id": user_id,
            "full_name": full_name,
            "email": email,
            "password_hash": password_hash,
            "role": role,
            "isAdmin": False,
            "isActive": True,
            "accepted_terms": True,
            "accepted_privacy": True,
            "accepted_lgpd": True,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
    )


def _seed_alert(db, *, alert_id: str, user_id: str, user_role: str, tipo: str, nivel: int, action: str):
    now_iso = datetime.now(timezone.utc).isoformat()
    db.system_alerts.insert_one(
        {
            "alert_id": alert_id,
            "alert_signature": f"{tipo}:{user_id}:{action}:{uuid.uuid4().hex[:8]}",
            "tipo_alerta": tipo,
            "nivel_gravidade": int(nivel),
            "status": "pendente",
            "user_id": user_id,
            "user_role": user_role,
            "mensagem": f"TEST {tipo}",
            "acao_sugerida": action,
            "acao_final": None,
            "auto_executado": False,
            "justificativa_admin": None,
            "occurrences": 1,
            "metadata": {},
            "criado_em": now_iso,
            "atualizado_em": now_iso,
        }
    )


def test_get_admin_alerts_lists_alerts_with_status_level_and_type(base_url, iter41_cleanup_scope):
    admin_session = _admin_login(base_url)
    mongo_client, db = _mongo_db()

    user_id = f"TEST_ITER41_USER_{uuid.uuid4().hex[:8]}"
    alert_id = f"test-iter41-list-{uuid.uuid4().hex[:8]}"
    iter41_cleanup_scope["user_ids"].append(user_id)
    iter41_cleanup_scope["alert_ids"].append(alert_id)

    try:
        _insert_user(db, user_id=user_id, email=f"iter41_list_{uuid.uuid4().hex[:8]}@petpasso.com", role="cliente", full_name="TEST Iter41 List")
        _seed_alert(
            db,
            alert_id=alert_id,
            user_id=user_id,
            user_role="cliente",
            tipo="TEST_ITER41_LIST",
            nivel=2,
            action="mark_occurrence_pending",
        )
    finally:
        mongo_client.close()

    response = admin_session.get(
        f"{base_url}/api/admin/alerts",
        params={"tipo_alerta": "TEST_ITER41_LIST", "limit": 50},
        timeout=25,
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert isinstance(rows, list)
    assert rows

    matched = next((row for row in rows if row.get("alert_id") == alert_id), None)
    assert matched is not None
    assert matched.get("status") in {"pendente", "executado", "ignorado", "revisar_depois"}
    assert int(matched.get("nivel_gravidade")) == 2
    assert matched.get("tipo_alerta") == "TEST_ITER41_LIST"


def test_ignore_requires_justification_for_level_2_and_4_alerts(base_url, iter41_cleanup_scope):
    admin_session = _admin_login(base_url)
    mongo_client, db = _mongo_db()

    user_id = f"TEST_ITER41_USER_{uuid.uuid4().hex[:8]}"
    alert_level2 = f"test-iter41-l2-{uuid.uuid4().hex[:8]}"
    alert_level4 = f"test-iter41-l4-{uuid.uuid4().hex[:8]}"
    iter41_cleanup_scope["user_ids"].append(user_id)
    iter41_cleanup_scope["alert_ids"].extend([alert_level2, alert_level4])

    try:
        _insert_user(db, user_id=user_id, email=f"iter41_ignore_{uuid.uuid4().hex[:8]}@petpasso.com", role="cliente", full_name="TEST Iter41 Ignore")
        _seed_alert(
            db,
            alert_id=alert_level2,
            user_id=user_id,
            user_role="cliente",
            tipo="TEST_ITER41_IGNORE_L2",
            nivel=2,
            action="mark_occurrence_pending",
        )
        _seed_alert(
            db,
            alert_id=alert_level4,
            user_id=user_id,
            user_role="cliente",
            tipo="TEST_ITER41_IGNORE_L4",
            nivel=4,
            action="mark_occurrence_pending",
        )
    finally:
        mongo_client.close()

    response_l2_no_just = admin_session.post(
        f"{base_url}/api/admin/alerts/{alert_level2}/action",
        json={"decision": "ignore", "justification": ""},
        timeout=25,
    )
    assert response_l2_no_just.status_code == 400

    response_l4_no_just = admin_session.post(
        f"{base_url}/api/admin/alerts/{alert_level4}/action",
        json={"decision": "ignore", "justification": ""},
        timeout=25,
    )
    assert response_l4_no_just.status_code == 400

    response_l2_with_just = admin_session.post(
        f"{base_url}/api/admin/alerts/{alert_level2}/action",
        json={"decision": "ignore", "justification": "Validação operacional iter41."},
        timeout=25,
    )
    assert response_l2_with_just.status_code == 200, response_l2_with_just.text
    assert response_l2_with_just.json().get("status") == "ignorado"


def test_confirm_executes_permitted_auto_action_for_level_3_alert(base_url, iter41_cleanup_scope):
    admin_session = _admin_login(base_url)
    mongo_client, db = _mongo_db()

    user_id = f"TEST_ITER41_WALKER_{uuid.uuid4().hex[:8]}"
    alert_id = f"test-iter41-l3-{uuid.uuid4().hex[:8]}"
    iter41_cleanup_scope["user_ids"].append(user_id)
    iter41_cleanup_scope["alert_ids"].append(alert_id)

    try:
        _insert_user(db, user_id=user_id, email=f"iter41_l3_{uuid.uuid4().hex[:8]}@petpasso.com", role="passeador", full_name="TEST Iter41 Walker")
        _seed_alert(
            db,
            alert_id=alert_id,
            user_id=user_id,
            user_role="passeador",
            tipo="TEST_ITER41_CONFIRM_L3",
            nivel=3,
            action="reduce_matching_priority",
        )
    finally:
        mongo_client.close()

    confirm_response = admin_session.post(
        f"{base_url}/api/admin/alerts/{alert_id}/action",
        json={"decision": "confirm", "justification": "Confirmando ação automática."},
        timeout=25,
    )
    assert confirm_response.status_code == 200, confirm_response.text
    body = confirm_response.json()
    assert body.get("status") == "executado"
    assert body.get("auto_executado") is True
    assert "Prioridade no matching reduzida" in str(body.get("acao_final") or "")

    mongo_client, db = _mongo_db()
    try:
        user_row = db.users.find_one({"id": user_id}, {"_id": 0})
    finally:
        mongo_client.close()

    assert user_row is not None
    assert user_row.get("match_penalty_until")
    assert float(user_row.get("match_penalty_points") or 0.0) >= 0.02


def test_alert_triggers_generated_for_walker_client_and_disintermediation(base_url, iter41_cleanup_scope):
    admin_session = _admin_login(base_url)
    now_iso = datetime.now(timezone.utc).isoformat()

    walker_id = f"TEST_ITER41_WALKER_{uuid.uuid4().hex[:8]}"
    client_id = f"TEST_ITER41_CLIENT_{uuid.uuid4().hex[:8]}"
    disinter_client_id = f"TEST_ITER41_DISINT_{uuid.uuid4().hex[:8]}"
    iter41_cleanup_scope["user_ids"].extend([walker_id, client_id, disinter_client_id])

    walk_ids = [
        f"TEST_ITER41_WALK_{uuid.uuid4().hex[:8]}_{idx}" for idx in range(4)
    ]
    iter41_cleanup_scope["walk_ids"].extend(walk_ids)

    mongo_client, db = _mongo_db()
    try:
        _insert_user(db, user_id=walker_id, email=f"iter41_walker_{uuid.uuid4().hex[:8]}@petpasso.com", role="passeador", full_name="TEST Iter41 Walker Trigger")
        _insert_user(db, user_id=client_id, email=f"iter41_client_{uuid.uuid4().hex[:8]}@petpasso.com", role="cliente", full_name="TEST Iter41 Client Trigger")
        _insert_user(db, user_id=disinter_client_id, email=f"iter41_disint_{uuid.uuid4().hex[:8]}@petpasso.com", role="cliente", full_name="TEST Iter41 Disinter Trigger")

        db.users.update_one(
            {"id": walker_id},
            {
                "$set": {
                    "quality_metrics": {
                        "rating_recent_avg": 4.5,
                        "rating_avg": 4.6,
                        "severe_delay_rate": 19.0,
                        "no_show_rate": 0.0,
                        "completion_rate": 95.0,
                        "rating_std_dev": 0.3,
                    },
                    "updated_at": now_iso,
                }
            },
        )

        db.users.update_one(
            {"id": disinter_client_id},
            {
                "$set": {
                    "flag_suspeita_desintermediacao": True,
                    "desintermediacao_flag_reason": "CONTACT_ATTEMPT",
                    "desintermediacao_flagged_at": now_iso,
                    "desintermediacao_flag_expires_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                    "updated_at": now_iso,
                }
            },
        )

        db.walks.insert_many(
            [
                {
                    "id": walk_ids[0],
                    "client_user_id": client_id,
                    "walker_user_id": walker_id,
                    "status": "Cancelado",
                    "tipoCancelamento": "cliente",
                    "cancellation_justified_by_system": False,
                    "cancellation_justified_by_admin": False,
                    "updated_at": now_iso,
                    "created_at": now_iso,
                },
                {
                    "id": walk_ids[1],
                    "client_user_id": client_id,
                    "walker_user_id": walker_id,
                    "status": "Cancelado",
                    "tipoCancelamento": "cliente",
                    "cancellation_justified_by_system": False,
                    "cancellation_justified_by_admin": False,
                    "updated_at": now_iso,
                    "created_at": now_iso,
                },
                {
                    "id": walk_ids[2],
                    "client_user_id": client_id,
                    "walker_user_id": walker_id,
                    "status": "Finalizado",
                    "updated_at": now_iso,
                    "created_at": now_iso,
                },
                {
                    "id": walk_ids[3],
                    "client_user_id": disinter_client_id,
                    "walker_user_id": walker_id,
                    "status": "Finalizado",
                    "updated_at": now_iso,
                    "created_at": now_iso,
                },
            ]
        )
    finally:
        mongo_client.close()

    alerts_response = admin_session.get(f"{base_url}/api/admin/alerts", params={"limit": 500}, timeout=30)
    assert alerts_response.status_code == 200, alerts_response.text
    rows = alerts_response.json()
    assert isinstance(rows, list)

    walker_alert = next(
        (row for row in rows if row.get("user_id") == walker_id and row.get("tipo_alerta") == "WALKER_SEVERE_DELAY_SPIKE"),
        None,
    )
    client_alert = next(
        (row for row in rows if row.get("user_id") == client_id and row.get("tipo_alerta") == "CLIENT_HIGH_CANCEL_RATE"),
        None,
    )
    disinter_alert = next(
        (row for row in rows if row.get("user_id") == disinter_client_id and row.get("tipo_alerta") == "DISINTERMEDIATION_CONTACT_ATTEMPT"),
        None,
    )

    assert walker_alert is not None
    assert int(walker_alert.get("nivel_gravidade")) == 3
    assert client_alert is not None
    assert int(client_alert.get("nivel_gravidade")) == 2
    assert disinter_alert is not None
    assert int(disinter_alert.get("nivel_gravidade")) == 3


def test_auth_cors_preflight_allows_credentials_with_explicit_origin(base_url):
    response = requests.options(
        f"{base_url}/api/auth/login",
        headers={
            "Origin": base_url,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=20,
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("Access-Control-Allow-Origin") == base_url
    assert response.headers.get("Access-Control-Allow-Credentials") == "true"
