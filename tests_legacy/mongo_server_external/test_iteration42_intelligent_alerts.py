import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient


# Module coverage: intelligent alerts queue/filter/settings, grouping behavior, reversible actions, decision audit, and key auth guardrails.


def _resolve_public_base_url() -> str:
    frontend_env = Path("/app/frontend/.env")
    values = dotenv_values(frontend_env) if frontend_env.exists() else {}
    raw = (
        os.environ.get("EXPO_BACKEND_URL")
        or values.get("EXPO_BACKEND_URL")
        or values.get("EXPO_PUBLIC_BACKEND_URL")
    )
    if not raw:
        pytest.skip("EXPO_BACKEND_URL/EXPO_PUBLIC_BACKEND_URL não configurado")
    return str(raw).rstrip("/")


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
        timeout=30,
    )
    assert response.status_code == 200, response.text
    token = response.json().get("access_token")
    assert token
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return session


def _seed_user(db, *, user_id: str, email: str, role: str, full_name: str):
    now_iso = datetime.now(timezone.utc).isoformat()
    db.users.insert_one(
        {
            "id": user_id,
            "full_name": full_name,
            "email": email,
            "password_hash": "$2b$12$gVBjrY7Q0zM2cifDODzj4.2m3gpnqI2s7xMYPx7Yxj75BpwQJ8WQ6",
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


def _seed_alert(
    db,
    *,
    alert_id: str,
    user_id: str,
    user_role: str,
    tipo: str,
    categoria: str,
    nivel: int,
    prioridade_score: float,
    acao_sugerida: str,
    status: str = "pendente",
    metadata: dict | None = None,
):
    now_iso = datetime.now(timezone.utc).isoformat()
    db.system_alerts.insert_one(
        {
            "alert_id": alert_id,
            "alert_signature": f"{tipo}:{user_id}:{acao_sugerida}",
            "tipo_alerta": tipo,
            "categoria": categoria,
            "prioridade_score": prioridade_score,
            "nivel_gravidade": int(nivel),
            "status": status,
            "user_id": user_id,
            "user_role": user_role,
            "contexto": "TEST_CONTEXT",
            "mensagem": f"TEST {tipo}",
            "acao_sugerida": acao_sugerida,
            "acao_final": None,
            "auto_executado": False,
            "justificativa_admin": None,
            "occurrences": 1,
            "metadata": metadata or {},
            "criado_em": now_iso,
            "atualizado_em": now_iso,
        }
    )


@pytest.fixture()
def iter42_scope():
    scope = {
        "user_ids": [],
        "walk_ids": [],
        "alert_ids": [],
        "coupon_ids": [],
        "audit_alert_ids": [],
        "login_identifiers": [],
        "settings_backup": None,
    }
    yield scope

    mongo_client, db = _mongo_db()
    try:
        if scope["walk_ids"]:
            db.walks.delete_many({"id": {"$in": scope["walk_ids"]}})
        if scope["alert_ids"]:
            db.system_alerts.delete_many({"alert_id": {"$in": scope["alert_ids"]}})
        if scope["user_ids"]:
            db.users.delete_many({"id": {"$in": scope["user_ids"]}})
            db.notifications.delete_many({"user_id": {"$in": scope["user_ids"]}})
            db.operational_occurrences.delete_many({"user_id": {"$in": scope["user_ids"]}})
        if scope["coupon_ids"]:
            db.coupons.delete_many({"id": {"$in": scope["coupon_ids"]}})
        if scope["audit_alert_ids"]:
            db.system_alert_audit.delete_many({"alert_id": {"$in": scope["audit_alert_ids"]}})
        if scope["login_identifiers"]:
            db.login_attempts.delete_many({"identifier": {"$in": scope["login_identifiers"]}})
        if scope["settings_backup"] is not None:
            db.system_alert_priority_settings.update_one(
                {"id": "default"},
                {"$set": scope["settings_backup"]},
                upsert=True,
            )
    finally:
        mongo_client.close()


def test_auth_seed_admin_hash_uses_bcrypt_2b_prefix():
    mongo_client, db = _mongo_db()
    try:
        admin = db.users.find_one({"email": "admin@petpasso.com"}, {"_id": 0, "password_hash": 1})
    finally:
        mongo_client.close()

    assert admin is not None
    password_hash = str(admin.get("password_hash") or "")
    assert password_hash.startswith("$2b$")


def test_auth_login_sets_http_only_cookies():
    base_url = _resolve_public_base_url()
    response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "admin@petpasso.com", "password": "Admin@123"},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    set_cookie = response.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie
    assert "refresh_token=" in set_cookie
    assert "HttpOnly" in set_cookie


def test_auth_lockout_after_five_failed_attempts(iter42_scope):
    base_url = _resolve_public_base_url()
    identifier = f"203.0.113.42:admin@petpasso.com"
    iter42_scope["login_identifiers"].append(identifier)

    mongo_client, db = _mongo_db()
    try:
        db.login_attempts.delete_many({"identifier": identifier})
    finally:
        mongo_client.close()

    for _ in range(5):
        fail_response = requests.post(
            f"{base_url}/api/auth/login",
            json={"email": "admin@petpasso.com", "password": "wrong-pass"},
            headers={"x-forwarded-for": "203.0.113.42"},
            timeout=30,
        )
        assert fail_response.status_code == 401

    lock_response = requests.post(
        f"{base_url}/api/auth/login",
        json={"email": "admin@petpasso.com", "password": "wrong-pass"},
        headers={"x-forwarded-for": "203.0.113.42"},
        timeout=30,
    )
    assert lock_response.status_code == 429


def test_get_admin_alerts_returns_queue_sorted_by_priority_score_desc(iter42_scope):
    base_url = _resolve_public_base_url()
    admin_session = _admin_login(base_url)
    mongo_client, db = _mongo_db()

    user_id = f"TEST_ITER42_USER_{uuid.uuid4().hex[:8]}"
    iter42_scope["user_ids"].append(user_id)
    _seed_user(db, user_id=user_id, email=f"iter42_sorted_{uuid.uuid4().hex[:8]}@petpasso.com", role="cliente", full_name="TEST Sorted")

    high_id = f"iter42-high-{uuid.uuid4().hex[:8]}"
    low_id = f"iter42-low-{uuid.uuid4().hex[:8]}"
    iter42_scope["alert_ids"].extend([high_id, low_id])
    _seed_alert(
        db,
        alert_id=high_id,
        user_id=user_id,
        user_role="cliente",
        tipo="TEST_SORT_HIGH",
        categoria="operacional",
        nivel=4,
        prioridade_score=99.0,
        acao_sugerida="mark_occurrence_pending",
    )
    _seed_alert(
        db,
        alert_id=low_id,
        user_id=user_id,
        user_role="cliente",
        tipo="TEST_SORT_LOW",
        categoria="operacional",
        nivel=1,
        prioridade_score=5.0,
        acao_sugerida="mark_occurrence_pending",
    )
    mongo_client.close()

    response = admin_session.get(f"{base_url}/api/admin/alerts", params={"limit": 200}, timeout=35)
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) >= 2
    by_id = {item.get("alert_id"): idx for idx, item in enumerate(rows)}
    assert high_id in by_id
    assert low_id in by_id
    assert by_id[high_id] < by_id[low_id]


def test_get_admin_alerts_supports_status_nivel_tipo_categoria_filters(iter42_scope):
    base_url = _resolve_public_base_url()
    admin_session = _admin_login(base_url)
    mongo_client, db = _mongo_db()

    user_id = f"TEST_ITER42_USER_{uuid.uuid4().hex[:8]}"
    iter42_scope["user_ids"].append(user_id)
    _seed_user(db, user_id=user_id, email=f"iter42_filter_{uuid.uuid4().hex[:8]}@petpasso.com", role="cliente", full_name="TEST Filters")

    target_alert_id = f"iter42-filter-{uuid.uuid4().hex[:8]}"
    noise_alert_id = f"iter42-noise-{uuid.uuid4().hex[:8]}"
    iter42_scope["alert_ids"].extend([target_alert_id, noise_alert_id])

    _seed_alert(
        db,
        alert_id=target_alert_id,
        user_id=user_id,
        user_role="cliente",
        tipo="TEST_FILTER_TARGET",
        categoria="financeiro",
        nivel=4,
        prioridade_score=88.0,
        acao_sugerida="mark_occurrence_pending",
        status="pendente",
    )
    _seed_alert(
        db,
        alert_id=noise_alert_id,
        user_id=user_id,
        user_role="cliente",
        tipo="TEST_FILTER_NOISE",
        categoria="operacional",
        nivel=1,
        prioridade_score=10.0,
        acao_sugerida="mark_occurrence_pending",
        status="executado",
    )
    mongo_client.close()

    response = admin_session.get(
        f"{base_url}/api/admin/alerts",
        params={
            "status": "pendente",
            "nivel_gravidade": 4,
            "tipo_alerta": "TEST_FILTER_TARGET",
            "categoria": "financeiro",
            "limit": 20,
        },
        timeout=35,
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert rows
    assert all(item.get("status") == "pendente" for item in rows)
    assert all(int(item.get("nivel_gravidade")) == 4 for item in rows)
    assert all(item.get("tipo_alerta") == "TEST_FILTER_TARGET" for item in rows)
    assert all(item.get("categoria") == "financeiro" for item in rows)


def test_priority_settings_patch_persists_and_reflects_on_subsequent_get(iter42_scope):
    base_url = _resolve_public_base_url()
    admin_session = _admin_login(base_url)

    original = admin_session.get(f"{base_url}/api/admin/alerts/priority-settings", timeout=35)
    assert original.status_code == 200, original.text
    original_json = original.json()
    iter42_scope["settings_backup"] = dict(original_json)

    payload = {
        "weights": {
            "impacto_financeiro": 40,
            "risco_operacional": 25,
            "reincidencia": 15,
            "proximidade_tempo": 10,
            "frequencia_evento": 10,
        },
        "grouping_windows_hours": {
            "operacional": 20,
            "financeiro": 30,
            "comportamental": 18,
            "sistemico": 9,
        },
        "systemic_region_failure_threshold": 4,
        "systemic_overload_threshold": 9,
    }
    patch_response = admin_session.patch(
        f"{base_url}/api/admin/alerts/priority-settings",
        json=payload,
        timeout=35,
    )
    assert patch_response.status_code == 200, patch_response.text

    get_after = admin_session.get(f"{base_url}/api/admin/alerts/priority-settings", timeout=35)
    assert get_after.status_code == 200, get_after.text
    data = get_after.json()
    assert int(data["grouping_windows_hours"]["financeiro"]) == 30
    assert int(data["grouping_windows_hours"]["sistemico"]) == 9
    assert int(data["systemic_region_failure_threshold"]) == 4
    assert int(data["systemic_overload_threshold"]) == 9


def test_engine_generates_operacional_financeiro_comportamental_sistemico_categories(iter42_scope):
    base_url = _resolve_public_base_url()
    admin_session = _admin_login(base_url)
    now_iso = datetime.now(timezone.utc).isoformat()
    settings_resp = admin_session.get(f"{base_url}/api/admin/alerts/priority-settings", timeout=35)
    assert settings_resp.status_code == 200, settings_resp.text
    settings_data = settings_resp.json()
    systemic_threshold = int(settings_data.get("systemic_region_failure_threshold") or 3)
    no_show_needed = max(4, systemic_threshold)

    mongo_client, db = _mongo_db()
    try:
        walker_id = f"TEST_ITER42_W_{uuid.uuid4().hex[:8]}"
        client_fin_id = f"TEST_ITER42_F_{uuid.uuid4().hex[:8]}"
        client_comp_id = f"TEST_ITER42_C_{uuid.uuid4().hex[:8]}"
        iter42_scope["user_ids"].extend([walker_id, client_fin_id, client_comp_id])

        _seed_user(db, user_id=walker_id, email=f"iter42_w_{uuid.uuid4().hex[:8]}@petpasso.com", role="passeador", full_name="TEST Walker")
        _seed_user(db, user_id=client_fin_id, email=f"iter42_f_{uuid.uuid4().hex[:8]}@petpasso.com", role="cliente", full_name="TEST Financial")
        _seed_user(db, user_id=client_comp_id, email=f"iter42_c_{uuid.uuid4().hex[:8]}@petpasso.com", role="cliente", full_name="TEST Behavioral")

        db.users.update_one(
            {"id": walker_id},
            {
                "$set": {
                    "quality_metrics": {
                        "rating_recent_avg": 4.6,
                        "rating_avg": 4.7,
                        "severe_delay_rate": 20.0,
                        "no_show_rate": 0.0,
                        "completion_rate": 95.0,
                        "rating_std_dev": 0.2,
                    },
                    "updated_at": now_iso,
                }
            },
        )
        db.users.update_one(
            {"id": client_comp_id},
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

        walk_ids = [f"TEST_ITER42_WALK_{uuid.uuid4().hex[:8]}_{idx}" for idx in range(2 + no_show_needed)]
        iter42_scope["walk_ids"].extend(walk_ids)
        walk_docs = [
            {
                "id": walk_ids[0],
                "client_user_id": client_fin_id,
                "walker_user_id": walker_id,
                "status": "Finalizado",
                "refund_amount": 40.0,
                "created_at": now_iso,
                "updated_at": now_iso,
                "pickup_neighborhood": "TEST_REGION_SYS",
                "neighborhood": "TEST_REGION_SYS",
            },
            {
                "id": walk_ids[1],
                "client_user_id": client_fin_id,
                "walker_user_id": walker_id,
                "status": "Finalizado",
                "refund_amount": 45.0,
                "created_at": now_iso,
                "updated_at": now_iso,
                "pickup_neighborhood": "TEST_REGION_SYS",
                "neighborhood": "TEST_REGION_SYS",
            },
        ]

        for idx in range(no_show_needed):
            walk_docs.append(
                {
                    "id": walk_ids[2 + idx],
                    "client_user_id": client_comp_id,
                    "walker_user_id": walker_id,
                    "status": "Não comparecimento do passeador",
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "pickup_neighborhood": "TEST_REGION_SYS",
                    "neighborhood": "TEST_REGION_SYS",
                }
            )

        db.walks.insert_many(walk_docs)
    finally:
        mongo_client.close()

    response_all = admin_session.get(f"{base_url}/api/admin/alerts", params={"limit": 500}, timeout=40)
    assert response_all.status_code == 200, response_all.text
    rows_all = response_all.json()

    assert any(item.get("categoria") == "operacional" and item.get("user_id") == walker_id for item in rows_all)
    assert any(item.get("categoria") == "financeiro" and item.get("user_id") == client_fin_id for item in rows_all)
    assert any(item.get("categoria") == "comportamental" and item.get("user_id") == client_comp_id for item in rows_all)

    response_systemic = admin_session.get(
        f"{base_url}/api/admin/alerts",
        params={"categoria": "sistemico", "limit": 500},
        timeout=40,
    )
    assert response_systemic.status_code == 200, response_systemic.text
    rows_systemic = response_systemic.json()
    assert any("region:test_region_sys" == str(item.get("user_id")) for item in rows_systemic)


def test_grouping_of_similar_alerts_increments_occurrences_instead_of_duplicates(iter42_scope):
    base_url = _resolve_public_base_url()
    admin_session = _admin_login(base_url)
    now_iso = datetime.now(timezone.utc).isoformat()

    mongo_client, db = _mongo_db()
    walker_id = f"TEST_ITER42_GRP_{uuid.uuid4().hex[:8]}"
    iter42_scope["user_ids"].append(walker_id)
    _seed_user(db, user_id=walker_id, email=f"iter42_grp_{uuid.uuid4().hex[:8]}@petpasso.com", role="passeador", full_name="TEST Grouping")
    db.users.update_one(
        {"id": walker_id},
        {
            "$set": {
                "quality_metrics": {
                    "rating_recent_avg": 4.6,
                    "rating_avg": 4.5,
                    "severe_delay_rate": 25.0,
                    "no_show_rate": 0.0,
                    "completion_rate": 95.0,
                    "rating_std_dev": 0.2,
                },
                "updated_at": now_iso,
            }
        },
    )
    mongo_client.close()

    first_run = admin_session.get(
        f"{base_url}/api/admin/alerts",
        params={"tipo_alerta": "WALKER_SEVERE_DELAY_SPIKE", "limit": 200},
        timeout=35,
    )
    assert first_run.status_code == 200, first_run.text
    first_rows = [row for row in first_run.json() if row.get("user_id") == walker_id]
    assert first_rows
    first_alert = first_rows[0]
    first_alert_id = first_alert.get("alert_id")

    mongo_client, db = _mongo_db()
    try:
        first_db_alert = db.system_alerts.find_one({"alert_id": first_alert_id}, {"_id": 0, "occurrences": 1})
    finally:
        mongo_client.close()
    first_occurrences = int((first_db_alert or {}).get("occurrences") or 1)

    second_run = admin_session.get(
        f"{base_url}/api/admin/alerts",
        params={"tipo_alerta": "WALKER_SEVERE_DELAY_SPIKE", "limit": 200},
        timeout=35,
    )
    assert second_run.status_code == 200, second_run.text
    second_rows = [row for row in second_run.json() if row.get("user_id") == walker_id]
    assert second_rows
    second_alert = second_rows[0]

    mongo_client, db = _mongo_db()
    try:
        second_db_alert = db.system_alerts.find_one({"alert_id": first_alert_id}, {"_id": 0, "occurrences": 1})
    finally:
        mongo_client.close()
    second_occurrences = int((second_db_alert or {}).get("occurrences") or 1)

    assert second_alert.get("alert_id") == first_alert_id
    assert second_occurrences >= first_occurrences


def test_post_alert_action_requires_justification_for_ignore_level_2_and_4(iter42_scope):
    base_url = _resolve_public_base_url()
    admin_session = _admin_login(base_url)
    mongo_client, db = _mongo_db()

    user_id = f"TEST_ITER42_J_{uuid.uuid4().hex[:8]}"
    iter42_scope["user_ids"].append(user_id)
    _seed_user(db, user_id=user_id, email=f"iter42_just_{uuid.uuid4().hex[:8]}@petpasso.com", role="cliente", full_name="TEST Justification")

    level2_id = f"iter42-ignore-l2-{uuid.uuid4().hex[:8]}"
    level4_id = f"iter42-ignore-l4-{uuid.uuid4().hex[:8]}"
    iter42_scope["alert_ids"].extend([level2_id, level4_id])
    _seed_alert(
        db,
        alert_id=level2_id,
        user_id=user_id,
        user_role="cliente",
        tipo="TEST_IGNORE_L2",
        categoria="comportamental",
        nivel=2,
        prioridade_score=60.0,
        acao_sugerida="mark_occurrence_pending",
    )
    _seed_alert(
        db,
        alert_id=level4_id,
        user_id=user_id,
        user_role="cliente",
        tipo="TEST_IGNORE_L4",
        categoria="comportamental",
        nivel=4,
        prioridade_score=90.0,
        acao_sugerida="mark_occurrence_pending",
    )
    mongo_client.close()

    no_just_2 = admin_session.post(
        f"{base_url}/api/admin/alerts/{level2_id}/action",
        json={"decision": "ignore", "justification": ""},
        timeout=30,
    )
    assert no_just_2.status_code == 400

    no_just_4 = admin_session.post(
        f"{base_url}/api/admin/alerts/{level4_id}/action",
        json={"decision": "ignore", "justification": ""},
        timeout=30,
    )
    assert no_just_4.status_code == 400

    with_just = admin_session.post(
        f"{base_url}/api/admin/alerts/{level2_id}/action",
        json={"decision": "ignore", "justification": "TEST justificação obrigatória"},
        timeout=30,
    )
    assert with_just.status_code == 200, with_just.text
    assert with_just.json().get("status") == "ignorado"


def test_new_reversible_actions_apply_risk_flag_block_coupon_and_suspend_preselection(iter42_scope):
    base_url = _resolve_public_base_url()
    admin_session = _admin_login(base_url)
    now_iso = datetime.now(timezone.utc).isoformat()

    mongo_client, db = _mongo_db()
    user_id = f"TEST_ITER42_A_{uuid.uuid4().hex[:8]}"
    iter42_scope["user_ids"].append(user_id)
    _seed_user(db, user_id=user_id, email=f"iter42_actions_{uuid.uuid4().hex[:8]}@petpasso.com", role="passeador", full_name="TEST Actions")

    coupon_id = f"TEST_ITER42_COUPON_{uuid.uuid4().hex[:8]}"
    iter42_scope["coupon_ids"].append(coupon_id)
    db.coupons.insert_one(
        {
            "id": coupon_id,
            "code": f"TEST42{uuid.uuid4().hex[:5].upper()}",
            "discount_percent": 15,
            "discount_fixed": 0,
            "max_global_uses": 10,
            "max_uses_per_user": 1,
            "used_count": 0,
            "applicable_walk_types": ["Individual"],
            "is_active": True,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
    )

    alert_apply_risk = f"iter42-action-risk-{uuid.uuid4().hex[:8]}"
    alert_block_coupon = f"iter42-action-coupon-{uuid.uuid4().hex[:8]}"
    alert_suspend = f"iter42-action-suspend-{uuid.uuid4().hex[:8]}"
    iter42_scope["alert_ids"].extend([alert_apply_risk, alert_block_coupon, alert_suspend])

    _seed_alert(
        db,
        alert_id=alert_apply_risk,
        user_id=user_id,
        user_role="passeador",
        tipo="TEST_ACTION_APPLY_RISK",
        categoria="comportamental",
        nivel=3,
        prioridade_score=78.0,
        acao_sugerida="apply_risk_flag",
        metadata={"reason": "TEST_APPLY_RISK"},
    )
    _seed_alert(
        db,
        alert_id=alert_block_coupon,
        user_id=user_id,
        user_role="passeador",
        tipo="TEST_ACTION_BLOCK_COUPON",
        categoria="financeiro",
        nivel=3,
        prioridade_score=80.0,
        acao_sugerida="block_suspicious_coupon",
        metadata={"coupon_id": coupon_id},
    )
    _seed_alert(
        db,
        alert_id=alert_suspend,
        user_id=user_id,
        user_role="passeador",
        tipo="TEST_ACTION_SUSPEND_PRE",
        categoria="operacional",
        nivel=3,
        prioridade_score=82.0,
        acao_sugerida="suspend_auto_preselection",
    )
    mongo_client.close()

    for alert_id in [alert_apply_risk, alert_block_coupon, alert_suspend]:
        action_response = admin_session.post(
            f"{base_url}/api/admin/alerts/{alert_id}/action",
            json={"decision": "confirm", "justification": "TEST confirm action"},
            timeout=30,
        )
        assert action_response.status_code == 200, action_response.text
        assert action_response.json().get("status") == "executado"

    mongo_client, db = _mongo_db()
    try:
        user_row = db.users.find_one({"id": user_id}, {"_id": 0})
        coupon_row = db.coupons.find_one({"id": coupon_id}, {"_id": 0})
    finally:
        mongo_client.close()

    assert user_row is not None
    assert bool(user_row.get("behavior_risk_flag_active")) is True
    assert user_row.get("auto_preselection_suspended_until")
    assert coupon_row is not None
    assert coupon_row.get("status") == "blocked"
    assert bool(coupon_row.get("blocked_by_system")) is True


def test_decision_audit_is_written_into_system_alert_audit(iter42_scope):
    base_url = _resolve_public_base_url()
    admin_session = _admin_login(base_url)
    mongo_client, db = _mongo_db()

    user_id = f"TEST_ITER42_AUDIT_{uuid.uuid4().hex[:8]}"
    iter42_scope["user_ids"].append(user_id)
    _seed_user(db, user_id=user_id, email=f"iter42_audit_{uuid.uuid4().hex[:8]}@petpasso.com", role="cliente", full_name="TEST Audit")

    alert_id = f"iter42-audit-{uuid.uuid4().hex[:8]}"
    iter42_scope["alert_ids"].append(alert_id)
    iter42_scope["audit_alert_ids"].append(alert_id)
    _seed_alert(
        db,
        alert_id=alert_id,
        user_id=user_id,
        user_role="cliente",
        tipo="TEST_AUDIT_ALERT",
        categoria="operacional",
        nivel=2,
        prioridade_score=55.0,
        acao_sugerida="mark_occurrence_pending",
    )
    mongo_client.close()

    action_response = admin_session.post(
        f"{base_url}/api/admin/alerts/{alert_id}/action",
        json={"decision": "review_later", "justification": "TEST audit trail"},
        timeout=30,
    )
    assert action_response.status_code == 200, action_response.text

    mongo_client, db = _mongo_db()
    try:
        audit = db.system_alert_audit.find_one({"alert_id": alert_id}, {"_id": 0})
    finally:
        mongo_client.close()

    assert audit is not None
    assert audit.get("alert_id") == alert_id
    assert audit.get("decisao_admin") == "review_later"
    assert audit.get("status_resultante") == "revisar_depois"
