import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from prisma.errors import UniqueViolationError
from pydantic import TypeAdapter

from litellm.models.verification_token import LiteLLM_VerificationToken
from litellm.proxy.utils import PrismaClient
from litellm.repositories.table_repositories import DeletedVerificationTokenRepository
from litellm.repositories.team_repository import TeamRepository
from litellm.repositories.verification_token_repository import VerificationTokenRepository
from litellm.types.proxy.management_endpoints.key_migration import (
    ImportedKey,
    ImportRequest,
    InspectedKey,
    Inspection,
    InspectRequest,
    KeySettings,
    KeyState,
    MigrationFailure,
    MigrationKey,
    MigrationTeam,
    Preflight,
    PreflightRequest,
)

_IGNORED: Final = frozenset(
    {
        "token",
        "team_id",
        "blocked",
        "spend",
        "total_spend",
        "model_spend",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
        "settings_updated_at",
        "last_active",
        "rotation_count",
        "last_rotation_at",
        "soft_budget_cooldown",
    }
)
_EMPTY_OBJECTS: Final = frozenset(
    {"aliases", "config", "permissions", "metadata", "model_max_budget", "budget_fallbacks", "router_settings"}
)
_EMPTY_ARRAYS: Final = frozenset(
    {"allowed_cache_controls", "allowed_routes", "policies", "access_group_ids", "budget_limits"}
)
_MODELS: Final = TypeAdapter(tuple[str, ...])


@dataclass(frozen=True, slots=True)
class _RepositoryClient:
    db: object


def _utc(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed: Final = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _empty(name: str, value: object) -> bool:
    if value is None:
        return True
    if name in _EMPTY_OBJECTS:
        return isinstance(value, dict) and not value
    if name in _EMPTY_ARRAYS:
        return isinstance(value, list) and not value
    return name == "auto_rotate" and value is False


def _settings(row: LiteLLM_VerificationToken) -> KeySettings | MigrationFailure:
    if row.key_type not in (None, "default"):
        return MigrationFailure(409, "Unsupported LiteLLM field: key_type")
    data: Final = row.model_dump()
    unsupported: Final = next(
        (
            name
            for name, value in data.items()
            if name not in KeySettings.model_fields and name not in _IGNORED and not _empty(name, value)
        ),
        None,
    )
    if unsupported is not None:
        return MigrationFailure(409, f"Unsupported LiteLLM field: {unsupported}")
    return KeySettings.model_validate(
        {**row.model_dump(include=set(KeySettings.model_fields)), "expires": _utc(row.expires)}, strict=False
    )


class KeyMigrationService:
    def __init__(self, client: PrismaClient) -> None:
        self._client = client
        self._reader = _RepositoryClient(client.writer_db)

    async def _team(self, client: _RepositoryClient, team_id: str) -> MigrationTeam | MigrationFailure:
        team: Final = await TeamRepository(client).find_by_id(team_id)
        if team is None:
            return MigrationFailure(404, "Team not found")
        return MigrationTeam(team_id=team_id, blocked=bool(team.blocked), models=_MODELS.validate_python(team.models))

    async def _read(self, client: _RepositoryClient, key_hash: str) -> tuple[LiteLLM_VerificationToken | None, bool]:
        row: Final = await VerificationTokenRepository(client).find_by_id(key_hash)
        archived: Final = await DeletedVerificationTokenRepository(client).table.find_first(where={"token": key_hash})
        return row, archived is not None

    async def _inspect_key(self, key_hash: str, team_id: str) -> InspectedKey | MigrationFailure:
        row, archived = await self._read(self._reader, key_hash)
        if row is None:
            return InspectedKey(key_hash=key_hash, status="deleted" if archived else "missing")
        if row.team_id != team_id:
            return MigrationFailure(409, "Key belongs to another team")
        if row.blocked:
            return InspectedKey(key_hash=key_hash, status="blocked", settings=KeySettings())
        expires: Final = _utc(row.expires)
        if expires is not None and expires <= datetime.now(timezone.utc):
            return InspectedKey(key_hash=key_hash, status="expired", settings=KeySettings(expires=expires))
        settings: Final = _settings(row)
        return (
            settings
            if isinstance(settings, MigrationFailure)
            else InspectedKey(key_hash=key_hash, status="active", settings=settings)
        )

    async def inspect(self, request: InspectRequest) -> Inspection | MigrationFailure:
        team: Final = await self._team(self._reader, request.team_id)
        if isinstance(team, MigrationFailure):
            return team
        rows: Final = tuple(await asyncio.gather(*(self._inspect_key(h, request.team_id) for h in request.hashes)))
        failure: Final = next((row for row in rows if isinstance(row, MigrationFailure)), None)
        if failure is not None:
            return failure
        return Inspection(team=team, keys=tuple(row for row in rows if isinstance(row, InspectedKey)))

    async def _check(self, client: _RepositoryClient, key: MigrationKey, team_id: str) -> KeyState | MigrationFailure:
        if key.settings.expires is not None and (
            key.settings.expires.tzinfo is None or key.settings.expires <= datetime.now(timezone.utc)
        ):
            return MigrationFailure(409, "Key expiry is invalid or in the past")
        row, archived = await self._read(client, key.key_hash)
        if archived:
            return MigrationFailure(409, "Archived key")
        if row is not None:
            if row.team_id != team_id or row.blocked or _settings(row) != key.settings:
                return MigrationFailure(409, "Key settings or owner conflict")
            return KeyState(key_hash=key.key_hash, status="already_present")
        if key.settings.key_alias and await VerificationTokenRepository(client).find_by_alias(key.settings.key_alias):
            return MigrationFailure(409, "Key alias conflict")
        return KeyState(key_hash=key.key_hash, status="new")

    async def preflight(self, request: PreflightRequest) -> Preflight | MigrationFailure:
        team: Final = await self._team(self._reader, request.team_id)
        if isinstance(team, MigrationFailure):
            return team
        aliases: Final = tuple(k.settings.key_alias for k in request.keys if k.settings.key_alias)
        if len(set(aliases)) != len(aliases) or len({k.key_hash for k in request.keys}) != len(request.keys):
            return MigrationFailure(409, "Duplicate key hash or alias")
        states: Final = tuple(
            await asyncio.gather(*(self._check(self._reader, key, request.team_id) for key in request.keys))
        )
        failure: Final = next((state for state in states if isinstance(state, MigrationFailure)), None)
        if failure is not None:
            return failure
        return Preflight(keys=tuple(state for state in states if isinstance(state, KeyState)))

    async def import_key(self, request: ImportRequest) -> ImportedKey | MigrationFailure:
        try:
            async with self._client.tx() as transaction:
                await transaction.query_raw("SELECT 1 FROM pg_advisory_xact_lock(221, 1)")
                client: Final = _RepositoryClient(transaction)
                team: Final = await self._team(client, request.team_id)
                if isinstance(team, MigrationFailure):
                    return team
                state: Final = await self._check(client, request.key, request.team_id)
                if isinstance(state, MigrationFailure):
                    return state
                if state.status == "already_present":
                    return ImportedKey(key_hash=request.key.key_hash, status="already_present")
                settings: Final = request.key.settings
                created: Final = await VerificationTokenRepository(client).create_token(
                    token=request.key.key_hash,
                    team_id=request.team_id,
                    key_name=settings.key_name,
                    key_alias=settings.key_alias,
                    key_type=settings.key_type,
                    expires=_utc(settings.expires),
                    models=list(settings.models),
                    rpm_limit=settings.rpm_limit,
                    tpm_limit=settings.tpm_limit,
                    max_parallel_requests=settings.max_parallel_requests,
                )
                if _settings(created) != settings:
                    raise RuntimeError("Imported key does not match validated settings")
                return ImportedKey(key_hash=request.key.key_hash, status="created")
        except UniqueViolationError:
            matched: Final = await self._check(self._reader, request.key, request.team_id)
            if isinstance(matched, MigrationFailure):
                return matched
            return (
                ImportedKey(key_hash=request.key.key_hash, status="already_present")
                if matched.status == "already_present"
                else MigrationFailure(409, "Concurrent key import conflict")
            )
