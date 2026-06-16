"""R10 — endurecimento do split contra erro de arredondamento (centavos).

Invariantes (testes de propriedade sobre amostras determinísticas):
1. platform + tenant + walker == amount, SEM sobra/falta de centavo (tolerância 0).
2. O percentual derivado do split (enviado ao gateway), aplicado ao amount, devolve
   o walker_amount contábil — sem centavo perdido/criado (limite do modelo de
   percentual do Asaas: 4 casas → divergência máxima de 1 centavo).
"""
from app.services.payment_split_service import compute_split, walker_percent_from_split

# Amostras determinísticas (sem RNG): valores "redondos", quebrados e com dízimas.
AMOUNTS = [10.0, 33.33, 99.99, 100.0, 100.10, 7.0, 3.0, 250.0, 49.90, 1234.56, 0.0]
COMMISSIONS = [0.0, 5.0, 8.0, 12.0, 20.0, 33.33]
MARGINS = [0.0, 5.0, 10.0]


def test_split_three_parts_sum_to_amount_exactly():
    for a in AMOUNTS:
        for c in COMMISSIONS:
            for m in MARGINS:
                s = compute_split(a, c, m)
                soma = s["platform_amount"] + s["tenant_amount"] + s["walker_amount"]
                assert round(soma, 2) == round(a, 2), (a, c, m, s)


def test_gateway_percent_returns_contable_walker_amount():
    for a in AMOUNTS:
        if a <= 0:
            continue
        for c in COMMISSIONS:
            for m in MARGINS:
                s = compute_split(a, c, m)
                pct = walker_percent_from_split(s)
                gateway_walker = round(a * pct / 100.0, 2)
                # repasse no gateway == repasse contábil (≤ 1 centavo de erro de modelo)
                assert abs(gateway_walker - s["walker_amount"]) <= 0.01, (a, c, m, pct, s)


def test_walker_percent_from_split_zero_total_is_zero():
    s = compute_split(0.0, 12.0, 10.0)
    assert walker_percent_from_split(s) == 0.0


def test_walker_percent_from_split_honors_margin():
    s = compute_split(100.0, 12.0, 10.0)
    assert walker_percent_from_split(s) == 78.0  # não 88
