import hmac
import json
import logging
import os
import re

import aiohttp
import aiomysql
import discord
from aiohttp import web
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

STEAMID64_BASE = 76561197960265728

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
VERIFIED_ROLE_ID = int(os.environ["VERIFIED_ROLE_ID"])
LINK_URL = os.environ.get("LINK_URL", "https://www.boberland.ru/api/auth/discord/link")
# По умолчанию раз в час — под VERIFIED_ROLE_ID и под роли из ROLE_MAPPING_FILE.
SYNC_INTERVAL_SECONDS = int(os.environ.get("SYNC_INTERVAL_SECONDS", "3600"))
# Дефолт — рядом с bot.py, а не от текущей рабочей директории процесса: на
# некоторых хостингах CWD при запуске не совпадает с папкой бота.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROLE_MAPPING_FILE = os.environ.get("ROLE_MAPPING_FILE", os.path.join(BASE_DIR, "role_mapping.json"))

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASS = os.environ["DB_PASS"]

# Релей для сайта: у сервера сайта заблокирован исходящий доступ к
# discord.com (подтверждено diag_network.php), поэтому обмен OAuth-кода на
# токен делается отсюда — этот сервер до discord.com достаёт нормально.
DISCORD_CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
DISCORD_CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
RELAY_SECRET = os.environ["RELAY_SECRET"]
PORT = int(os.environ.get("PORT", "8080"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("link-bot")

intents = discord.Intents.default()
intents.members = True

GUILD_OBJECT = discord.Object(id=GUILD_ID)

_STEAM_LEGACY_RE = re.compile(r"^STEAM_[0-5]:([01]):(\d+)$", re.IGNORECASE)
_STEAM64_RE = re.compile(r"^7656119\d{10}$")


def normalize_steamid64(steamid: str):
    """Порт Services\\SteamIdentity::normalizeTo64 (public/src/Services/SteamIdentity.php) —
    as_admins.steamid хранится либо как SteamID64, либо как "STEAM_1:Y:Z"."""
    steamid = (steamid or "").strip()
    if _STEAM64_RE.match(steamid):
        return steamid
    m = _STEAM_LEGACY_RE.match(steamid)
    if m:
        return str(STEAMID64_BASE + int(m.group(2)) * 2 + int(m.group(1)))
    return None


def load_role_mapping() -> dict:
    """Читает ROLE_MAPPING_FILE: {"admin_groups": {"Название AS-группы": "discord_role_id"},
    "vip_groups": {"group_key": "discord_role_id"}}. Ключи должны совпадать с
    as_groups.name (Админ-панель → Настройки → Админ-группы, AdminSystem для CS2)
    и admin_vip_groups.group_key (VIPCore groups.ini) соответственно — файл
    перечитывается на каждом тике, перезапуск бота не нужен. Значением может быть
    один ID роли или список ID (["111", "222"]), если группе нужно выдавать
    сразу несколько ролей в Discord."""
    empty = {"admin_groups": {}, "vip_groups": {}}
    try:
        # utf-8-sig проглатывает BOM, который некоторые редакторы (в т.ч. на
        # хостингах) молча добавляют в начало файла и который иначе ломает
        # json.load с ошибкой "Expecting value: line 1 column 1".
        with open(ROLE_MAPPING_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        log.warning(
            "%s не найден — роли администратора/VIP синхронизироваться не будут "
            "(проверьте, что файл загружен на хостинг рядом с bot.py)",
            ROLE_MAPPING_FILE,
        )
        return empty
    except (OSError, json.JSONDecodeError) as e:
        log.error("Не удалось прочитать %s: %s", ROLE_MAPPING_FILE, e)
        return empty

    def to_role_id_map(section: str) -> dict:
        result = {}
        for key, raw_value in (data.get(section) or {}).items():
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            role_ids = set()
            for value in values:
                try:
                    role_ids.add(int(value))
                except (TypeError, ValueError):
                    log.warning("Некорректный ID роли для %r в %s (%s): %r", key, ROLE_MAPPING_FILE, section, value)
            if role_ids:
                result[key] = role_ids
        return result

    return {
        "admin_groups": to_role_id_map("admin_groups"),
        "vip_groups": to_role_id_map("vip_groups"),
    }


async def handle_discord_exchange(request: web.Request) -> web.Response:
    if not hmac.compare_digest(request.headers.get("X-Relay-Secret", ""), RELAY_SECRET):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
    except ValueError:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    code = payload.get("code")
    redirect_uri = payload.get("redirect_uri")
    if not code or not redirect_uri:
        return web.json_response({"ok": False, "error": "missing_code_or_redirect_uri"}, status=400)

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_id": DISCORD_CLIENT_ID,
                    "client_secret": DISCORD_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            ) as token_resp:
                token_data = await token_resp.json(content_type=None)
        except aiohttp.ClientError as e:
            return web.json_response({"ok": False, "error": f"token_request_failed: {e}"}, status=502)

        access_token = token_data.get("access_token")
        if not access_token:
            error = token_data.get("error_description") or token_data.get("error") or "token_exchange_failed"
            return web.json_response({"ok": False, "error": error}, status=502)

        try:
            async with session.get(
                "https://discord.com/api/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            ) as identity_resp:
                identity = await identity_resp.json(content_type=None)
        except aiohttp.ClientError as e:
            return web.json_response({"ok": False, "error": f"identity_request_failed: {e}"}, status=502)

    if not identity.get("id"):
        return web.json_response({"ok": False, "error": "identity_fetch_failed"}, status=502)

    return web.json_response({
        "ok": True,
        "id": identity["id"],
        "username": identity.get("username", "discord"),
        "discriminator": identity.get("discriminator", "0"),
    })


class LinkBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, status=discord.Status.invisible)
        self.db_pool = None

    async def setup_hook(self):
        # Привязку пишет только сайт (AuthController::discordLinkCallback) — боту
        # для синхронизации ролей достаточно SELECT, отдельный read-only юзер
        # MySQL безопаснее, чем шарить сюда основные DB_* сайта.
        self.db_pool = await aiomysql.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            db=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            autocommit=True,
            minsize=1,
            maxsize=5,
        )
        self.tree.copy_global_to(guild=GUILD_OBJECT)
        await self.tree.sync(guild=GUILD_OBJECT)
        self.sync_roles.start()

        try:
            app = web.Application()
            app.router.add_post("/discord-exchange", handle_discord_exchange)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", PORT)
            await site.start()
            log.info("Relay HTTP server listening on port %s", PORT)
        except OSError as e:
            # Не даём релею уронить весь бот — /link, /status и синхронизация
            # ролей с портом не связаны и должны работать даже если релей не
            # смог стартовать (например, порт уже занят).
            log.error("Relay HTTP server failed to start on port %s: %s", PORT, e)

    async def close(self):
        self.sync_roles.cancel()
        if self.db_pool is not None:
            self.db_pool.close()
            await self.db_pool.wait_closed()
        await super().close()

    @tasks.loop(seconds=SYNC_INTERVAL_SECONDS)
    async def sync_roles(self):
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            return
        role = guild.get_role(VERIFIED_ROLE_ID)
        if role is None:
            log.warning("VERIFIED_ROLE_ID %s not found in guild %s", VERIFIED_ROLE_ID, GUILD_ID)
            return

        role_mapping = load_role_mapping()
        admin_role_map = role_mapping["admin_groups"]
        vip_role_map = role_mapping["vip_groups"]
        # Роли, которыми бот управляет сам — их можно снимать, если участник
        # больше не подпадает ни под один маппинг; остальные роли не трогаем.
        managed_role_ids = set()
        for role_ids in admin_role_map.values():
            managed_role_ids |= role_ids
        for role_ids in vip_role_map.values():
            managed_role_ids |= role_ids

        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT discord_id, steamid64 FROM users WHERE discord_id IS NOT NULL")
                linked_rows = await cur.fetchall()

                admin_groups_by_steamid64 = {}
                if admin_role_map:
                    # as_admins.steamid — SteamID64 или "STEAM_1:Y:Z" (заполняется вручную
                    # в панели или игровым AdminSystem), поэтому нормализуем на стороне бота.
                    await cur.execute(
                        "SELECT a.steamid, g.name FROM as_admins a "
                        "JOIN as_admins_servers s ON s.admin_id = a.id "
                        "JOIN as_groups g ON g.id = s.group_id "
                        "WHERE a.steamid != '0' AND (s.expires = 0 OR s.expires > UNIX_TIMESTAMP())"
                    )
                    for raw_steamid, group_name in await cur.fetchall():
                        steamid64 = normalize_steamid64(raw_steamid)
                        if steamid64:
                            admin_groups_by_steamid64.setdefault(steamid64, set()).add(group_name)

                vip_groups_by_account_id = {}
                if vip_role_map:
                    await cur.execute(
                        "SELECT account_id, `group` FROM vip_users WHERE expires = 0 OR expires > UNIX_TIMESTAMP()"
                    )
                    for account_id, group_key in await cur.fetchall():
                        vip_groups_by_account_id.setdefault(account_id, set()).add(group_key)

        linked_ids = {str(discord_id) for discord_id, _ in linked_rows}
        steamid64_by_discord = {
            str(discord_id): steamid64 for discord_id, steamid64 in linked_rows if steamid64
        }

        for member in guild.members:
            discord_id = str(member.id)
            is_linked = discord_id in linked_ids
            has_role = role in member.roles
            try:
                if is_linked and not has_role:
                    await member.add_roles(role, reason="Discord привязан на сайте")
                elif not is_linked and has_role:
                    await member.remove_roles(role, reason="Discord отвязан на сайте")
            except discord.Forbidden:
                log.warning("Не хватает прав изменить роли для %s", member)

            if not managed_role_ids:
                continue

            desired_role_ids = set()
            steamid64 = steamid64_by_discord.get(discord_id)
            if steamid64:
                for group_name in admin_groups_by_steamid64.get(steamid64, ()):
                    desired_role_ids |= admin_role_map.get(group_name, set())

                try:
                    account_id = int(steamid64) - STEAMID64_BASE
                except ValueError:
                    account_id = None
                if account_id is not None:
                    for group_key in vip_groups_by_account_id.get(account_id, ()):
                        desired_role_ids |= vip_role_map.get(group_key, set())

            current_managed_ids = {r.id for r in member.roles} & managed_role_ids
            to_add = [guild.get_role(rid) for rid in desired_role_ids - current_managed_ids]
            to_remove = [guild.get_role(rid) for rid in current_managed_ids - desired_role_ids]
            to_add = [r for r in to_add if r is not None]
            to_remove = [r for r in to_remove if r is not None]

            try:
                if to_add:
                    await member.add_roles(*to_add, reason="Синхронизация роли/VIP с сайтом")
                if to_remove:
                    await member.remove_roles(*to_remove, reason="Роль/VIP на сайте больше не активны")
            except discord.Forbidden:
                log.warning("Не хватает прав изменить привилегированные роли для %s", member)

    @sync_roles.before_loop
    async def before_sync_roles(self):
        await self.wait_until_ready()


bot = LinkBot()


@bot.tree.command(name="link", description="Привязать Discord к профилю на сайте", guild=GUILD_OBJECT)
async def link(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Привязка Discord",
        description=(
            "1. Войдите на сайт через Steam.\n"
            f"2. Откройте [настройки профиля]({LINK_URL}) и нажмите «Привязать Discord».\n\n"
            "Роль на сервере (включая привилегии администратора и VIP) выдастся "
            "автоматически в течение часа после привязки/изменения на сайте."
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Проверить статус привязки Discord", guild=GUILD_OBJECT)
async def status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)

    async with bot.db_pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, steamid, discord_linked_at FROM users WHERE discord_id = %s",
                (str(interaction.user.id),),
            )
            row = await cur.fetchone()

    if row is None:
        await interaction.followup.send(
            "Discord не привязан к аккаунту на сайте. Используйте /link.", ephemeral=True
        )
        return

    await interaction.followup.send(
        f"Привязано к аккаунту #{row['id']} (SteamID {row['steamid']}), с {row['discord_linked_at']}.",
        ephemeral=True,
    )


if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
