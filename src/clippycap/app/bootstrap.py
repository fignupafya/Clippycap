"""The composition root: build a fully wired :class:`Application` from configuration.

This is the one place that imports concrete implementations. It loads the config, creates and
migrates the database, the event bus, the registries; registers the built-in ``video`` media type
and the BLAKE3 identity strategy the same way a plugin would; discovers and loads plugins;
constructs the scanner, the job queue and the application services; and runs first-run setup
(seeding the reference types from the config). Nothing else in the app does any wiring.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from clippycap.app.config_service import ConfigService
from clippycap.app.editing_service import EditingService
from clippycap.app.jobs import ThreadJobQueue
from clippycap.app.reference_service import ReferenceService, ReferenceTypeService
from clippycap.app.scan_service import ScanService
from clippycap.app.services import AssetService, NoteService, TagService
from clippycap.app.source_service import SavedViewService, SourceService
from clippycap.core.entities import ReferenceType
from clippycap.core.ports import MetadataExtractor, Thumbnailer, VideoEditor
from clippycap.infra.config import Config, load_config
from clippycap.infra.config.loader import default_install_dir
from clippycap.infra.db.database import SqliteDatabase
from clippycap.infra.media.ffmpeg import resolve_ffmpeg_tools
from clippycap.infra.media.video_editor import FfmpegVideoEditor, UnavailableVideoEditor
from clippycap.infra.media.video_metadata import FfprobeMetadataExtractor, NoOpMetadataExtractor
from clippycap.infra.media.video_thumbnail import FfmpegThumbnailer, UnavailableThumbnailer
from clippycap.infra.scan.hashing import Blake3IdentityStrategy
from clippycap.infra.scan.scanner import LibraryScanner
from clippycap.media_types.video.video_media_type import VideoMediaType
from clippycap.plugins_runtime.context import PluginContext
from clippycap.plugins_runtime.discovery import discover_and_load
from clippycap.plugins_runtime.event_bus import InProcessEventBus
from clippycap.plugins_runtime.registries import Registries

_log = logging.getLogger(__name__)
_FIRST_RUN_KEY = "first_run_done"
DB_FILENAME = "library.sqlite"
TAG_IMAGES_DIRNAME = "tag-images"


@dataclass
class Application:
    """A fully wired application instance -- handed to the HTTP layer and the desktop shell."""

    config: Config
    database: SqliteDatabase
    event_bus: InProcessEventBus
    registries: Registries
    jobs: ThreadJobQueue
    assets: AssetService
    tags: TagService
    notes: NoteService
    references: ReferenceService
    reference_types: ReferenceTypeService
    sources: SourceService
    saved_views: SavedViewService
    scans: ScanService
    editing: EditingService
    config_service: ConfigService
    loaded_plugins: list[str]
    data_dir: Path
    thumbnail_dir: Path
    tag_images_dir: Path
    install_dir: Path
    ffmpeg_available: bool

    def shutdown(self) -> None:
        self.jobs.shutdown()


def _build_video_media_type(
    config: Config, ffmpeg_path: Path | None, ffprobe_path: Path | None
) -> VideoMediaType:
    extractor: MetadataExtractor = (
        FfprobeMetadataExtractor(ffprobe_path) if ffprobe_path is not None else NoOpMetadataExtractor()
    )
    thumbnailer: Thumbnailer = (
        FfmpegThumbnailer(
            ffmpeg_path, width=config.thumbnails.width, at_fraction=config.thumbnails.poster_at_fraction,
            output_format=config.thumbnails.format,
        )
        if ffmpeg_path is not None
        else UnavailableThumbnailer()
    )
    return VideoMediaType(
        extensions=config.media.video.extensions,
        recorded_at_patterns=config.media.video.recorded_at_filename_patterns,
        identity_strategy_name=config.media.video.identity_strategy,
        metadata_extractor=extractor, thumbnailer=thumbnailer,
    )


def _first_run_setup(database: SqliteDatabase, config: Config) -> None:
    with database.transaction() as uow:
        if uow.meta.get(_FIRST_RUN_KEY) is not None:
            return
        for index, seed in enumerate(config.seed.reference_types):
            if uow.reference_types.get_by_name(seed.name) is None:
                uow.reference_types.add(
                    ReferenceType(name=seed.name, color=seed.color, reverse_name=seed.reverse, sort_order=index)
                )
        uow.meta.set(_FIRST_RUN_KEY, "1")


def build_application(
    *,
    default_toml_path: Path,
    data_dir_override: Path | None = None,
    install_dir_override: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Application:
    config = load_config(
        default_path=default_toml_path, data_dir_override=data_dir_override,
        install_dir_override=install_dir_override, env=env,
    )
    install_dir = install_dir_override or default_install_dir()
    ffmpeg_path, ffprobe_path = resolve_ffmpeg_tools(config, install_dir)
    data_dir = Path(config.app.data_dir)
    thumbnail_dir = Path(config.thumbnails.cache_dir)
    tag_images_dir = data_dir / TAG_IMAGES_DIRNAME
    for directory in (data_dir, thumbnail_dir, tag_images_dir, Path(config.logging.dir)):
        directory.mkdir(parents=True, exist_ok=True)

    database = SqliteDatabase(data_dir / DB_FILENAME)
    database.initialise()
    _first_run_setup(database, config)

    event_bus = InProcessEventBus()
    registries = Registries()
    registries.identity_strategies.register("blake3", Blake3IdentityStrategy())
    registries.media_types.register("video", _build_video_media_type(config, ffmpeg_path, ffprobe_path))

    plugin_context = PluginContext(
        registries=registries, event_bus=event_bus, config=config, data_dir=data_dir,
        plugin_data_dir=data_dir / "plugins", logger=logging.getLogger("clippycap.plugin"),
    )
    loaded_plugins = discover_and_load(
        directories=[Path(d) for d in config.plugins.dirs],
        enabled=config.plugins.enabled, disabled=config.plugins.disabled, base_context=plugin_context,
    )
    _log.info("started: %d media types, %d identity strategies, %d plugins",
              len(registries.media_types), len(registries.identity_strategies), len(loaded_plugins))

    scanner = LibraryScanner(
        database, list(registries.media_types), dict(registries.identity_strategies.items()), event_bus, config
    )
    jobs = ThreadJobQueue()

    if ffmpeg_path is not None:
        video_editor: VideoEditor = FfmpegVideoEditor(
            ffmpeg_path, reencode=config.editing.reencode,
            crf=config.editing.reencode_crf, preset=config.editing.reencode_preset,
        )
    else:
        video_editor = UnavailableVideoEditor()
    editing_service = EditingService(
        database, video_editor, dict(registries.media_types.items()),
        dict(registries.identity_strategies.items()), event_bus, config, thumbnail_dir,
    )
    config_service = ConfigService(
        default_toml_path=default_toml_path, data_dir=data_dir, install_dir=install_dir, env=env,
    )

    return Application(
        config=config, database=database, event_bus=event_bus, registries=registries, jobs=jobs,
        assets=AssetService(database, event_bus, thumbnail_dir), tags=TagService(database, event_bus, tag_images_dir),
        notes=NoteService(database, event_bus), references=ReferenceService(database, event_bus),
        reference_types=ReferenceTypeService(database), sources=SourceService(database, event_bus),
        saved_views=SavedViewService(database), scans=ScanService(database, scanner, jobs, event_bus),
        editing=editing_service, config_service=config_service, loaded_plugins=loaded_plugins,
        data_dir=data_dir, thumbnail_dir=thumbnail_dir,
        tag_images_dir=tag_images_dir, install_dir=install_dir, ffmpeg_available=ffmpeg_path is not None,
    )
