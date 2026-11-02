"""Owner DB regressions; run only against an isolated migrated LiteLLM test DB.

MIGRATION_TEST_DATABASE_URL=... python -m unittest discover -s tests/local_testing -p test_key_migration_postgres.py
"""

import asyncio
import os
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from prisma import Prisma

from litellm.caching.dual_cache import DualCache
from litellm.proxy.management_helpers.key_migration import KeyMigrationService
from litellm.proxy.utils import PrismaClient, ProxyLogging
from litellm.types.proxy.management_endpoints.key_migration import (
    ImportRequest,
    InspectRequest,
    KeySettings,
    MigrationFailure,
    MigrationKey,
    PreflightRequest,
)


@unittest.skipUnless(os.environ.get("MIGRATION_TEST_DATABASE_URL"), "isolated PostgreSQL required")
class OwnerDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Prisma(datasource={"url": os.environ["MIGRATION_TEST_DATABASE_URL"]})
        await self.db.connect()
        self.client = PrismaClient(
            database_url=os.environ["MIGRATION_TEST_DATABASE_URL"],
            proxy_logging_obj=ProxyLogging(user_api_key_cache=DualCache()),
        )
        await self.client.connect()
        self.store = KeyMigrationService(self.client)
        self.team = "ap221-" + uuid4().hex
        self.hash = uuid4().hex + uuid4().hex
        await self.db.execute_raw(
            'INSERT INTO "LiteLLM_TeamTable" (team_id, models) VALUES ($1, ARRAY[]::text[])', self.team
        )

    async def asyncTearDown(self):
        await self.db.execute_raw('DELETE FROM "LiteLLM_VerificationToken" WHERE team_id=$1', self.team)
        await self.db.execute_raw('DELETE FROM "LiteLLM_DeletedVerificationToken" WHERE team_id=$1', self.team)
        await self.db.execute_raw('DELETE FROM "LiteLLM_TeamTable" WHERE team_id=$1', self.team)
        await self.client.disconnect()
        await self.db.disconnect()

    def request(self, **settings):
        return ImportRequest(
            contract_version=1,
            team_id=self.team,
            key=MigrationKey(key_hash=self.hash, settings=KeySettings(**settings)),
        )

    async def inspect(self):
        return await self.store.inspect(InspectRequest(contract_version=1, team_id=self.team, hashes=(self.hash,)))

    async def test_concurrent_import_and_retry_preserve_spend(self):
        request = self.request()
        results = await asyncio.gather(*(self.store.import_key(request) for _ in range(3)))
        self.assertEqual(sum(r.status == "created" for r in results), 1)
        await self.db.execute_raw('UPDATE "LiteLLM_VerificationToken" SET spend=12.5 WHERE token=$1', self.hash)
        self.assertEqual((await self.store.import_key(request)).status, "already_present")
        rows = await self.db.query_raw('SELECT spend FROM "LiteLLM_VerificationToken" WHERE token=$1', self.hash)
        self.assertEqual(rows[0]["spend"], 12.5)

    async def test_key_type_and_limits_round_trip(self):
        request = self.request(
            key_type="default", models=("migration-model",), rpm_limit=0, tpm_limit=17, max_parallel_requests=2
        )
        created = await self.store.import_key(request)
        self.assertEqual(created.status, "created")
        inspected = await self.inspect()
        self.assertEqual(inspected.keys[0].settings, request.key.settings)

    async def test_zero_limits_policies_and_metadata_rejected(self):
        await self.store.import_key(self.request())
        for column in ("tpd_limit", "max_budget"):
            await self.db.execute_raw(f'UPDATE "LiteLLM_VerificationToken" SET "{column}"=0 WHERE token=$1', self.hash)
            error = await self.inspect()
            self.assertIsInstance(error, MigrationFailure)
            self.assertEqual(error.status, 409)
            self.assertIn(column, error.detail)
            await self.db.execute_raw(
                f'UPDATE "LiteLLM_VerificationToken" SET "{column}"=NULL WHERE token=$1', self.hash
            )
        await self.db.execute_raw(
            "UPDATE \"LiteLLM_VerificationToken\" SET policies=ARRAY['restricted'] WHERE token=$1", self.hash
        )
        self.assertIsInstance(await self.inspect(), MigrationFailure)
        await self.db.execute_raw(
            'UPDATE "LiteLLM_VerificationToken" SET policies=ARRAY[]::text[],metadata=\'{"custom":true}\'::jsonb WHERE token=$1',
            self.hash,
        )
        self.assertIsInstance(await self.inspect(), MigrationFailure)

    async def test_expiry_preserves_utc_with_moscow_session(self):
        # The real schema's view prevents altering expires. Isolated owner
        # schemas exercise both types without modifying that view.
        expected = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        for column_type in ("TIMESTAMP", "TIMESTAMPTZ"):
            schema = "ap221_" + uuid4().hex
            await self.db.execute_raw(f'CREATE SCHEMA "{schema}"')
            connection = None
            try:
                for table in ("LiteLLM_VerificationToken", "LiteLLM_DeletedVerificationToken", "LiteLLM_TeamTable"):
                    await self.db.execute_raw(
                        f'CREATE TABLE "{schema}"."{table}" (LIKE public."{table}" INCLUDING ALL)'
                    )
                await self.db.execute_raw(
                    f'ALTER TABLE "{schema}"."LiteLLM_VerificationToken" ALTER COLUMN expires TYPE {column_type}'
                )
                await self.db.execute_raw(
                    f'INSERT INTO "{schema}"."LiteLLM_TeamTable" (team_id,models) VALUES ($1,ARRAY[]::text[])',
                    self.team,
                )
                parts = urlsplit(os.environ["MIGRATION_TEST_DATABASE_URL"])
                query = dict(parse_qsl(parts.query))
                query["schema"] = schema
                connection = PrismaClient(
                    database_url=urlunsplit(parts._replace(query=urlencode(query))),
                    proxy_logging_obj=ProxyLogging(user_api_key_cache=DualCache()),
                )
                await connection.connect()
                await connection.writer_db.execute_raw("SET TIME ZONE 'Europe/Moscow'")
                store = KeyMigrationService(connection)
                request = self.request(expires=expected)
                await store.import_key(request)
                result = await store.inspect(InspectRequest(contract_version=1, team_id=self.team, hashes=(self.hash,)))
                self.assertEqual(result.keys[0].settings.expires.isoformat(), "2030-01-01T12:00:00+00:00")
                self.assertEqual((await store.import_key(request)).status, "already_present")
            finally:
                if connection is not None:
                    await connection.disconnect()
                await self.db.execute_raw(f'DROP SCHEMA "{schema}" CASCADE')

    async def test_concurrent_alias_claim_only_one_key(self):
        requests = tuple(
            self.request(key_alias=self.team).model_copy(
                update={
                    "key": MigrationKey(key_hash=uuid4().hex + uuid4().hex, settings=KeySettings(key_alias=self.team))
                }
            )
            for _ in range(3)
        )
        results = await asyncio.gather(*(self.store.import_key(r) for r in requests), return_exceptions=True)
        self.assertEqual(sum(not isinstance(r, MigrationFailure) and r.status == "created" for r in results), 1)
        self.assertEqual(sum(isinstance(r, MigrationFailure) and r.status == 409 for r in results), 2)

    async def test_alias_conflict_and_changed_settings(self):
        await self.store.import_key(self.request(key_alias=self.team))
        other = self.request(key_alias=self.team)
        other = other.model_copy(update={"key": other.key.model_copy(update={"key_hash": "f" * 64})})
        self.assertIsInstance(await self.store.import_key(other), MigrationFailure)
        self.assertIsInstance(await self.store.import_key(self.request(key_alias="changed")), MigrationFailure)

    async def test_archived_key_not_revived(self):
        await self.db.execute_raw(
            'INSERT INTO "LiteLLM_DeletedVerificationToken" (id,token,team_id,models,deleted_at) VALUES ($3,$1,$2,ARRAY[]::text[],now())',
            self.hash,
            self.team,
            uuid4().hex,
        )
        self.assertIsInstance(await self.store.import_key(self.request()), MigrationFailure)

    async def test_inactive_key_can_be_excluded_with_unsupported_metadata(self):
        await self.store.import_key(self.request())
        await self.db.execute_raw(
            'UPDATE "LiteLLM_VerificationToken" SET blocked=true,metadata=\'{"old":true}\'::jsonb WHERE token=$1',
            self.hash,
        )
        self.assertEqual((await self.inspect()).keys[0].status, "blocked")
        await self.db.execute_raw(
            "UPDATE \"LiteLLM_VerificationToken\" SET blocked=false,expires='2020-01-01' WHERE token=$1", self.hash
        )
        self.assertEqual((await self.inspect()).keys[0].status, "expired")

    async def test_preflight_conflicts_do_not_insert_any_keys(self):
        await self.store.import_key(self.request())
        different = self.request(key_name="changed")
        preflight = PreflightRequest(contract_version=1, team_id=self.team, keys=(different.key,))
        self.assertIsInstance(await self.store.preflight(preflight), MigrationFailure)
        rows = await self.db.query_raw(
            'SELECT count(*) AS total FROM "LiteLLM_VerificationToken" WHERE team_id=$1', self.team
        )
        self.assertEqual(rows[0]["total"], 1)


if __name__ == "__main__":
    unittest.main()
