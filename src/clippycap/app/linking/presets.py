"""Built-in starter recipes for the linker gallery (LINKERS.md §9.6).

Each preset is a complete :class:`LinkerDefinition` with the user-specific bits (their folder) left
blank to fill in. They are *data*, not special-cased code -- the same engine runs a preset and a
hand-built linker -- and span domains on purpose (a game demo and a film screenplay) to prove the
system is general, not demo-specific.
"""

from __future__ import annotations

from dataclasses import dataclass

from clippycap.app.linking.types import (
    AssetScope,
    Condition,
    FieldDef,
    LinkerDefinition,
    MatchSpec,
    Ref,
    ResolveSpec,
    SideSpec,
    TargetScope,
)


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    description: str
    color: str
    definition: LinkerDefinition


def _demo_interval() -> LinkerDefinition:
    """N clips : 1 demo, matched by 'the demo was recording when the clip was saved' (interval)."""
    return LinkerDefinition(
        target=TargetScope(extensions=["dem"]),
        source=AssetScope(media_type="video"),
        clip=SideSpec(fields=[
            FieldDef(name="T", type="datetime", source={"kind": "metadata", "key": "recorded_at"}),  # type: ignore[arg-type]
        ]),
        file=SideSpec(fields=[
            FieldDef(name="start", type="datetime", source={"kind": "attr", "attr": "created"}),  # type: ignore[arg-type]
            FieldDef(name="end", type="datetime", source={"kind": "attr", "attr": "mtime"}),  # type: ignore[arg-type]
        ]),
        match=MatchSpec(conditions=[Condition(
            op="interval_contains", left=Ref(side="clip", field="T"),
            start=Ref(side="file", field="start"), end=Ref(side="file", field="end"), slack=10,
        )]),
        resolve=ResolveSpec(strategy="best_per_clip", tiebreak=["nearest_time"]),
    )


def _sidecar_by_name() -> LinkerDefinition:
    """1 clip : 1 document, matched by a shared scene/sequence number in both names (any layout,
    leading zeros normalised by reading the captures 'as Number')."""
    return LinkerDefinition(
        target=TargetScope(extensions=["docx", "pdf", "txt"]),
        # ``*%n%*`` captures the first run of digits anywhere in the name; reading it "as Number"
        # makes 012 == 12. The user tailors the template to their own names in the builder.
        clip=SideSpec(
            template="*%n%*",
            fields=[FieldDef(name="n", type="int", source={"kind": "capture", "name": "n"})],  # type: ignore[arg-type]
        ),
        file=SideSpec(
            template="*%n%*",
            fields=[FieldDef(name="n", type="int", source={"kind": "capture", "name": "n"})],  # type: ignore[arg-type]
        ),
        match=MatchSpec(conditions=[Condition(
            op="equals", left=Ref(side="clip", field="n"), right=Ref(side="file", field="n"),
        )]),
        resolve=ResolveSpec(strategy="best_overall", per_clip_max=1, per_file_max=1),
    )


def _same_time() -> LinkerDefinition:
    """Any file named within N seconds of the clip's recording time (a generic time pairing)."""
    return LinkerDefinition(
        clip=SideSpec(fields=[
            FieldDef(name="T", type="datetime", source={"kind": "metadata", "key": "recorded_at"}),  # type: ignore[arg-type]
        ]),
        file=SideSpec(fields=[
            FieldDef(name="T", type="datetime", source={"kind": "attr", "attr": "created"}),  # type: ignore[arg-type]
        ]),
        match=MatchSpec(conditions=[Condition(
            op="within", left=Ref(side="clip", field="T"), right=Ref(side="file", field="T"), tolerance=15,
        )]),
        resolve=ResolveSpec(strategy="best_per_clip", tiebreak=["nearest_time"]),
    )


PRESETS: tuple[Preset, ...] = (
    Preset(
        key="demo_interval", name="Game demo (by recording time)", color="#7c9cff",
        description="Attach the demo/recording that was running when each clip was captured. "
                    "Matches by time interval -- no matching file names needed. (e.g. TF2 demos.)",
        definition=_demo_interval(),
    ),
    Preset(
        key="sidecar_by_name", name="Sidecar document (by number in the name)", color="#22c55e",
        description="Attach a script / PDF / notes file that shares a scene or sequence number with "
                    "the clip's name -- any layout, leading zeros are ignored.",
        definition=_sidecar_by_name(),
    ),
    Preset(
        key="same_time", name="Any file recorded at the same time", color="#f59e0b",
        description="Attach a file whose creation time is within a few seconds of the clip's "
                    "recording time. A generic starting point for any companion file.",
        definition=_same_time(),
    ),
)


def preset_by_key(key: str) -> Preset | None:
    return next((p for p in PRESETS if p.key == key), None)
