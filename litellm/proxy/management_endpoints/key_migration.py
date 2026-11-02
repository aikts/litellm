"""Temporary AP-221 migration API. Never accepts plaintext keys or arbitrary SQL."""

import hmac
import json
import os
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, NoReturn, Protocol

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

MAX_BODY = 1024 * 1024
router = APIRouter(prefix="/internal/key-migration", tags=["internal key migration"])


class RawQueries(Protocol):
    async def query_raw(self, query: str, *args: object) -> Sequence[Mapping[str, object]]: ...
    async def execute_raw(self, query: str, *args: object) -> int: ...


class MigrationDatabase(RawQueries, Protocol):
    def tx(self) -> AbstractAsyncContextManager[RawQueries]: ...


class PrismaMigrationDatabase:
    """Validate the dynamic Prisma wrapper at the SQL serialization boundary."""

    def __init__(self, db: object) -> None:
        self._db = db

    async def query_raw(self, query: str, *args: object) -> Sequence[Mapping[str, object]]:
        method = getattr(self._db, "query_raw", None)
        if not callable(method):
            raise TypeError("Migration query interface unavailable")
        result = method(query, *args)
        if not isinstance(result, Awaitable):
            raise TypeError("Migration query must be async")
        return TypeAdapter(list[dict[str, object]]).validate_python(await result)

    async def execute_raw(self, query: str, *args: object) -> int:
        method = getattr(self._db, "execute_raw", None)
        if not callable(method):
            raise TypeError("Migration execute interface unavailable")
        result = method(query, *args)
        if not isinstance(result, Awaitable):
            raise TypeError("Migration execute must be async")
        return TypeAdapter(int).validate_python(await result)

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[RawQueries]:
        method = getattr(self._db, "tx", None)
        if not callable(method):
            raise TypeError("Migration transaction interface unavailable")
        context = method()
        if not isinstance(context, AbstractAsyncContextManager):
            raise TypeError("Migration transaction must be async")
        async with context as transaction:
            yield PrismaMigrationDatabase(transaction)


class Column(BaseModel):
    column_name: str
    is_nullable: Literal["YES", "NO"]
    column_default: object = None


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key_name: str | None = None
    key_alias: str | None = None
    key_type: Literal["default"] | None = None
    expires: datetime | None = None
    models: list[str] = Field(default_factory=list)
    rpm_limit: int | None = Field(default=None, ge=0)
    tpm_limit: int | None = Field(default=None, ge=0)
    max_parallel_requests: int | None = Field(default=None, ge=0)


class Key(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key_hash: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    settings: Settings


class Inspect(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    contract_version: Literal[1]
    team_id: str = Field(min_length=1)
    hashes: list[str] = Field(max_length=100)


class Preflight(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    contract_version: Literal[1]
    team_id: str = Field(min_length=1)
    keys: list[Key] = Field(max_length=100)


class Import(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    contract_version: Literal[1]
    team_id: str = Field(min_length=1)
    key: Key


# Exact defaults only: zero is a constraint, not an empty value.
DEFAULTS = {
    "spend": 0.0,
    "total_spend": 0.0,
    "blocked": False,
    "soft_budget_cooldown": False,
    "aliases": {},
    "config": {},
    "permissions": {},
    "metadata": {},
    "model_spend": {},
    "model_max_budget": {},
    "budget_fallbacks": {},
    "allowed_cache_controls": [],
    "allowed_routes": [],
    "policies": [],
    "access_group_ids": [],
    "auto_rotate": False,
    "rotation_count": 0,
}
EMPTY = {**DEFAULTS, "router_settings": {}, "budget_limits": {}}
IGNORED = {
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


def conflict(message: str) -> NoReturn:
    raise HTTPException(409, message)


def instant(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, (datetime, str)):
        conflict("Invalid LiteLLM timestamp")
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def settings(row: dict[str, object]) -> Settings:
    for name, value in row.items():
        if name in Settings.model_fields or name in IGNORED or value is None:
            continue
        if name in EMPTY and type(value) is type(EMPTY[name]) and value == EMPTY[name]:
            continue
        # Only a column name from the server schema, never request/row values.
        conflict(f"Unsupported LiteLLM field: {name}")
    values = {name: row.get(name) for name in Settings.model_fields}
    values["expires"] = instant(values["expires"])
    return Settings.model_validate(values)


class MigrationStore:
    """Uses LiteLLM's existing Prisma connection; credentials never leave the owner."""

    def __init__(self, db: MigrationDatabase) -> None:
        self.db = db

    async def team(self, db: RawQueries, team_id: str) -> dict[str, object]:
        rows = await db.query_raw(
            'SELECT team_id, blocked, models FROM "LiteLLM_TeamTable" WHERE team_id = $1', team_id
        )
        if not rows:
            raise HTTPException(404, "Team not found")
        row = rows[0]
        return {"team_id": row["team_id"], "blocked": bool(row["blocked"]), "models": row["models"] or []}

    async def read(self, db: RawQueries, key_hash: str) -> tuple[dict[str, object] | None, bool]:
        rows = await db.query_raw(
            'SELECT row_to_json(k) AS data FROM "LiteLLM_VerificationToken" k WHERE token = $1', key_hash
        )
        archived = await db.query_raw(
            'SELECT 1 FROM "LiteLLM_DeletedVerificationToken" WHERE token = $1 LIMIT 1', key_hash
        )
        return (TypeAdapter(dict[str, object]).validate_python(rows[0]["data"]) if rows else None), bool(archived)

    async def schema(self, db: RawQueries) -> set[str]:
        columns = await db.query_raw(
            "SELECT column_name, is_nullable, column_default FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'LiteLLM_VerificationToken'"
        )
        fields = TypeAdapter(list[Column]).validate_python(columns)
        names = {row.column_name for row in fields}
        supplied = set(Settings.model_fields) | set(DEFAULTS) | {"token", "team_id", "created_at", "updated_at"}
        if not (set(Settings.model_fields) | {"token", "team_id"}).issubset(names):
            conflict("Unsupported LiteLLM schema")
        for row in fields:
            if row.is_nullable == "NO" and row.column_default is None and row.column_name not in supplied:
                conflict("Unsupported required LiteLLM field: " + row.column_name)
        return names

    async def inspect(self, data: Inspect) -> dict[str, object]:
        await self.schema(self.db)
        team = await self.team(self.db, data.team_id)
        result = []
        for key_hash in data.hashes:
            if len(key_hash) != 64 or any(c not in "0123456789abcdef" for c in key_hash):
                raise HTTPException(422, "Invalid key hash")
            row, archived = await self.read(self.db, key_hash)
            state, projected = "missing", None
            if row is not None:
                if row["team_id"] != data.team_id:
                    conflict("Key belongs to another team")
                expires = instant(row["expires"])
                if row["blocked"]:
                    state, projected = "blocked", Settings()
                elif expires is not None and expires <= datetime.now(timezone.utc):
                    state, projected = "expired", Settings(expires=expires)
                else:
                    state, projected = "active", settings(row)
            elif archived:
                state = "deleted"
            result.append(
                {
                    "key_hash": key_hash,
                    "status": state,
                    "settings": projected.model_dump(mode="json") if projected else None,
                }
            )
        return {"contract_version": 1, "team": team, "keys": result}

    async def check(self, db: RawQueries, key: Key, team_id: str) -> bool:
        if key.settings.expires is not None and (
            key.settings.expires.tzinfo is None or key.settings.expires <= datetime.now(timezone.utc)
        ):
            conflict("Key expiry is invalid or in the past")
        row, archived = await self.read(db, key.key_hash)
        if archived:
            conflict("Archived key")
        if row is not None:
            if row["team_id"] != team_id or row["blocked"] or settings(row) != key.settings:
                conflict("Key settings or owner conflict")
            return True
        if key.settings.key_alias:
            aliases = await db.query_raw(
                'SELECT 1 FROM "LiteLLM_VerificationToken" WHERE key_alias = $1 LIMIT 1', key.settings.key_alias
            )
            if aliases:
                conflict("Key alias conflict")
        return False

    async def preflight(self, data: Preflight) -> dict[str, object]:
        await self.schema(self.db)
        await self.team(self.db, data.team_id)
        aliases = [key.settings.key_alias for key in data.keys if key.settings.key_alias]
        if len(set(aliases)) != len(aliases) or len({k.key_hash for k in data.keys}) != len(data.keys):
            conflict("Duplicate key hash or alias")
        keys = [
            {
                "key_hash": key.key_hash,
                "status": "already_present" if await self.check(self.db, key, data.team_id) else "new",
            }
            for key in data.keys
        ]
        return {"contract_version": 1, "keys": keys}

    async def import_key(self, data: Import) -> dict[str, object]:
        async with self.db.tx() as tx:
            # Serialize migration alias claims and check again under the same transaction.
            await tx.query_raw("SELECT 1 FROM pg_advisory_xact_lock(221, 1)")
            await tx.execute_raw("SET LOCAL TIME ZONE 'UTC'")
            names = await self.schema(tx)
            await self.team(tx, data.team_id)
            present = await self.check(tx, data.key, data.team_id)
            if not present:
                values: dict[str, object] = {name: value for name, value in DEFAULTS.items() if name in names}
                values.update(data.key.settings.model_dump(mode="json"))
                values.update(token=data.key.key_hash, team_id=data.team_id)
                for name in ("created_at", "updated_at"):
                    if name in names:
                        values[name] = datetime.now(timezone.utc).isoformat()
                # Column identifiers come only from the fixed server-side allowlist.
                columns = ", ".join('"' + name + '"' for name in values)
                inserted = await tx.query_raw(
                    f'INSERT INTO "LiteLLM_VerificationToken" ({columns}) SELECT {columns} FROM jsonb_populate_record(NULL::"LiteLLM_VerificationToken", $1::jsonb) ON CONFLICT (token) DO NOTHING RETURNING token',
                    json.dumps(values),
                )
                present = not bool(inserted)
                if not await self.check(tx, data.key, data.team_id):
                    conflict("Import verification failed")
        return {
            "contract_version": 1,
            "key_hash": data.key.key_hash,
            "status": "already_present" if present else "created",
        }


async def authorize_and_decode(request: Request, model: type[BaseModel]) -> BaseModel:
    expected = os.environ.get("LITELLM_KEY_MIGRATION_TOKEN", "")
    headers = request.headers.getlist("authorization")
    supplied = headers[0][7:] if len(headers) == 1 and headers[0].startswith("Bearer ") else ""
    if not expected or not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "Migration request too large")
    try:
        return model.model_validate_json(bytes(raw))
    except ValidationError:
        # Pydantic's ordinary error includes the input, which carries key hashes.
        raise HTTPException(422, "Invalid migration request") from None


def store() -> MigrationStore:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(503, "Migration database unavailable")
    from litellm.proxy.db.routing_prisma_wrapper import writer_wrapper

    return MigrationStore(PrismaMigrationDatabase(writer_wrapper(prisma_client.db)))


async def dispatch(request: Request, model: type[BaseModel], operation: str) -> JSONResponse:
    data = await authorize_and_decode(request, model)
    try:
        result = await getattr(store(), operation)(data)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 -- never expose driver SQL, credentials or key values
        # No driver exceptions: SQL, keys and connection details must not reach logs or callers.
        raise HTTPException(503, "Migration database operation failed") from None
    response = JSONResponse(result)
    if len(response.body) > MAX_BODY:
        raise HTTPException(413, "Migration response too large")
    return response


@router.post("/inspect")
async def inspect_keys(request: Request) -> JSONResponse:
    return await dispatch(request, Inspect, "inspect")


@router.post("/preflight")
async def preflight_keys(request: Request) -> JSONResponse:
    return await dispatch(request, Preflight, "preflight")


@router.post("/import")
async def import_key(request: Request) -> JSONResponse:
    response = await dispatch(request, Import, "import_key")
    # Clear this worker and shared cache. Other workers may retain bounded local TTLs.
    from litellm.proxy.proxy_server import user_api_key_cache

    body = json.loads(bytes(response.body))
    try:
        await user_api_key_cache.async_delete_cache(body["key_hash"])
    except Exception:  # noqa: BLE001 -- sanitized cache boundary after an idempotent commit
        raise HTTPException(503, "Migration cache invalidation failed; retry import") from None
    return response
