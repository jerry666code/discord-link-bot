import logging
import os

import aiomysql
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
VERIFIED_ROLE_ID = int(os.environ["VERIFIED_ROLE_ID"])
LINK_URL = os.environ.get("LINK_URL", "https://www.boberland.ru/api/auth/discord/link")
SYNC_INTERVAL_SECONDS = int(os.environ.get("SYNC_INTERVAL_SECONDS", "60"))

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASS = os.environ["DB_PASS"]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("link-bot")

intents = discord.Intents.default()
intents.members = True

GUILD_OBJECT = discord.Object(id=GUILD_ID)


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

        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT discord_id FROM users WHERE discord_id IS NOT NULL")
                linked_ids = {row[0] for row in await cur.fetchall()}

        for member in guild.members:
            is_linked = str(member.id) in linked_ids
            has_role = role in member.roles
            try:
                if is_linked and not has_role:
                    await member.add_roles(role, reason="Discord привязан на сайте")
                elif not is_linked and has_role:
                    await member.remove_roles(role, reason="Discord отвязан на сайте")
            except discord.Forbidden:
                log.warning("Не хватает прав изменить роли для %s", member)

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
            "Роль на сервере выдастся автоматически в течение минуты после привязки."
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
