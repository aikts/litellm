from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

KeyHash: TypeAlias = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]


class MigrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class KeySettings(MigrationModel):
    key_name: str | None = None
    key_alias: str | None = None
    key_type: Literal["default"] | None = None
    expires: datetime | None = None
    models: tuple[str, ...] = ()
    rpm_limit: int | None = Field(default=None, ge=0)
    tpm_limit: int | None = Field(default=None, ge=0)
    max_parallel_requests: int | None = Field(default=None, ge=0)


class MigrationKey(MigrationModel):
    key_hash: KeyHash
    settings: KeySettings


class MigrationRequest(MigrationModel):
    contract_version: Literal[1]
    team_id: str = Field(min_length=1)


class InspectRequest(MigrationRequest):
    hashes: tuple[KeyHash, ...] = Field(max_length=100)


class PreflightRequest(MigrationRequest):
    keys: tuple[MigrationKey, ...] = Field(max_length=100)


class ImportRequest(MigrationRequest):
    key: MigrationKey


class MigrationTeam(MigrationModel):
    team_id: str
    blocked: bool
    models: tuple[str, ...]


class InspectedKey(MigrationModel):
    key_hash: str
    status: Literal["missing", "active", "blocked", "expired", "deleted"]
    settings: KeySettings | None = None


class Inspection(MigrationModel):
    contract_version: Literal[1] = 1
    team: MigrationTeam
    keys: tuple[InspectedKey, ...]


class KeyState(MigrationModel):
    key_hash: str
    status: Literal["new", "already_present"]


class Preflight(MigrationModel):
    contract_version: Literal[1] = 1
    keys: tuple[KeyState, ...]


class ImportedKey(MigrationModel):
    contract_version: Literal[1] = 1
    key_hash: str
    status: Literal["created", "already_present"]


@dataclass(frozen=True, slots=True)
class MigrationFailure:
    status: Literal[404, 409]
    detail: str
