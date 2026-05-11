"""Pydantic schema for the application configuration.

This mirrors ``config/default.toml`` exactly. The loader
(:mod:`clippycap.infra.config.loader`) merges the layered sources, expands the
``@path`` tokens, then validates the result against :class:`Config`. Unknown keys
*and* missing keys both fail validation -- there are no code-level defaults or
fallbacks anywhere; ``config/default.toml`` is the single source of every default.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_HEX_COLOR = r"^#[0-9a-fA-F]{6}$"


class _Section(BaseModel):
    """Base for every config section: reject unknown keys, immutable once loaded."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AppConfig(_Section):
    name: str = Field(min_length=1)
    data_dir: str = Field(min_length=1)
    write_local_on_first_run: bool


class ServerConfig(_Section):
    host: str = Field(min_length=1)
    port: int = Field(ge=0, le=65535)
    open_browser: bool


class ShellConfig(_Section):
    mode: Literal["pywebview", "browser"]
    window_title: str = Field(min_length=1)
    window_width: int = Field(ge=200)
    window_height: int = Field(ge=200)
    remember_window_state: bool


class UiConfig(_Section):
    theme: Literal["dark", "light"]
    accent_color: str = Field(pattern=_HEX_COLOR)
    grid_density: Literal["small", "medium", "large"]
    default_sort: str = Field(min_length=1)
    locale: str = Field(min_length=1)
    markdown_in_notes: bool


class ScanConfig(_Section):
    recursive_default: bool
    follow_symlinks: bool
    include_hidden_files: bool
    ignored_globs: list[str]
    skip_modified_within_seconds: int = Field(ge=0)
    scan_on_startup: bool
    periodic_scan_minutes: int = Field(ge=0)


class IdentityConfig(_Section):
    # The strategy name is resolved against the IdentityStrategy registry at runtime
    # (plugins may register more), so it cannot be a closed Literal here.
    strategy: str = Field(min_length=1)
    partial_hash_head_bytes: int = Field(ge=0)
    partial_hash_tail_bytes: int = Field(ge=0)
    dedup_by_volume_file_id: bool


class HashCacheConfig(_Section):
    enabled: bool


class ThumbnailsConfig(_Section):
    cache_dir: str = Field(min_length=1)
    format: Literal["webp", "jpg", "png"]
    width: int = Field(ge=16)
    per_video_count: int = Field(ge=1)
    poster_at_fraction: float = Field(ge=0.0, le=1.0)
    regenerate_on_metadata_change: bool


class FfmpegConfig(_Section):
    enabled: bool
    # "auto" -> probe (bundled, PATH, common install dirs); "@bundled" -> only the bundled
    # binary; otherwise an absolute path. Resolved by infra/media at runtime.
    ffmpeg_path: str = Field(min_length=1)
    ffprobe_path: str = Field(min_length=1)


class VideoMediaConfig(_Section):
    extensions: list[str] = Field(min_length=1)
    recorded_at_filename_patterns: list[str]
    identity_strategy: str = Field(min_length=1)


class MediaConfig(_Section):
    ffmpeg: FfmpegConfig
    video: VideoMediaConfig


class PlayerConfig(_Section):
    speeds: list[float] = Field(min_length=1)
    default_speed: float = Field(gt=0)
    skip_seconds: float = Field(gt=0)
    skip_seconds_fine: float = Field(gt=0)
    pause_on_add_note: bool
    prefer_rvfc: bool


class EditingConfig(_Section):
    # ffmpeg cuts at keyframes by default (instant, lossless, but the cut can land up to a GOP
    # early); set true to re-encode for frame-accurate cuts (slower, a tiny quality cost).
    reencode: bool
    reencode_crf: int = Field(ge=0, le=51)
    reencode_preset: str = Field(min_length=1)
    # Trim / remove-segment / cut overwrite the original file in place; when true, a copy of the
    # pre-edit file is kept beside it first.
    keep_original_backup: bool
    # Name for the new clip produced by save/cut-segment. Placeholders: {stem} {start} {end} {ext}
    # ({start}/{end} formatted as "mm-ss").
    new_clip_name_template: str = Field(min_length=1)
    # Description shown on the reference auto-created from an extracted clip back to its source; "" => no link.
    # (The name is kept for back-compat with existing local.toml files; it's a plain label now, not a type.)
    excerpt_reference_type: str


class SearchConfig(_Section):
    fts_enabled: bool


class LoggingConfig(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    dir: str = Field(min_length=1)
    max_files: int = Field(ge=1)
    max_file_mb: float = Field(gt=0)


class PluginsConfig(_Section):
    dirs: list[str]
    enabled: list[str]
    disabled: list[str]


class ReferenceTypeSeed(_Section):
    name: str = Field(min_length=1)
    reverse: str | None = None
    color: str = Field(pattern=_HEX_COLOR)


class SeedConfig(_Section):
    reference_types: list[ReferenceTypeSeed]


class FirstRunConfig(_Section):
    suggest_sources: list[str]


class Config(_Section):
    """The fully resolved, validated application configuration."""

    app: AppConfig
    server: ServerConfig
    shell: ShellConfig
    ui: UiConfig
    scan: ScanConfig
    identity: IdentityConfig
    hash_cache: HashCacheConfig
    thumbnails: ThumbnailsConfig
    media: MediaConfig
    player: PlayerConfig
    editing: EditingConfig
    keybindings: dict[str, str]
    sort: dict[str, str]
    search: SearchConfig
    logging: LoggingConfig
    plugins: PluginsConfig
    seed: SeedConfig
    firstrun: FirstRunConfig

    @model_validator(mode="after")
    def _check_cross_references(self) -> Config:
        if self.ui.default_sort not in self.sort:
            raise ValueError(
                f"[ui].default_sort = {self.ui.default_sort!r} is not one of the [sort] keys "
                f"{sorted(self.sort)}"
            )
        if self.player.default_speed not in self.player.speeds:
            raise ValueError(
                f"[player].default_speed = {self.player.default_speed} is not listed in "
                f"[player].speeds = {self.player.speeds}"
            )
        return self
