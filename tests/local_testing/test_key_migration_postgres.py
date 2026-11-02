"""Owner DB regressions; run only against an isolated migrated LiteLLM test DB.

MIGRATION_TEST_DATABASE_URL=... python -m unittest discover -s tests/local_testing -p test_key_migration_postgres.py
"""

import asyncio
import importlib.util
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import HTTPException
from prisma import Prisma

MODULE = Path(__file__).resolve().parents[2] / "litellm/proxy/management_endpoints/key_migration.py"
spec = importlib.util.spec_from_file_location("migration_db_contract", MODULE)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


@unittest.skipUnless(os.environ.get("MIGRATION_TEST_DATABASE_URL"), "isolated PostgreSQL required")
class OwnerDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Prisma(datasource={"url": os.environ["MIGRATION_TEST_DATABASE_URL"]})
        await self.db.connect()
        self.store = migration.MigrationStore(migration.PrismaMigrationDatabase(self.db))
        self.team = "ap221-" + uuid4().hex
        self.hash = uuid4().hex + uuid4().hex
        await self.db.execute_raw(
            'INSERT INTO "LiteLLM_TeamTable" (team_id, models) VALUES ($1, ARRAY[]::text[])', self.team
        )

    async def asyncTearDown(self):
        await self.db.execute_raw('DELETE FROM "LiteLLM_VerificationToken" WHERE team_id=$1', self.team)
        await self.db.execute_raw('DELETE FROM "LiteLLM_DeletedVerificationToken" WHERE team_id=$1', self.team)
        await self.db.execute_raw('DELETE FROM "LiteLLM_TeamTable" WHERE team_id=$1', self.team)
        await self.db.disconnect()

    def request(self, **settings):
        return migration.Import(
            contract_version=1,
            team_id=self.team,
            key=migration.Key(key_hash=self.hash, settings=migration.Settings(**settings)),
        )

    async def inspect(self):
        return await self.store.inspect(migration.Inspect(contract_version=1, team_id=self.team, hashes=[self.hash]))

    async def test_concurrent_import_and_retry_preserve_spend(self):
        request = self.request()
        results = await asyncio.gather(*(self.store.import_key(request) for _ in range(3)))
        self.assertEqual(sum(r["status"] == "created" for r in results), 1)
        await self.db.execute_raw('UPDATE "LiteLLM_VerificationToken" SET spend=12.5 WHERE token=$1', self.hash)
        self.assertEqual((await self.store.import_key(request))["status"], "already_present")
        rows = await self.db.query_raw('SELECT spend FROM "LiteLLM_VerificationToken" WHERE token=$1', self.hash)
        self.assertEqual(rows[0]["spend"], 12.5)

    async def test_zero_limits_unknown_fields_and_metadata_rejected(self):
        await self.store.import_key(self.request())
        for column in ("tpd_limit", "max_budget"):
            await self.db.execute_raw(f'UPDATE "LiteLLM_VerificationToken" SET "{column}"=0 WHERE token=$1', self.hash)
            with self.assertRaises(HTTPException) as error:
                await self.inspect()
            self.assertEqual(error.exception.status_code, 409)
            self.assertIn(column, error.exception.detail)
            await self.db.execute_raw(
                f'UPDATE "LiteLLM_VerificationToken" SET "{column}"=NULL WHERE token=$1', self.hash
            )
        await self.db.execute_raw(
            'ALTER TABLE "LiteLLM_VerificationToken" ADD COLUMN ap221_unknown_limit DOUBLE PRECISION'
        )
        try:
            await self.db.execute_raw(
                'UPDATE "LiteLLM_VerificationToken" SET ap221_unknown_limit=0 WHERE token=$1', self.hash
            )
            with self.assertRaises(HTTPException):
                await self.inspect()
        finally:
            await self.db.execute_raw('ALTER TABLE "LiteLLM_VerificationToken" DROP COLUMN ap221_unknown_limit')
        await self.db.execute_raw(
            'UPDATE "LiteLLM_VerificationToken" SET metadata=\'{"custom":true}\'::jsonb WHERE token=$1', self.hash
        )
        with self.assertRaises(HTTPException):
            await self.inspect()

    async def test_expiry_preserves_utc_with_moscow_session(self):
        # The real schema's view prevents altering expires. Isolated owner
        # schemas exercise both types without modifying that view.
        expected = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        for column_type in ("TIMESTAMP", "TIMESTAMPTZ"):
            schema = "ap221_" + uuid4().hex
            await self.db.execute_raw(f'CREATE SCHEMA "{schema}"')
            connection = None
            try:
                await self.db.execute_raw(f'''CREATE TABLE "{schema}"."LiteLLM_VerificationToken" (
                    token TEXT PRIMARY KEY,team_id TEXT,key_name TEXT,key_alias TEXT,
                    key_type TEXT,expires {column_type},models TEXT[],rpm_limit BIGINT,
                    tpm_limit BIGINT,max_parallel_requests INTEGER,blocked BOOLEAN,
                    metadata JSONB DEFAULT '{{}}',spend DOUBLE PRECISION DEFAULT 0)''')
                await self.db.execute_raw(f'CREATE TABLE "{schema}"."LiteLLM_DeletedVerificationToken" (token TEXT)')
                await self.db.execute_raw(
                    f'CREATE TABLE "{schema}"."LiteLLM_TeamTable" (team_id TEXT,blocked BOOLEAN,models TEXT[])'
                )
                await self.db.execute_raw(
                    f'INSERT INTO "{schema}"."LiteLLM_TeamTable" VALUES ($1,false,ARRAY[]::text[])', self.team
                )
                parts = urlsplit(os.environ["MIGRATION_TEST_DATABASE_URL"])
                query = dict(parse_qsl(parts.query))
                query["schema"] = schema
                connection = Prisma(datasource={"url": urlunsplit(parts._replace(query=urlencode(query)))})
                await connection.connect()
                await connection.execute_raw("SET TIME ZONE 'Europe/Moscow'")
                store = migration.MigrationStore(migration.PrismaMigrationDatabase(connection))
                request = self.request(expires=expected)
                await store.import_key(request)
                result = await store.inspect(
                    migration.Inspect(contract_version=1, team_id=self.team, hashes=[self.hash])
                )
                self.assertEqual(result["keys"][0]["settings"]["expires"], "2030-01-01T12:00:00Z")
                self.assertEqual((await store.import_key(request))["status"], "already_present")
            finally:
                if connection is not None:
                    await connection.disconnect()
                await self.db.execute_raw(f'DROP SCHEMA "{schema}" CASCADE')

    async def test_unknown_required_column_rejected_before_insert(self):
        await self.db.execute_raw(
            "ALTER TABLE \"LiteLLM_VerificationToken\" ADD COLUMN ap221_required TEXT NOT NULL DEFAULT 'temporary'"
        )
        try:
            await self.db.execute_raw(
                'ALTER TABLE "LiteLLM_VerificationToken" ALTER COLUMN ap221_required DROP DEFAULT'
            )
            with self.assertRaises(HTTPException) as error:
                await self.store.import_key(self.request())
            self.assertEqual(error.exception.status_code, 409)
            rows = await self.db.query_raw(
                'SELECT count(*) AS total FROM "LiteLLM_VerificationToken" WHERE token=$1', self.hash
            )
            self.assertEqual(rows[0]["total"], 0)
        finally:
            await self.db.execute_raw('ALTER TABLE "LiteLLM_VerificationToken" DROP COLUMN ap221_required')

    async def test_concurrent_alias_claim_only_one_key(self):
        requests = [self.request(key_alias=self.team) for _ in range(3)]
        for request in requests:
            request.key.key_hash = uuid4().hex + uuid4().hex
        results = await asyncio.gather(*(self.store.import_key(r) for r in requests), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) and r["status"] == "created" for r in results), 1)
        self.assertEqual(sum(isinstance(r, HTTPException) and r.status_code == 409 for r in results), 2)

    async def test_alias_conflict_and_changed_settings(self):
        await self.store.import_key(self.request(key_alias=self.team))
        other = self.request(key_alias=self.team)
        other.key.key_hash = "f" * 64
        with self.assertRaises(HTTPException):
            await self.store.import_key(other)
        with self.assertRaises(HTTPException):
            await self.store.import_key(self.request(key_alias="changed"))

    async def test_archived_key_not_revived(self):
        await self.db.execute_raw(
            'INSERT INTO "LiteLLM_DeletedVerificationToken" (id,token,team_id,models,deleted_at) VALUES ($3,$1,$2,ARRAY[]::text[],now())',
            self.hash,
            self.team,
            uuid4().hex,
        )
        with self.assertRaises(HTTPException):
            await self.store.import_key(self.request())

    async def test_inactive_key_can_be_excluded_with_unsupported_metadata(self):
        await self.store.import_key(self.request())
        await self.db.execute_raw(
            'UPDATE "LiteLLM_VerificationToken" SET blocked=true,metadata=\'{"old":true}\'::jsonb WHERE token=$1',
            self.hash,
        )
        self.assertEqual((await self.inspect())["keys"][0]["status"], "blocked")
        await self.db.execute_raw(
            "UPDATE \"LiteLLM_VerificationToken\" SET blocked=false,expires='2020-01-01' WHERE token=$1", self.hash
        )
        self.assertEqual((await self.inspect())["keys"][0]["status"], "expired")

    async def test_preflight_conflicts_do_not_insert_any_keys(self):
        await self.store.import_key(self.request())
        different = self.request(key_name="changed")
        preflight = migration.Preflight(contract_version=1, team_id=self.team, keys=[different.key])
        with self.assertRaises(HTTPException):
            await self.store.preflight(preflight)
        rows = await self.db.query_raw(
            'SELECT count(*) AS total FROM "LiteLLM_VerificationToken" WHERE team_id=$1', self.team
        )
        self.assertEqual(rows[0]["total"], 1)


if __name__ == "__main__":
    unittest.main()
