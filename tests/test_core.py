"""Smoke tests for the domain layer (mypy --strict does the real interface checking)."""

from __future__ import annotations

from clippycap.core import entities, events, ports, query


def test_entities_construct_with_minimal_args() -> None:
    asset = entities.Asset(identity_hash="b3:abc", media_type="video", title="clip", size_bytes=10)
    assert asset.id is None
    assert asset.metadata == {}
    assert asset.last_opened_at is None
    tag = entities.Tag(name="kill", color="#56c271")
    assert tag.icon is None
    assert tag.sort_order == 0
    note = entities.Note(asset_id=1, body="hi")
    assert note.timestamp_ms is None
    ref = entities.Reference(from_asset_id=1, to_asset_id=2)
    assert ref.type_id is None
    src = entities.Source(path="/x")
    assert src.recursive is True
    assert src.media_types == []
    view = entities.SavedView(name="untagged", filter_json="{}", sort_key="added_desc")
    assert view.sort_order == 0
    rtype = entities.ReferenceType(name="see also", color="#4fb6f0")
    assert rtype.reverse_name is None
    path = entities.AssetPath(asset_id=1, path="/x/a.mp4")
    assert path.present is True


def test_events_are_immutable_value_objects() -> None:
    a = events.TagApplied(asset_id=1, tag_id=2)
    b = events.TagApplied(asset_id=1, tag_id=2)
    assert a == b
    assert hash(a) == hash(b)
    assert isinstance(a, events.Event)


def test_asset_filter_defaults() -> None:
    f = query.AssetFilter()
    assert f.tags_all == []
    assert f.text is None
    assert f.untagged_only is False


def test_ports_module_exposes_the_contracts() -> None:
    for name in (
        "AssetRepository",
        "TagRepository",
        "NoteRepository",
        "ReferenceRepository",
        "ReferenceTypeRepository",
        "SourceRepository",
        "SavedViewRepository",
        "HashCacheStore",
        "MetaStore",
        "UnitOfWork",
        "Database",
        "IdentityStrategy",
        "MetadataExtractor",
        "Thumbnailer",
        "MediaTypeProvider",
        "JobQueue",
        "JobHandle",
        "ProgressReporter",
        "PortableExporter",
        "PortableImporter",
    ):
        assert hasattr(ports, name), name
