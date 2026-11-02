import json
import os
import unittest
from typing import Final
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from litellm.proxy.management_endpoints.key_migration import MAX_BODY, migration_service, router
from litellm.types.proxy.management_endpoints.key_migration import Inspection, InspectRequest, MigrationTeam


class InspectService:
    async def inspect(self, request: InspectRequest) -> Inspection:
        return Inspection(team=MigrationTeam(team_id=request.team_id, models=(), blocked=False), keys=())


class KeyMigrationAuthorization(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[migration_service] = InspectService
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://lite")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_all_methods_reject_bad_auth_before_resolving_database(self):
        def unavailable_service():
            raise AssertionError("Unauthorized request reached the database dependency")

        self.app.dependency_overrides[migration_service] = unavailable_service
        for method in ("inspect", "preflight", "import"):
            for expected, headers in (
                ("secret", {}),
                ("secret", {"Authorization": "Bearer "}),
                ("secret", {"Authorization": "Basic secret"}),
                ("secret", {"Authorization": "Bearer wrong"}),
                ("secret", {"Authorization": "Bearer sk-master"}),
                ("secret", [("Authorization", "Bearer secret"), ("Authorization", "Bearer secret")]),
                ("", {"Authorization": "Bearer "}),
                ("", {"Authorization": "Bearer secret"}),
            ):
                with self.subTest(method=method, configured=bool(expected)):
                    with patch.dict(os.environ, {"LITELLM_KEY_MIGRATION_TOKEN": expected}):
                        response: Final = await self.client.post(
                            "/internal/key-migration/" + method, content=b"not-json", headers=headers
                        )
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers["www-authenticate"], "Bearer")
                    self.assertNotIn("secret", response.text)

    async def test_rotation_accepts_only_the_current_token(self):
        payload: Final = {"contract_version": 1, "team_id": "target", "hashes": []}
        for expected, supplied, status in (
            ("old", "old", 200),
            ("new", "old", 401),
            ("new", "new", 200),
            ("", "new", 401),
        ):
            with patch.dict(os.environ, {"LITELLM_KEY_MIGRATION_TOKEN": expected}):
                response: Final = await self.client.post(
                    "/internal/key-migration/inspect", json=payload, headers={"Authorization": "Bearer " + supplied}
                )
            self.assertEqual(response.status_code, status)

    async def test_invalid_or_oversized_request_does_not_reveal_input(self):
        requests: Final = (
            (b"x" * (MAX_BODY + 1), 413),
            (json.dumps({"contract_version": 2, "team_id": "target", "hashes": []}).encode(), 422),
            (json.dumps({"contract_version": 1, "team_id": "target", "hashes": [], "sql": "secret"}).encode(), 422),
            (json.dumps({"contract_version": 1, "team_id": "target", "hashes": ["a" * 64] * 101}).encode(), 422),
            (json.dumps({"contract_version": 1, "team_id": "target", "hashes": ["invalid"]}).encode(), 422),
        )
        for body, status in requests:
            with patch.dict(os.environ, {"LITELLM_KEY_MIGRATION_TOKEN": "secret"}):
                response: Final = await self.client.post(
                    "/internal/key-migration/inspect", content=body, headers={"Authorization": "Bearer secret"}
                )
            self.assertEqual(response.status_code, status)
            self.assertNotIn("secret", response.text)


if __name__ == "__main__":
    unittest.main()
