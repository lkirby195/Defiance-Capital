"""What a rejected form says, and which box it says it next to.  # SPEC §8.1, §12

A form that comes back with a list of complaints at the top of the page makes a person read
the list, find the box each line is about, and hope they matched them up the way the server
did. ``FormProblems`` is the other arrangement: each message is filed under the box it is
about, the template renders it under that box (``api/templates/_fields.html``), and only the
ones that are about *more than one* box - the payoff date against the term against the closing
date - go to the top, where they belong, because there is no single box to put them under.

Three sources feed it and all three end up in the same shape:

* Pydantic. An issue's ``loc`` names the field, so ``purchase_price`` lands on the Purchase
  Price box. A model-level validator has no ``loc`` at all - that is what cross-field means -
  and lands at the top. An issue inside the repeated court-matter rows names its row, which
  is as close to the cell as a plain HTML table gets.
* The form's own rules (``api/intake_form.py``): a required box left empty, a split product
  missing half its split. Both name a box, so both file under it.
* A ``ValueError`` that is nobody's field in particular - the engine refusing to price a deal
  it cannot - which is a message about the whole submission and goes to the top.

Nothing here decides whether a submission is any good. It only decides where the answer goes.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from pydantic import ValidationError

# Pydantic prefixes a validator's own ValueError with this; a person reading a form does not
# need to be told the message is a value error.
_VALUE_ERROR_PREFIX = "Value error, "
# The repeated-row field on both forms, and what the page calls its table.
MATTERS = "court_records_team"
MATTERS_LABEL = "Matters found"


def clean(message: str) -> str:
    """One Pydantic message as a line a person reads."""
    text = message.removeprefix(_VALUE_ERROR_PREFIX).strip()
    return text[0].upper() + text[1:] if text else "This is not a valid entry."


@dataclass(frozen=True)
class FormProblems:
    """Every complaint about one submission, filed under the box it is about."""

    fields: dict[str, list[str]] = field(default_factory=dict)
    general: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.fields or self.general)

    def __len__(self) -> int:
        return len(self.general) + sum(len(messages) for messages in self.fields.values())

    def __iter__(self) -> Iterator[str]:
        """Every message, the cross-field ones first; for a caller with no form to render."""
        yield from self.general
        for name in sorted(self.fields):
            yield from (f"{name}: {message}" for message in self.fields[name])

    @property
    def lines(self) -> list[str]:
        return list(self)

    def at(self, name: str, *messages: str) -> FormProblems:
        """A copy with ``messages`` added under ``name``."""
        merged = {key: list(value) for key, value in self.fields.items()}
        merged.setdefault(name, []).extend(messages)
        return FormProblems(merged, list(self.general))

    def anywhere(self, *messages: str) -> FormProblems:
        """A copy with ``messages`` added at the top, where the cross-field ones go."""
        return FormProblems(
            {key: list(value) for key, value in self.fields.items()},
            [*self.general, *messages],
        )

    def merge(self, other: FormProblems) -> FormProblems:
        merged = {key: list(value) for key, value in self.fields.items()}
        for name, messages in other.fields.items():
            merged.setdefault(name, []).extend(messages)
        return FormProblems(merged, [*self.general, *other.general])


NO_PROBLEMS = FormProblems()


def at_top(*messages: str) -> FormProblems:
    """Problems that are about the submission rather than about one box."""
    return FormProblems({}, list(messages))


def by_field(messages: Mapping[str, Iterable[str]]) -> FormProblems:
    """Problems already filed by box name."""
    return FormProblems({name: list(lines) for name, lines in messages.items()}, [])


def _where(loc: tuple[object, ...], known: frozenset[str]) -> str | None:
    """The box an issue belongs under, or None when it belongs at the top.

    The first element of ``loc`` is the field; anything the form does not render a box for -
    a nested model, a repeated row, a name that has drifted out of the template - is not a
    box a message can sit under, so it goes to the top with its path spelled out instead.
    """
    if not loc:
        return None
    head = str(loc[0])
    return head if head in known and len(loc) == 1 else None


def _path(loc: tuple[object, ...]) -> str:
    """A nested issue's location as a person reads it: ``Matters found, row 2 (amount_usd)``."""
    if len(loc) >= 2 and str(loc[0]) == MATTERS and isinstance(loc[1], int):
        column = ".".join(str(part) for part in loc[2:])
        where = f"{MATTERS_LABEL}, row {loc[1] + 1}"
        return f"{where} ({column})" if column else where
    return ".".join(str(part) for part in loc) or "the form"


def from_validation_error(error: ValidationError, known: frozenset[str]) -> FormProblems:
    """A Pydantic complaint as problems, each under the box it names.  # SPEC §12

    ``known`` is the set of names the form actually renders a box for. A field outside it -
    or a model-level rule, which names no field at all - is a message with nowhere to sit, and
    it goes to the top carrying its own path so a person can still tell what it is about.
    """
    problems = FormProblems()
    for issue in error.errors():
        loc = tuple(issue["loc"])
        message = clean(str(issue["msg"]))
        box = _where(loc, known)
        if box is not None:
            problems = problems.at(box, message)
        elif loc:
            problems = problems.anywhere(f"{_path(loc)}: {message}")
        else:
            problems = problems.anywhere(message)
    return problems or at_top("The form could not be read.")
