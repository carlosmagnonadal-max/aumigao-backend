"""Schema de presença online do passeador (WK-02)."""
from pydantic import BaseModel


class WalkerOnlineUpdate(BaseModel):
    online: bool
