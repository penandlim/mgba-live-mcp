"""Success shapes exposed by tools; Pydantic generates their JSON Schemas."""

from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue

T = TypeVar("T")
# The Lua bridge serializes an empty table as [], even for an empty memory map.
EmptyArray = Annotated[list[JsonValue], Field(max_length=0)]


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str


class Started(Result):
    status: Literal["started"]
    pid: int
    fps_target: float
    session_dir: str


class Attached(Result):
    status: Literal["attached"]
    pid: int
    rom: str
    fps_target: float
    mgba_path: str | None


class Status(Result):
    pid: int
    alive: bool
    process_state: str
    identity_verified: bool
    transaction: dict[str, JsonValue] | None
    startup: dict[str, JsonValue] | None = None
    rom: str
    fps_target: float
    mgba_path: str | None
    heartbeat: JsonValue
    is_active: bool
    session_dir: str


class StatusList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: list[Status]


class Stopped(Result):
    pid: int
    alive_before: bool
    alive_after: Literal[False]
    stopped: bool
    outcome: Literal["stopped", "already_exited"]
    cleanup_errors: list[str] = Field(default_factory=list)


class Frame(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frame: int | None


class Framed(Result, Frame):
    pass


class Command(Framed, Generic[T]):
    data: T


class Tap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: int
    duration: int


class Keys(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: list[int]


class Cleared(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cleared: Literal["all"]


class View(Result):
    screenshot: Frame


class CommandView(Command[T], Generic[T]):
    screenshot: Frame


class StartupLua(Result):
    pid: int | None
    lua: JsonValue


class StartupView(StartupLua):
    screenshot: Frame


class Exported(Framed):
    path: str


class Memory(Framed):
    memory: dict[str, int] | EmptyArray


class RangeData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: int
    length: int
    data: list[int]


class MemoryRange(Framed):
    range: RangeData


class Pointer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    address: int
    value: int


class PointerData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: int
    count: int
    width: int
    pointers: list[Pointer]


class Pointers(Framed):
    pointers: PointerData


class Sprite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    address: int
    attr0: int
    attr1: int
    attr2: int


class OamData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base: int
    count: int
    sprites: list[Sprite]


class Oam(Framed):
    oam: OamData


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    address: int
    bytes: list[int]


class EntityData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base: int
    size: int
    count: int
    entities: list[Entity]


class Entities(Framed):
    entities: EntityData
