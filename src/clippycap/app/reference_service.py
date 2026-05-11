"""Reference and reference-type use cases."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from clippycap.core.entities import Reference, ReferenceType
from clippycap.core.errors import NotFoundError
from clippycap.core.events import EventBus, ReferenceCreated, ReferenceDeleted
from clippycap.core.ports import Database


@dataclass
class ReferenceView:
    reference: Reference
    type_name: str | None       # the *directional* name (reverse_name when seen from the "to" side)
    other_asset_id: int
    other_asset_title: str
    to_note_body: str | None    # body of the note at reference.to_timestamp_ms in reference.to_asset_id, if any


@dataclass
class ReferenceListing:
    outgoing: list[ReferenceView]
    incoming: list[ReferenceView]


class ReferenceTypeService:
    def __init__(self, database: Database) -> None:
        self._db = database

    def list_all(self) -> list[ReferenceType]:
        with self._db.transaction() as uow:
            return uow.reference_types.list_all()

    def create(
        self, *, name: str, color: str, reverse_name: str | None = None, sort_order: int = 0
    ) -> ReferenceType:
        with self._db.transaction() as uow:
            return uow.reference_types.add(
                ReferenceType(name=name, color=color, reverse_name=reverse_name, sort_order=sort_order)
            )

    def update(
        self, type_id: int, *, name: str, color: str, reverse_name: str | None, sort_order: int
    ) -> ReferenceType:
        with self._db.transaction() as uow:
            rtype = uow.reference_types.get(type_id)
            if rtype is None:
                raise NotFoundError(f"no reference type with id {type_id!r}")
            rtype.name, rtype.color, rtype.reverse_name, rtype.sort_order = name, color, reverse_name, sort_order
            uow.reference_types.update(rtype)
            return rtype

    def delete(self, type_id: int) -> None:
        with self._db.transaction() as uow:
            if uow.reference_types.get(type_id) is None:
                raise NotFoundError(f"no reference type with id {type_id!r}")
            uow.reference_types.delete(type_id)

    def reorder(self, ordered_ids: Sequence[int]) -> None:
        with self._db.transaction() as uow:
            uow.reference_types.reorder(ordered_ids)


class ReferenceService:
    def __init__(self, database: Database, event_bus: EventBus) -> None:
        self._db = database
        self._bus = event_bus

    def for_asset(self, asset_id: int) -> ReferenceListing:
        with self._db.transaction() as uow:
            if uow.assets.get(asset_id) is None:
                raise NotFoundError(f"no asset with id {asset_id!r}")
            types = {rt.id: rt for rt in uow.reference_types.list_all()}

            def view(ref: Reference, *, incoming: bool) -> ReferenceView:
                rtype = types.get(ref.type_id) if ref.type_id is not None else None
                if rtype is None:
                    name: str | None = None
                else:
                    name = (rtype.reverse_name or rtype.name) if incoming else rtype.name
                other_id = ref.from_asset_id if incoming else ref.to_asset_id
                other = uow.assets.get(other_id)
                to_note_body: str | None = None
                if ref.to_timestamp_ms is not None:
                    for n in uow.notes.list_for_asset(ref.to_asset_id):
                        if n.timestamp_ms == ref.to_timestamp_ms:
                            to_note_body = n.body
                            break
                return ReferenceView(
                    reference=ref, type_name=name, other_asset_id=other_id,
                    other_asset_title=other.title if other is not None else "(deleted)",
                    to_note_body=to_note_body,
                )

            outgoing = [view(r, incoming=False) for r in uow.references.list_outgoing(asset_id)]
            incoming = [view(r, incoming=True) for r in uow.references.list_incoming(asset_id)]
        return ReferenceListing(outgoing=outgoing, incoming=incoming)

    def create(
        self, *, from_asset_id: int, to_asset_id: int, type_id: int | None = None, label: str = "",
        from_timestamp_ms: int | None = None, to_timestamp_ms: int | None = None, note: str = "",
    ) -> Reference:
        with self._db.transaction() as uow:
            if uow.assets.get(from_asset_id) is None:
                raise NotFoundError(f"no asset with id {from_asset_id!r}")
            if uow.assets.get(to_asset_id) is None:
                raise NotFoundError(f"no asset with id {to_asset_id!r}")
            if type_id is not None and uow.reference_types.get(type_id) is None:
                raise NotFoundError(f"no reference type with id {type_id!r}")
            ref = uow.references.add(Reference(
                from_asset_id=from_asset_id, to_asset_id=to_asset_id, type_id=type_id, label=label,
                from_timestamp_ms=from_timestamp_ms, to_timestamp_ms=to_timestamp_ms, note=note,
            ))
        assert ref.id is not None
        self._bus.publish(
            ReferenceCreated(reference_id=ref.id, from_asset_id=from_asset_id, to_asset_id=to_asset_id)
        )
        return ref

    def delete(self, ref_id: int) -> None:
        with self._db.transaction() as uow:
            if uow.references.get(ref_id) is None:
                raise NotFoundError(f"no reference with id {ref_id!r}")
            uow.references.delete(ref_id)
        self._bus.publish(ReferenceDeleted(reference_id=ref_id))
