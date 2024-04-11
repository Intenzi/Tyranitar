import asyncio
import os

import aiohttp
import asqlite
import discord
from discord.ext import commands
from playwright.async_api import async_playwright

from secrets import TOKEN


discord.utils.setup_logging()

intents = discord.Intents.default()
intents.message_content = True  # privileged intent
bot = commands.Bot(command_prefix=commands.when_mentioned_or("."), case_insensitive=True, strip_after_prefix=True, activity=discord.Activity(type=3, name="Gen 3 from afar"), intents=intents)


@bot.hybrid_command()
async def ping(ctx):
    await ctx.send(f'Pong! {round(bot.latency * 1000)}ms')


@bot.command()
@commands.is_owner()
async def sync(ctx):  # use this anytime a new slash command is made
    await bot.tree.sync()
    await ctx.send("Successfully synced all commands!")


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')


bot.owner_ids = {378678890345791489, 296937641121939456}  # for calling sync cmd


create_query = """
CREATE TABLE IF NOT EXISTS psreplays (
    replayid TEXT PRIMARY KEY,
    format_text TEXT,
    battle_text1 TEXT[],
    battle_text2 TEXT[]
);
"""


async def main():
    async with bot, asqlite.create_pool('database.db') as pool:
        for cog in os.listdir('Cogs'):
            if cog.endswith('.py'):
                await bot.load_extension('Cogs.' + cog[:-3])

        bot.session = aiohttp.ClientSession()
        bot.pool = pool
        async with pool.acquire() as conn:
            # Create the table if it doesn't exist
            await conn.execute(create_query)
            # Commit the transaction
            await conn.commit()

        bot.playwright = await async_playwright().start()
        bot.browser = await bot.playwright.chromium.launch()
        print("Browser on standby")
        await bot.start(TOKEN)

asyncio.run(main())
