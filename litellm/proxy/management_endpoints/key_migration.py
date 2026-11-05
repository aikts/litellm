import hmac
import os
from typing import Annotated, Final, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from litellm.proxy.management_helpers.key_migration import KeyMigrationService
from litellm.types.proxy.management_endpoints.key_migration import (
    ImportedKey,
    ImportRequest,
    InspectRequest,
    MigrationFailure,
    PreflightRequest,
)

RequestModel = TypeVar("RequestModel", bound=BaseModel)

MAX_BODY: Final = 1024 * 1024


def authorize(request: Request) -> None:
    expected: Final = os.environ.get("LITELLM_KEY_MIGRATION_TOKEN", "")
    headers: Final = request.headers.getlist("authorization")
    supplied: Final = headers[0][7:] if len(headers) == 1 and headers[0].startswith("Bearer ") else ""
    if not expected or not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})


router: Final = APIRouter(
    prefix="/internal/key-migration", tags=["internal key migration"], dependencies=[Depends(authorize)]
)


def migration_service() -> KeyMigrationService:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(503, "Migration database unavailable")
    return KeyMigrationService(prisma_client)


async def decode(request: Request, model: type[RequestModel]) -> RequestModel:
    raw: Final = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "Migration request too large")
    try:
        return model.model_validate_json(bytes(raw))
    except ValidationError:
        raise HTTPException(422, "Invalid migration request") from None


def respond(result: BaseModel | MigrationFailure) -> JSONResponse:
    if isinstance(result, MigrationFailure):
        raise HTTPException(result.status, result.detail)
    response: Final = JSONResponse(result.model_dump(mode="json"))
    if len(response.body) > MAX_BODY:
        raise HTTPException(413, "Migration response too large")
    return response


@router.post("/inspect")
async def inspect_keys(
    request: Request, service: Annotated[KeyMigrationService, Depends(migration_service)]
) -> JSONResponse:
    data: Final = await decode(request, InspectRequest)
    try:
        result: Final = await service.inspect(data)
    except Exception:  # noqa: BLE001  # driver exceptions may contain credentials or key values
        raise HTTPException(503, "Migration database operation failed") from None
    return respond(result)


@router.post("/preflight")
async def preflight_keys(
    request: Request, service: Annotated[KeyMigrationService, Depends(migration_service)]
) -> JSONResponse:
    data: Final = await decode(request, PreflightRequest)
    try:
        result: Final = await service.preflight(data)
    except Exception:  # noqa: BLE001  # driver exceptions may contain credentials or key values
        raise HTTPException(503, "Migration database operation failed") from None
    return respond(result)


@router.post("/import")
async def import_key(
    request: Request, service: Annotated[KeyMigrationService, Depends(migration_service)]
) -> JSONResponse:
    data: Final = await decode(request, ImportRequest)
    from litellm.proxy.proxy_server import user_api_key_cache

    try:
        result: Final = await service.import_key(data)
        if isinstance(result, ImportedKey):
            await user_api_key_cache.async_delete_cache(result.key_hash)
    except Exception:  # noqa: BLE001  # SQL/cache details must stay out of HTTP errors
        raise HTTPException(503, "Migration database operation failed; retry import") from None
    return respond(result)
