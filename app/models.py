"""Validated request models shared by the core FastAPI routes."""

from pydantic import BaseModel, Field


class ImportXC(BaseModel):
    name: str = ""
    server: str
    username: str
    password: str
    output: str = Field(default="ts", pattern="^(ts|m3u8)$")
    epg_url: str | None = None


class ImportM3U(BaseModel):
    name: str = ""
    m3u_url: str
    epg_url: str | None = None


class EPGPatch(BaseModel):
    epg_url: str


class GroupCreate(BaseModel):
    name: str
    parent_id: str | None = None


class GroupPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    pinned: bool | None = None
    teamarr: bool | None = None
    name_epg: bool | None = None
    category: str | None = None
    export_tag: str | None = None
    parent_id: str | None = None


class GroupReorder(BaseModel):
    group_ids: list[str]


class RuleAdd(BaseModel):
    field: str = Field(pattern="^(source_group|channel_name|any)$")
    pattern: str
    match_type: str = Field(default="contains", pattern="^(contains|starts_with|regex|exact)$")


class ChannelPatch(BaseModel):
    name: str | None = None
    tvg_id: str | None = None
    tvg_name: str | None = None
    enabled: bool | None = None
    favorite: bool | None = None
    logo: str | None = None
    placement_locked: bool | None = None
    backup_urls: list[str] | None = None


class ChannelMove(BaseModel):
    to_group_id: str


class BulkMove(BaseModel):
    channel_ids: list[str]
    to_group_id: str


class BulkToggle(BaseModel):
    channel_ids: list[str]
    enabled: bool


class BulkFavorite(BaseModel):
    channel_ids: list[str]
    favorite: bool


class BulkLock(BaseModel):
    channel_ids: list[str]
    locked: bool


class SettingsUpdate(BaseModel):
    teamarr_enabled: str | None = None
    teamarr_username: str | None = None
    teamarr_password: str | None = None
    teamarr_output: str | None = None
    teamarr_base_url: str | None = None
    refresh_interval_minutes: str | None = None
    epg_window_days: str | None = None
    dispatcharr_auto_refresh: str | None = None
    dispatcharr_url: str | None = None
    dispatcharr_username: str | None = None
    dispatcharr_password: str | None = None
    dispatcharr_m3u_account_id: str | None = None
    dispatcharr_epg_source_id: str | None = None
    ntfy_enabled: str | None = None
    ntfy_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = None


class EPGSourceAdd(BaseModel):
    name: str = ""
    url: str


class BulkRename(BaseModel):
    pattern: str
    replacement: str
    is_regex: bool = False


class BulkDelete(BaseModel):
    channel_ids: list[str]


class SmartGroupsSettings(BaseModel):
    enabled: bool = False
    cities: list[str] = []
    window_hours: int = 4
    custom_keywords: list[str] = []
    locals_enabled: bool = False
    locals_cities: list[str] = []
    locals_include_weather: bool = True


class BulkNameEpg(BaseModel):
    channel_ids: list[str]
    action: str = "name"


class BulkLogo(BaseModel):
    channel_ids: list[str]
    action: str = "clear"
    logo_url: str = ""


class ReorderChannels(BaseModel):
    channel_ids: list[str]


class HealthCheckRequest(BaseModel):
    channel_ids: list[str] = []
    limit: int = 50


class EpgRepairPackRequest(BaseModel):
    group_name: str | None = None
    restore_originals: bool = True
    clear_dummy: bool = True
    fix_suspicious: bool = True
    dry_run: bool = False


class RuleSandboxRequest(BaseModel):
    field: str = "any"
    pattern: str
    match_type: str = "contains"
    source_group: str | None = None
    limit: int = 50


class GroupTemplateSave(BaseModel):
    name: str
    group_id: str | None = None


class LogoOverride(BaseModel):
    team: str
    sport: str
    badge_url: str


class AutoMatchApply(BaseModel):
    matches: list[dict]
