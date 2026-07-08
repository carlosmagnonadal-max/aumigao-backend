"""Contrato do kit-issue-report (2026-07-08): o app envia missing_items como
LISTA + "note"; legado enviava dict {chave: bool}. Ambos devem normalizar."""
from app.routes.walks import _KIT_ITEM_LABELS, _normalize_missing_kit_items, WalkKitIssueReportRequest


class TestNormalizeMissingKitItems:
    def test_list_format_from_app(self):
        assert _normalize_missing_kit_items(["has_water", "has_bags"]) == ["has_water", "has_bags"]

    def test_legacy_dict_format_falsy_means_missing(self):
        assert _normalize_missing_kit_items({"has_water": False, "has_bowl": True}) == ["has_water"]

    def test_none_and_empty(self):
        assert _normalize_missing_kit_items(None) == []
        assert _normalize_missing_kit_items([]) == []
        assert _normalize_missing_kit_items({}) == []

    def test_labels_cover_all_kit_keys(self):
        assert set(_KIT_ITEM_LABELS) == {"has_water", "has_bowl", "has_bags", "has_first_aid"}


class TestRequestSchemaAcceptsAppPayload:
    def test_list_missing_items_and_note_do_not_422(self):
        payload = WalkKitIssueReportRequest.model_validate(
            {"confirm_report": True, "missing_items": ["has_water"], "note": "Faltou água no passeio"}
        )
        assert payload.missing_items == ["has_water"]
        assert payload.note == "Faltou água no passeio"
