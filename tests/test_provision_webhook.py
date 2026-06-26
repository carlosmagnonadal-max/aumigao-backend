from app.routes.payments import _provision_safe

class P:  # payment-like
    id = "pp1"; amount = 100; platform_amount = 20; walker_amount = 80; tenant_id = "t1"

def test_provision_safe_never_raises(monkeypatch):
    # força erro interno e garante que não propaga
    import app.routes.payments as mod
    def boom(*a, **k): raise RuntimeError("x")
    monkeypatch.setattr(mod, "compute_and_store_provision", boom, raising=False)
    _provision_safe(db=None, tenant_id="t1", payment_like=P(), revenue_type="walk_commission")  # não levanta
