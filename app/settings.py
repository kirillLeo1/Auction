from pydantic_settings import BaseSettings
from pydantic import computed_field

class Settings(BaseSettings):
    BOT_TOKEN: str
    ADMIN_IDS: str
    DATABASE_URL: str
    CHANNEL_ID: int
    BASE_URL: str
    MONOPAY_TOKEN: str
    MONOPAY_REDIRECT_URL: str
    HOLD_HOURS: int = 24  # 24 години очікування оплати
    TZ: str = "Europe/Kyiv"
    MONOPAY_SKIP_SIGNATURE: bool = False
    MANAGER_CHAT_ID: int = 0

    @computed_field
    @property
    def admin_id_set(self) -> set[int]:
        return {int(x.strip()) for x in self.ADMIN_IDS.split(',') if x.strip()}

settings = Settings()
