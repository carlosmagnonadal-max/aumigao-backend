"""Tarefa A (Wave 5) — persistência de max_dog_size no PUT /walker/profile.

Reusa o build minimo de tests/test_routes_walker_core.py (app só com o router de
walker, SQLite em memória, overrides de get_db / get_current_user).
"""
from tests.test_routes_walker_core import build, WALKER_ID
from app.models.walker_profile import WalkerProfile


def test_put_profile_persists_max_dog_size():
    client, db = build(profile_kwargs={"bio": "exp", "city": "Salvador"})
    r = client.put("/walker/profile", json={"max_dog_size": "Pequeno"})
    assert r.status_code == 200, r.text
    assert r.json()["max_dog_size"] == "Pequeno"
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == WALKER_ID).first()
    assert profile.max_dog_size == "Pequeno"


def test_put_profile_rejects_invalid_max_dog_size_422():
    client, _ = build(profile_kwargs={"bio": "exp", "city": "Salvador"})
    r = client.put("/walker/profile", json={"max_dog_size": "Gigante"})
    assert r.status_code == 422, r.text


def test_put_profile_default_max_dog_size_is_grande_when_omitted():
    # Perfil novo sem max_dog_size explícito mantém o default permissivo "Grande".
    client, db = build(profile_kwargs={"bio": "exp", "city": "Salvador"})
    r = client.put("/walker/profile", json={"city": "Salvador"})
    assert r.status_code == 200, r.text
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == WALKER_ID).first()
    assert profile.max_dog_size == "Grande"
