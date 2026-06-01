"""Linker use cases: CRUD over linkers, the non-destructive preview, running, the resolved
attachments shown on a clip, manual pin/exclude overrides, and the reveal / open-with actions.

Running and previewing delegate to :class:`~clippycap.app.linker_runner.LinkerRunner`. Reveal/open
spawn a program with the file path passed as a single argv element (never a shell string), and the
program always comes from trusted linker config -- never derived from the file's name or contents.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from clippycap.app.jobs import ThreadJobQueue
from clippycap.app.linker_runner import LinkerRunner
from clippycap.app.linking.engine import EngineResult
from clippycap.app.linking.presets import PRESETS, Preset
from clippycap.app.linking.types import dump_definition, load_definition
from clippycap.core.entities import Attachment, AttachmentOverride, Linker
from clippycap.core.errors import InvalidInputError, NotFoundError, UnsupportedError
from clippycap.core.ports import Database

_log = logging.getLogger(__name__)


@dataclass
class LinkerService:
    database: Database
    jobs: ThreadJobQueue
    runner: LinkerRunner

    # ---- presets --------------------------------------------------------
    def presets(self) -> Sequence[Preset]:
        return PRESETS

    # ---- CRUD -----------------------------------------------------------
    def list_all(self) -> list[Linker]:
        with self.database.transaction() as uow:
            return uow.linkers.list_all()

    def get(self, linker_id: int) -> Linker:
        with self.database.transaction() as uow:
            return self._require(uow.linkers.get(linker_id), linker_id)

    def create(
        self, *, name: str, definition_json: str, description: str = "", color: str = "",
        enabled: bool = False,
    ) -> Linker:
        defn = load_definition(definition_json)               # validate before storing
        with self.database.transaction() as uow:
            linker = uow.linkers.add(Linker(
                name=name, definition_json=dump_definition(defn), description=description,
                color=color, enabled=enabled, schema_version=defn.schema_version,
            ))
        if enabled and linker.id is not None:
            self.run(linker.id)
        return linker

    def update(
        self, linker_id: int, *, name: str, definition_json: str, description: str, color: str, enabled: bool,
    ) -> Linker:
        defn = load_definition(definition_json)
        with self.database.transaction() as uow:
            linker = self._require(uow.linkers.get(linker_id), linker_id)
            linker.name, linker.description, linker.color = name, description, color
            linker.enabled, linker.definition_json = enabled, dump_definition(defn)
            linker.schema_version = defn.schema_version
            uow.linkers.update(linker)
        if enabled:
            self.run(linker_id)
        return linker

    def delete(self, linker_id: int) -> None:
        with self.database.transaction() as uow:
            self._require(uow.linkers.get(linker_id), linker_id)
            uow.linkers.delete(linker_id)

    def set_enabled(self, linker_id: int, enabled: bool) -> Linker:
        with self.database.transaction() as uow:
            linker = self._require(uow.linkers.get(linker_id), linker_id)
            linker.enabled = enabled
            uow.linkers.update(linker)
        if enabled:
            self.run(linker_id)
        return linker

    def reorder(self, ordered_ids: Sequence[int]) -> None:
        with self.database.transaction() as uow:
            uow.linkers.reorder(ordered_ids)

    def clone(self, linker_id: int) -> Linker:
        with self.database.transaction() as uow:
            src = self._require(uow.linkers.get(linker_id), linker_id)
            base = f"{src.name} (copy)"
            name = base
            n = 2
            while uow.linkers.get_by_name(name) is not None:
                name, n = f"{base} {n}", n + 1
            return uow.linkers.add(Linker(
                name=name, definition_json=src.definition_json, description=src.description,
                color=src.color, enabled=False, schema_version=src.schema_version,
            ))

    # ---- run / preview --------------------------------------------------
    def run(self, linker_id: int) -> str:
        """Submit a background run; returns the job id."""
        def _job(_reporter: object) -> None:
            self.runner.run_linker(linker_id)
        return self.jobs.submit(f"link:{linker_id}", _job)

    def run_all_enabled(self) -> str:
        def _job(_reporter: object) -> None:
            self.runner.run_all_enabled()
        return self.jobs.submit("link:all", _job)

    def preview(self, definition_json: str) -> EngineResult:
        return self.runner.preview(definition_json)

    # ---- attachments + overrides ---------------------------------------
    def attachments_for_asset(self, asset_id: int) -> list[Attachment]:
        with self.database.transaction() as uow:
            if uow.assets.get(asset_id) is None:
                raise NotFoundError(f"no asset with id {asset_id!r}")
            return _dedupe_by_path(uow.attachments.list_for_asset(asset_id))

    def set_override(self, *, asset_id: int, linker_id: int, path: str, decision: str) -> None:
        if decision not in {"pin", "exclude"}:
            raise InvalidInputError(f"decision must be pin|exclude, got {decision!r}")
        with self.database.transaction() as uow:
            uow.attachment_overrides.set(AttachmentOverride(
                asset_id=asset_id, linker_id=linker_id, path=path, decision=decision,
            ))
        # Re-run this one linker synchronously so the caller sees the corrected links immediately.
        self.runner.run_linker(linker_id)

    def clear_override(self, *, asset_id: int, linker_id: int, path: str) -> None:
        with self.database.transaction() as uow:
            uow.attachment_overrides.clear(asset_id, linker_id, path)
        self.runner.run_linker(linker_id)

    # ---- reveal / open --------------------------------------------------
    def reveal(self, attachment_id: int) -> None:
        path = self._attachment_path(attachment_id)
        _reveal_in_file_manager(path)

    def open_default(self, attachment_id: int) -> None:
        path = self._attachment_path(attachment_id)
        _open_default(path)

    def open_with(self, attachment_id: int, action_name: str) -> None:
        with self.database.transaction() as uow:
            att = self._require_att(uow.attachments.get(attachment_id), attachment_id)
            linker = self._require(uow.linkers.get(att.linker_id), att.linker_id)
        defn = load_definition(linker.definition_json)
        action = next((a for a in defn.actions.open_with if a.name == action_name), None)
        if action is None:
            raise NotFoundError(f"no open-with action {action_name!r} on linker {att.linker_id}")
        argv = [action.program] + [att.path if a == "%PATH%" else a for a in action.args]
        _spawn(argv)

    # ---- helpers --------------------------------------------------------
    def _attachment_path(self, attachment_id: int) -> str:
        with self.database.transaction() as uow:
            return self._require_att(uow.attachments.get(attachment_id), attachment_id).path

    @staticmethod
    def _require(linker: Linker | None, linker_id: int) -> Linker:
        if linker is None:
            raise NotFoundError(f"no linker with id {linker_id!r}")
        return linker

    @staticmethod
    def _require_att(att: Attachment | None, attachment_id: int) -> Attachment:
        if att is None:
            raise NotFoundError(f"no attachment with id {attachment_id!r}")
        return att


def _dedupe_by_path(attachments: list[Attachment]) -> list[Attachment]:
    """For display: one row per file path (a file linked by two linkers shows once -- the manual /
    highest-scored wins, matching the storage rule that dedupes by (asset, path))."""
    best: dict[str, Attachment] = {}
    for att in attachments:
        cur = best.get(att.path)
        if cur is None or (att.origin == "manual" and cur.origin != "manual") or att.score > cur.score:
            best[att.path] = att
    return sorted(best.values(), key=lambda a: (a.origin != "manual", -a.score, a.label.casefold()))


# --------------------------------------------------------------------------- OS actions


def _reveal_in_file_manager(path: str) -> None:
    p = Path(path)
    if sys.platform == "win32":
        # /select, highlights the file in Explorer; falls back to opening the folder if it's gone.
        target = str(p) if p.exists() else str(p.parent)
        _spawn(["explorer", f"/select,{target}"], check=False)
    elif sys.platform == "darwin":
        _spawn(["open", "-R", str(p)])
    else:
        _spawn(["xdg-open", str(p.parent if not p.is_dir() else p)])


def _open_default(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        _spawn(["open", path])
    else:
        _spawn(["xdg-open", path])


def _spawn(argv: list[str], *, check: bool = False) -> None:
    try:
        subprocess.Popen(argv, shell=False)
    except OSError as exc:
        if check:
            raise UnsupportedError(f"could not run {argv[0]!r}: {exc}") from exc
        _log.warning("failed to spawn %r: %s", argv, exc)
