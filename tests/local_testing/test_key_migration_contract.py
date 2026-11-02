"""Standalone migration HTTP tests: python -m unittest discover -s tests/local_testing -p test_key_migration_contract.py.

Only FastAPI/httpx are required; the owner SQL implementation is exercised by the
AP-221 two-cluster PostgreSQL validation, without mocking the Prisma wire protocol.
"""

import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

MODULE = Path(__file__).resolve().parents[2] / "litellm/proxy/management_endpoints/key_migration.py"
spec = importlib.util.spec_from_file_location("migration_contract", MODULE)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class Store:
    def __init__(self):
        self.calls = []

    async def inspect(self, data):
        self.calls.append(data)
        return {"contract_version": 1, "team": {"team_id": data.team_id, "models": [], "blocked": False}, "keys": []}

    async def preflight(self, data):
        self.calls.append(data)
        return {"contract_version": 1, "keys": []}


class Contract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.include_router(migration.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://lite")
        self.store = Store()

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_all_methods_reject_bad_auth_before_database_or_body(self):
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
                with self.subTest(method=method, headers=headers, configured=bool(expected)):
                    with (
                        patch.dict(os.environ, {"LITELLM_KEY_MIGRATION_TOKEN": expected}),
                        patch.object(migration, "store", side_effect=AssertionError("DB must not be touched")),
                    ):
                        response = await self.client.post(
                            "/internal/key-migration/" + method, content=b"not-json", headers=headers
                        )
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers["www-authenticate"], "Bearer")
                    self.assertNotIn("secret", response.text)

    async def test_valid_token_and_rotation(self):
        payload = {"contract_version": 1, "team_id": "target", "hashes": []}
        for expected, supplied, status in (
            ("old", "old", 200),
            ("new", "old", 401),
            ("new", "new", 200),
            ("", "new", 401),
        ):
            with (
                patch.dict(os.environ, {"LITELLM_KEY_MIGRATION_TOKEN": expected}),
                patch.object(migration, "store", return_value=self.store),
            ):
                response = await self.client.post(
                    "/internal/key-migration/inspect", json=payload, headers={"Authorization": "Bearer " + supplied}
                )
            self.assertEqual(response.status_code, status)
        self.assertEqual(len(self.store.calls), 2)

    async def test_size_version_unknown_fields_and_count(self):
        requests = [
            (b"x" * (migration.MAX_BODY + 1), 413),
            (json.dumps({"contract_version": 2, "team_id": "target", "hashes": []}).encode(), 422),
            (json.dumps({"contract_version": 1, "team_id": "target", "hashes": [], "sql": "secret"}).encode(), 422),
            (json.dumps({"contract_version": 1, "team_id": "target", "hashes": ["a" * 64] * 101}).encode(), 422),
        ]
        for body, status in requests:
            with (
                patch.dict(os.environ, {"LITELLM_KEY_MIGRATION_TOKEN": "secret"}),
                patch.object(migration, "store", side_effect=AssertionError("DB must not be touched")),
            ):
                response = await self.client.post(
                    "/internal/key-migration/inspect", content=body, headers={"Authorization": "Bearer secret"}
                )
            self.assertEqual(response.status_code, status)
            self.assertNotIn("secret", response.text)


if __name__ == "__main__":
    unittest.main()
