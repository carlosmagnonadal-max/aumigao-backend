"""app.lib.walk_time — conversão hora local do tenant → UTC (bug 08/07/2026)."""
from datetime import datetime

from app.lib.walk_time import parse_wall_time, walk_start_utc


def test_naive_local_converte_para_utc_bahia():
    # 10:30 em America/Bahia (UTC-3, sem DST) = 13:30 UTC
    assert walk_start_utc("2026-07-08T10:30:00", "America/Bahia") == datetime(2026, 7, 8, 13, 30)


def test_naive_sem_tz_usa_default_bahia():
    assert walk_start_utc("2026-07-08T10:30") == datetime(2026, 7, 8, 13, 30)


def test_string_com_offset_explicito_e_honrada():
    assert walk_start_utc("2026-07-08T10:30:00Z", "America/Bahia") == datetime(2026, 7, 8, 10, 30)
    assert walk_start_utc("2026-07-08T10:30:00-03:00") == datetime(2026, 7, 8, 13, 30)


def test_so_data_assume_inicio_do_dia_local():
    assert walk_start_utc("2026-07-08", "America/Bahia") == datetime(2026, 7, 8, 3, 0)


def test_invalido_e_vazio_devolvem_none():
    assert walk_start_utc(None) is None
    assert walk_start_utc("") is None
    assert walk_start_utc("nao-e-data") is None


def test_tz_invalida_cai_no_default():
    assert walk_start_utc("2026-07-08T10:30", "Fuso/Inexistente") == datetime(2026, 7, 8, 13, 30)


def test_parse_wall_time_preserva_hora_de_parede():
    parsed = parse_wall_time("2026-07-08T10:30")
    assert parsed == datetime(2026, 7, 8, 10, 30)
    assert parsed.tzinfo is None
