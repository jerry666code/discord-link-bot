import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiomysql

# Import the bot without credentials, dotenv files, or a live Discord connection.
with patch.dict(os.environ, {
    "DISCORD_BOT_TOKEN": "test", "GUILD_ID": "1", "VERIFIED_ROLE_ID": "2",
    "DB_HOST": "localhost", "DB_PORT": "3306", "DB_NAME": "test",
    "DB_USER": "test", "DB_PASS": "test", "DISCORD_CLIENT_ID": "test",
    "DISCORD_CLIENT_SECRET": "test", "RELAY_SECRET": "test", "PORT": "8080",
    "SYNC_INTERVAL_SECONDS": "3600",
}), patch("dotenv.load_dotenv"):
    import bot


STEAM = "76561198000000000"
ADMIN_MAP = {"Admin": {11}}
VIP_MAP = {"vip": {12}}
FACEIT_MAP = {"7": {70}, "8": {80, 81}, "10": {100}}
PUNISHMENT_ROLES = {20}
STATE = (
    [("42", STEAM)], {STEAM: {"Admin"}},
    {int(STEAM) - bot.STEAMID64_BASE: {"vip"}}, {STEAM: 8}, set(),
)


class MappingTests(unittest.TestCase):
    def test_optional_section_and_live_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.json"
            with patch.object(bot, "ROLE_MAPPING_FILE", str(path)):
                path.write_text('{"admin_groups":{"Admin":"11"}}', encoding="utf-8-sig")
                self.assertEqual(bot.load_role_mapping()["faceit_levels"], {})
                self.assertEqual(bot.load_role_mapping()["punishment_roles"], {})
                path.write_text(json.dumps({"faceit_levels": {
                    "7": "70", "8": ["80", "81"], "10": [],
                }}), encoding="utf-8-sig")
                self.assertEqual(bot.load_role_mapping()["faceit_levels"], {"7": {70}, "8": {80, 81}})

    def test_punishment_role_supports_single_id_and_multiple_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.json"
            with patch.object(bot, "ROLE_MAPPING_FILE", str(path)):
                for value, expected in (("20", {20}), (["20", "21"], {20, 21}), ([], set())):
                    path.write_text(json.dumps({"punishment_roles": {"active": value}}), encoding="utf-8")
                    self.assertEqual(bot.load_role_mapping()["punishment_roles"].get("active", set()), expected)
                    *_, punishment_ids, managed_ids = bot.bot._load_role_mapping_and_managed_ids()
                    self.assertEqual(punishment_ids, expected)
                    self.assertEqual(managed_ids, expected)

    def test_invalid_level_or_role_cannot_manage_a_role(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.json"
            path.write_text(json.dumps({"faceit_levels": {
                "0": "90", "11": "91", "8": ["bad", -1, True, 1.5, "80"],
            }}), encoding="utf-8")
            with patch.object(bot, "ROLE_MAPPING_FILE", str(path)), self.assertLogs(bot.log, "WARNING"):
                self.assertEqual(bot.load_role_mapping()["faceit_levels"], {"8": {80}})


class RoleSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.roles = {role_id: SimpleNamespace(id=role_id) for role_id in (2, 11, 12, 20, 70, 80, 81, 100, 999)}
        self.member = SimpleNamespace(id=42, roles=[self.roles[2], self.roles[70], self.roles[999]])

        async def add_roles(*roles, **kwargs):
            self.member.roles.extend(role for role in roles if role not in self.member.roles)

        async def remove_roles(*roles, **kwargs):
            self.member.roles[:] = [role for role in self.member.roles if role not in roles]

        self.member.add_roles = AsyncMock(side_effect=add_roles)
        self.member.remove_roles = AsyncMock(side_effect=remove_roles)
        self.guild = SimpleNamespace(
            get_role=self.roles.get, get_member=lambda member_id: self.member,
            members=[self.member],
        )

    async def run_sync(self, state, periodic=False):
        with patch.object(bot.bot, "get_guild", return_value=self.guild), \
                patch.object(bot.bot, "_load_role_mapping_and_managed_ids", return_value=(
                    ADMIN_MAP, VIP_MAP, FACEIT_MAP, PUNISHMENT_ROLES, {11, 12, 20, 70, 80, 81, 100},
                )), patch.object(bot.bot, "_load_role_state", new=AsyncMock(return_value=state)):
            if periodic:
                await bot.LinkBot.sync_roles.coro(bot.bot)
            else:
                await bot.bot.sync_member_now("42")

    async def test_level_change_replaces_old_role_and_is_idempotent_in_both_paths(self):
        await self.run_sync(STATE)
        self.assertEqual({role.id for role in self.member.roles}, {2, 11, 12, 80, 81, 999})
        self.member.add_roles.reset_mock()
        self.member.remove_roles.reset_mock()
        await self.run_sync(STATE, periodic=True)
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()

    async def test_missing_faceit_data_preserves_faceit_and_still_updates_admin_vip(self):
        await self.run_sync((*STATE[:3], {}, set()), periodic=True)
        self.assertEqual({role.id for role in self.member.roles}, {2, 11, 12, 70, 999})

    async def test_confirmed_no_faceit_or_unmapped_level_removes_old_role(self):
        for level in (0, 9):
            with self.subTest(level=level):
                self.member.roles = [self.roles[2], self.roles[70], self.roles[999]]
                await self.run_sync((*STATE[:3], {STEAM: level}, set()))
                self.assertEqual({role.id for role in self.member.roles}, {2, 11, 12, 999})

    async def test_discord_unlink_removes_all_managed_roles(self):
        self.member.roles = list(self.roles.values())
        await self.run_sync(([], {}, {}, {}, set()), periodic=True)
        self.assertEqual({role.id for role in self.member.roles}, {999})

    async def test_punishment_role_added_then_removed_when_last_active_punishment_ends(self):
        await self.run_sync((*STATE[:4], {STEAM}), periodic=True)
        self.assertEqual({role.id for role in self.member.roles}, {2, 11, 12, 20, 80, 81, 999})
        await self.run_sync(STATE)
        self.assertEqual({role.id for role in self.member.roles}, {2, 11, 12, 80, 81, 999})

    async def test_unknown_punishments_preserve_role_but_do_not_grant_it(self):
        await self.run_sync((*STATE[:4], None))
        self.assertNotIn(self.roles[20], self.member.roles)
        self.member.roles.append(self.roles[20])
        await self.run_sync((*STATE[:4], None), periodic=True)
        self.assertIn(self.roles[20], self.member.roles)


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def load_state(
        self, faceit_rows=None, error=None, mapping=FACEIT_MAP, punishment_roles=None,
        punishment_error=None, punishment_rows=None, punishment_db=None,
    ):
        cursor = MagicMock()
        async def execute(sql):
            if "faceit_profiles" in sql and error:
                raise error
            if "as_punishments" in sql:
                if punishment_error:
                    raise punishment_error
                if punishment_db is not None:
                    cursor.fetchall.side_effect = None
                    cursor.fetchall.return_value = punishment_db.execute(sql).fetchall()

        cursor.execute = AsyncMock(side_effect=execute)
        cursor.fetchall = AsyncMock(side_effect=[
            STATE[0], [(STEAM, "Admin")], [(int(STEAM) - bot.STEAMID64_BASE, "vip")],
            *([faceit_rows or []] if mapping and not error else []),
            *([punishment_rows or []] if punishment_roles and not punishment_error else []),
        ])
        cursor.__aenter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor
        acquired = MagicMock()
        acquired.__aenter__.return_value = connection
        pool = SimpleNamespace(acquire=lambda: acquired)
        with patch.object(bot.bot, "db_pool", pool):
            result = await bot.bot._load_role_state(ADMIN_MAP, VIP_MAP, mapping, punishment_roles)
        return result, cursor

    async def test_site_cache_level_and_confirmed_absence(self):
        result, _ = await self.load_state([(STEAM, 8, 1)])
        self.assertEqual(result, STATE)
        result, _ = await self.load_state([(STEAM, 8, 0)])
        self.assertEqual(result[3], {STEAM: 0})

    async def test_faceit_database_failure_does_not_block_other_roles(self):
        with self.assertLogs(bot.log, "ERROR"):
            result, _ = await self.load_state(error=aiomysql.OperationalError(1146, "missing table"))
        self.assertEqual(result, (*STATE[:3], {}, set()))

    async def test_invalid_cache_is_unknown_and_disabled_mapping_skips_query(self):
        with self.assertLogs(bot.log, "WARNING"):
            result, _ = await self.load_state([(STEAM, 255, 1)])
        self.assertEqual(result[3], {})
        result, cursor = await self.load_state(mapping={})
        self.assertEqual(result[3], {})
        self.assertEqual(cursor.execute.await_count, 3)

    async def test_active_punishment_query_filters_expired_and_revoked_and_normalizes_steamids(self):
        with closing(sqlite3.connect(":memory:")) as database:
            database.create_function("UNIX_TIMESTAMP", 0, lambda: 1000)
            database.execute(
                "CREATE TABLE as_punishments (steamid TEXT, expires INTEGER, "
                "unpunish_admin_id INTEGER, punish_type INTEGER)"
            )
            permanent, temporary, expired, revoked, boundary = [
                str(int(STEAM) + offset) for offset in range(5)
            ]
            formats = bot.steamid_all_formats(temporary)
            database.executemany("INSERT INTO as_punishments VALUES (?, ?, ?, ?)", [
                (permanent, 0, None, 0),
                (formats[1], 1001, 0, 1), (formats[2], 1002, None, 2),
                (formats[3], 1003, 0, 3),
                (expired, 999, 0, 0), (revoked, 0, 5, 0),
                (boundary, 1000, None, 1), ("invalid", 0, 0, 0),
            ])
            result, _ = await self.load_state(
                [(STEAM, 8, 1)], punishment_roles=PUNISHMENT_ROLES, punishment_db=database,
            )
            self.assertEqual(result[4], {permanent, temporary})
            # One remaining sanction still keeps the player active.
            database.execute("DELETE FROM as_punishments WHERE punish_type IN (1, 2)")
            result, _ = await self.load_state(
                [(STEAM, 8, 1)], punishment_roles=PUNISHMENT_ROLES, punishment_db=database,
            )
            self.assertEqual(result[4], {permanent, temporary})

    async def test_punishment_query_failure_preserves_other_sources(self):
        with self.assertLogs(bot.log, "ERROR"):
            result, _ = await self.load_state(
                [(STEAM, 8, 1)], punishment_roles=PUNISHMENT_ROLES,
                punishment_error=aiomysql.OperationalError(1142, "SELECT denied"),
            )
        self.assertEqual(result, (*STATE[:4], None))


if __name__ == "__main__":
    unittest.main()
