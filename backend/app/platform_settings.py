"""Panelden yönetilen platform ayarları.

Her istekte veritabanına gitmemek için kısa süreli (TTL) bellek içi önbellek kullanılır.
Panelde yapılan bir değişiklik en fazla TTL saniye içinde tüm API/worker instance'larına yansır.
"""
import time
from dataclasses import dataclass
from decimal import Decimal

_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class PlatformSettings:
    service_enabled: bool
    maintenance_message: str | None
    threshold_review: float
    threshold_block: float
    policy_version: int
    default_rate_limit_per_second: int
    default_rate_limit_per_minute: int
    retention_days: int = 30
    ai_enabled: bool = True
    ai_provider: str = "openai_moderation"
    ai_model: str = "gpt-5-nano"
    ai_vision_model: str = "gpt-5-nano"
    ai_max_calls_per_minute: int = 120
    ai_daily_budget_usd: Decimal = Decimal("5")
    ai_monthly_budget_usd: Decimal = Decimal("50")
    ai_alert_percent: int = 80
    autoban_mode: str = "enforce"
    autoban_auth_fail_limit: int = 50
    autoban_scan_limit: int = 30
    autoban_flood_limit: int = 6000
    autoban_panel_login_limit: int = 20
    autoban_allowlist: tuple = ()
    evidence_retention_hours: int = 72
    legal_hold_enabled: bool = True
    ai_sensitive_media: bool = True
    visual_rules_enabled: bool = True

    @property
    def policy_version_label(self) -> str:
        return f"v{self.policy_version}"


class SettingsCache:
    def __init__(self, ttl: float = _TTL_SECONDS):
        self._ttl = ttl
        self._value: PlatformSettings | None = None
        self._loaded_at = 0.0

    async def get(self, pool) -> PlatformSettings:
        now = time.monotonic()
        if self._value is None or now - self._loaded_at > self._ttl:
            try:
                row = await pool.fetchrow(
                    """
                    SELECT service_enabled, maintenance_message, threshold_review, threshold_block,
                           policy_version, default_rate_limit_per_second, default_rate_limit_per_minute, retention_days,
                           ai_enabled, ai_provider, ai_model, ai_vision_model, ai_max_calls_per_minute,
                           ai_daily_budget_usd, ai_monthly_budget_usd, ai_alert_percent,
                           autoban_mode, autoban_auth_fail_limit, autoban_scan_limit, autoban_flood_limit,
                           autoban_panel_login_limit, autoban_allowlist, evidence_retention_hours,
                           legal_hold_enabled, ai_sensitive_media, visual_rules_enabled
                    FROM platform_settings WHERE id = 1
                    """
                )
            except Exception:
                # Veritabanı geçici olarak yoksa son bilinen ayarlarla devam et (varsa)
                if self._value is not None:
                    return self._value
                raise
            data = dict(row)
            data["autoban_allowlist"] = tuple(data.get("autoban_allowlist") or ())
            self._value = PlatformSettings(**data)
            self._loaded_at = now
        return self._value

    def invalidate(self) -> None:
        self._value = None
