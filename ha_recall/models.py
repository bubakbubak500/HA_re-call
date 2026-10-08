"""Validated, independently versioned memory records."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Text = Annotated[str, Field(min_length=1, max_length=8000)]
Namespace = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


def now() -> str:
    return datetime.now(UTC).isoformat()


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: Annotated[str, Field(min_length=1, max_length=256)]
    entity_type: Literal["device", "area", "person", "automation", "concept", "ha_entity"] = (
        "concept"
    )
    external_id: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    aliases: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        default_factory=list, max_length=100
    )
    categories: list[str] = Field(default_factory=list, max_length=100)
    description: str = Field(default="", max_length=200000)
    document: Document | None = None


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: Text
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @field_validator("valid_from", "valid_until")
    @classmethod
    def timezone_required(cls, value):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("Validity timestamps must include a timezone")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def valid_interval(self):
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        return self


class Fact(Evidence):
    entity_id: Text
    predicate: Annotated[str, Field(min_length=1, max_length=256)]
    value: Text
    # A keyed property has one accepted value. Multi-valued observations opt out.
    exclusive: bool = True


class Relation(Evidence):
    subject_id: Text
    predicate: Annotated[str, Field(min_length=1, max_length=256)]
    object_id: Text
    attributes: dict[str, str] = Field(default_factory=dict, max_length=100)


def is_current(data: dict, at: str | None = None) -> bool:
    instant = datetime.fromisoformat(at) if at else datetime.now(UTC)
    return (
        not data.get("valid_from") or datetime.fromisoformat(data["valid_from"]) <= instant
    ) and (not data.get("valid_until") or instant < datetime.fromisoformat(data["valid_until"]))


def overlaps(left: dict, right: dict) -> bool:
    minimum = datetime.min.replace(tzinfo=UTC)
    maximum = datetime.max.replace(tzinfo=UTC)

    def start(d):
        return datetime.fromisoformat(d["valid_from"]) if d.get("valid_from") else minimum

    def end(d):
        return datetime.fromisoformat(d["valid_until"]) if d.get("valid_until") else maximum

    return max(start(left), start(right)) < min(end(left), end(right))
