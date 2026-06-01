"""The linking subsystem: the rule language (``types``), the pure matching engine
(``extract`` / ``steps`` / ``predicates`` / ``resolve`` / ``engine``) and the built-in presets.

A *linker* attaches companion files (demos, scripts, transcripts, RAWs, ...) to assets by a
user-defined rule. The rule is an :class:`~clippycap.app.linking.types.LinkerDefinition` -- a
versioned, validated, JSON-serialisable value object. The engine reads each side into typed fields
(extract + transform), proposes candidate pairs with scores (join), then selects winners under the
cardinality rules (resolve). See ``LINKERS.md`` for the full design.
"""
