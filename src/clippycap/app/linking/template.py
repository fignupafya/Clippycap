"""The ``%name%`` filename template -- compile a user template to a regex and parse a string with it.

A template mirrors a filename with ``%name%`` where a variable lives and literal text everywhere
else; ``*`` is an uncaptured wildcard. Literals are regex-escaped (so ``.`` means a literal dot). A
token captures lazily up to the next literal/anchor. A repeated name becomes a backreference (it must
match the same text in both places). This is the no-regex parse non-programmers use (LINKERS.md §9.3);
a power user can still drop to a raw regex via the ``regex_extract`` step instead.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

_TOKEN_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%|(\*)")

# A token's capture regex by its field type. Numeric tokens are type-constrained (so ``%n%`` stops at
# the first non-digit -- the design's answer to the "where does the number end?" ambiguity when a
# token is followed by a wildcard or another token); everything else captures lazily up to the next
# literal. The default (unknown / string / date) is lazy ``.+?``.
_TYPE_TOKEN = {
    "int": r"\d+",
    "float": r"\d+(?:[.,]\d+)?",
}
_DEFAULT_TOKEN = ".+?"


class TemplateError(ValueError):
    """A template that cannot be compiled (e.g. an empty token)."""


def compile_template(
    template: str, *, anchored: bool = True, case_insensitive: bool = True,
    token_types: Mapping[str, str] | None = None,
) -> re.Pattern[str]:
    """Compile a ``%name%`` template to a regex with one named group per (first) token occurrence.

    ``token_types`` maps a token name to its field type so numeric tokens become ``\\d+`` (bounded)
    instead of the lazy ``.+?`` default -- this is what makes ``SCN-%scene%*`` capture ``012`` rather
    than a single character."""
    types = token_types or {}
    parts: list[str] = []
    seen: set[str] = set()
    pos = 0
    for m in _TOKEN_RE.finditer(template):
        parts.append(re.escape(template[pos:m.start()]))           # the literal run before this token
        name, star = m.group(1), m.group(2)
        if star is not None:
            parts.append(".*?")
        else:
            assert name is not None
            if name in seen:
                parts.append(f"(?P={name})")                       # repeated name -> backreference
            else:
                seen.add(name)
                parts.append(f"(?P<{name}>{_TYPE_TOKEN.get(types.get(name, ''), _DEFAULT_TOKEN)})")
        pos = m.end()
    parts.append(re.escape(template[pos:]))                        # trailing literal
    body = "".join(parts)
    pattern = f"^(?:{body})$" if anchored else body
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        return re.compile(pattern, flags)
    except re.error as exc:                                         # pragma: no cover -- defensive
        raise TemplateError(str(exc)) from exc


def parse_with_template(
    text: str, template: str, *, anchored: bool = True, case_insensitive: bool = True,
    token_types: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Return the captured ``{name: value}`` for ``text``, or ``None`` if the template doesn't match."""
    rx = compile_template(
        template, anchored=anchored, case_insensitive=case_insensitive, token_types=token_types
    )
    m = rx.search(text)
    if m is None:
        return None
    return {k: (v if v is not None else "") for k, v in m.groupdict().items()}
