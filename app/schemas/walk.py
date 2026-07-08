from datetime import datetime
from pydantic import BaseModel, Field
from app.schemas.common import ORMModel

class WalkCreate(BaseModel):
    # Sec-P3: max_length defensivos — anti-DoS/log-injection. Limites generosos.
    pet_id: str = Field(..., max_length=100)
    walker_id: str | None = Field(None, max_length=100)
    walker_selection_mode: str | None = Field(None, max_length=100)
    scheduled_date: str = Field(..., max_length=50)
    duration_minutes: int
    price: float
    pickup_method: str = Field("Buscar em casa", max_length=100)
    modality: str = Field("standard", max_length=100)
    destination: str = Field("", max_length=500)
    # mig 0101: coordenadas do destino do Pet Tour (par coerente — os dois ou nenhum;
    # exigem destination não-vazio). Null para standard/legado/flag OFF.
    destination_lat: float | None = Field(None, ge=-90, le=90)
    destination_lng: float | None = Field(None, ge=-180, le=180)
    address_snapshot: str = Field("", max_length=2000)
    notes: str = Field("", max_length=2000)
    # mig 0100: ponto de encontro dedicado (substitui hack "Ponto de encontro: X" em notes).
    # Trio coerente: meeting_point exige lat+lng; sem meeting_point os 3 ficam null
    # (válido para pickup_method=Buscar em casa).
    meeting_point: str | None = Field(None, max_length=500)
    meeting_lat: float | None = Field(None, ge=-90, le=90)
    meeting_lng: float | None = Field(None, ge=-180, le=180)

class WalkUpdateStatus(BaseModel):
    status: str

class WalkResponse(ORMModel):
    id: str
    tutor_id: str
    walker_id: str | None = None
    pet_id: str
    pet_name: str | None = None
    pet_photo_url: str | None = None
    tutor_name: str | None = None
    client_name: str | None = None
    walker_name: str | None = None
    scheduled_date: str
    walk_date: str | None = None
    walk_time: str | None = None
    duration_minutes: int
    price: float
    status: str
    operational_status: str | None = None
    operationalStatus: str | None = None
    walker_selection_mode: str | None = None
    walkerSelectionMode: str | None = None
    assigned_walker_id: str | None = None
    assignedWalkerId: str | None = None
    current_attempt: int | None = None
    current_matching_attempt: int | None = None
    max_attempts: int | None = None
    max_matching_attempts: int | None = None
    confirmation_expires_at: datetime | None = None
    walker_confirmation_expires_at: datetime | None = None
    matching_started_at: datetime | None = None
    matching_finished_at: datetime | None = None
    no_walker_reason: str | None = None
    # Decisão do tutor (teste real 08/07): SEM estes campos aqui o response_model
    # DESCARTAVA o que o serializador mandava e o card reagendar/trocar/estorno
    # nunca chegava ao app. payment_cutoff_at idem (countdown do prazo de pagamento).
    tutor_decision_required: bool = False
    decision_reason: str | None = None
    is_exclusive_walker: bool = False
    payment_cutoff_at: str | None = None
    # Mig 0104: experiencia do passeio (xixi/coco) registrada pelo passeador.
    # Declarados aqui senao o response_model DESCARTA (gotcha 08/07).
    did_pee: bool | None = None
    did_poop: bool | None = None
    pickup_region_label: str | None = None
    pickup_distance_label: str | None = None
    pickup_privacy_level: str | None = None
    matching_attempts: list[dict] = Field(default_factory=list)
    operational_logs: list[dict] = Field(default_factory=list)
    pickup_method: str
    modality: str | None = None
    destination: str | None = None
    # mig 0101: coordenadas do destino do Pet Tour (lidas pelo app do passeador).
    destination_lat: float | None = None
    destination_lng: float | None = None
    address_snapshot: str
    notes: str
    # mig 0100: ponto de encontro dedicado (lido pelo app do passeador).
    meeting_point: str | None = None
    meeting_lat: float | None = None
    meeting_lng: float | None = None
    created_at: datetime
    # Aditivo (review P2 #3): app do passeador esconde o formulário de observação
    # do passeio quando False. Default False para compat com serializers de listagem.
    walk_observations_enabled: bool = False
