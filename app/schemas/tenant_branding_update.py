from pydantic import BaseModel


class TenantBrandingUpdatePayload(BaseModel):
    display_name: str = ""
    app_name: str = ""
    logo_url: str = ""
    icon_url: str = ""
    splash_image_url: str = ""
    primary_color: str = ""
    secondary_color: str = ""
    powered_by_enabled: bool = True
