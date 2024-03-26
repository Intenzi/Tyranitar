"""
This file is used to play published Pokémon Showdown replays onto discord

@author Intenzi
@license MIT
"""
import re
from datetime import datetime
from http import HTTPStatus

import aiohttp
import discord
from discord import app_commands as slash
from discord.ext import commands
from discord.ui import View, Button, Modal, TextInput
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from Helpers.task_cache import taskcache


def simple_sprite_gen(name: str, is_back: bool):
    """builds gen 3 sprites"""
    # simplify name
    name = name.lower().replace(':', '').replace(' ', '-').replace('%', '').replace('.', '').replace(
        '\u2019', '').replace('\u0301', '')
    if is_back:  # no gifs for back sprites
        base_url = "https://play.pokemonshowdown.com/sprites/gen3-back/"
        url_path = base_url + name + '.png'
    else:
        if name.startswith(('castform-', 'deoxys-')):  # the repo doesn't contain formes
            base_url = "https://play.pokemonshowdown.com/sprites/gen3/"
            url_path = base_url + name + '.png'
        else:
            base_url = "https://raw.githubusercontent.com/Dastardllydwarf/Emerald-Animated-Sprites/main/"
            url_path = base_url + name + '.gif'

    return url_path


class CurrentTurnButton(Button):
    """This button displays the current turn"""
    def __init__(self, total_turns, **kwargs):
        super().__init__(label=f"Turn 0/{total_turns}", disabled=True, style=discord.ButtonStyle.blurple, **kwargs)


class PreviousTurnButton(Button):
    def __init__(self, **kwargs):
        super().__init__(emoji="◀️", **kwargs, disabled=True)

    async def callback(self, interaction):
        embeds = interaction.message.embeds
        self.view.go_previous_turn(embeds)
        await interaction.response.edit_message(embeds=embeds, view=self.view)


class FirstTurnButton(Button):
    def __init__(self, **kwargs):
        super().__init__(emoji="⏮️", **kwargs, disabled=True)

    async def callback(self, interaction):
        embeds = interaction.message.embeds
        self.view.go_previous_turn(embeds, jump=0)
        await interaction.response.edit_message(embeds=embeds, view=self.view)


class NextTurnButton(Button):
    def __init__(self, **kwargs):
        super().__init__(emoji="▶️", **kwargs)

    async def callback(self, interaction):
        embeds = interaction.message.embeds
        self.view.go_forward_turn(embeds)
        await interaction.response.edit_message(embeds=embeds, view=self.view)


class LastTurnButton(Button):
    def __init__(self, **kwargs):
        super().__init__(emoji="⏭️", **kwargs)

    async def callback(self, interaction):
        embeds = interaction.message.embeds
        self.view.go_forward_turn(embeds, jump=self.view.total_turns)
        await interaction.response.edit_message(embeds=embeds, view=self.view)


class GoToTurnButton(Button):
    def __init__(self, **kwargs):
        super().__init__(emoji="🔢", label="Go To Turn", **kwargs)

    async def callback(self, interaction):
        await interaction.response.send_modal(GoToTurnModal(int(self.view.children[1].label[5:].split('/')[0]), self.view))


class GoToTurnModal(Modal):
    def __init__(self, current_turn, view):
        self.current_turn = current_turn
        self.view = view
        super().__init__(title="Jump to any turn")
        self.turn = TextInput(label="Enter Turn Number:", placeholder=f"Only from 0 to {view.total_turns}")
        self.add_item(self.turn)

    async def on_submit(self, interaction):
        turn = self.turn.value
        if not turn.isdigit():
            return await interaction.response.send_message("Please enter a valid turn number..", ephemeral=True)
        turn = int(turn)
        if turn not in range(self.view.total_turns+1):
            return await interaction.response.send_message(f"Please enter a number only between 0 to {self.view.total_turns}", ephemeral=True)
        if self.current_turn == turn:
            return await interaction.response.defer()

        embs = interaction.message.embeds
        if turn > self.current_turn:
            self.view.go_forward_turn(embs, jump=turn)
        else:
            self.view.go_previous_turn(embs, jump=turn)

        await interaction.response.edit_message(embeds=embs, view=self.view)


class SwapViewButton(Button):
    def __init__(self, player: str, **kwargs):
        super().__init__(emoji="🔀", label=player, **kwargs)

    async def callback(self, interaction):
        embs = interaction.message.embeds
        currently_p1 = self.label == self.view.p1
        current_turn = int(self.view.children[1].label[5:].split('/')[0])

        if currently_p1:
            embs[1].description = self.view.texts2[current_turn]
            embs[0].title.replace(f"{self.view.p1} vs. {self.view.p2}", f"{self.view.p2} vs. {self.view.p1}")
            self.label = self.view.p2
        else:
            embs[1].description = self.view.texts[current_turn]
            embs[0].title.replace(f"{self.view.p2} vs. {self.view.p1}", f"{self.view.p1} vs. {self.view.p2}")
            self.label = self.view.p1
        # just swap the images
        new_mon1_link = simple_sprite_gen(embs[1].thumbnail.url[:-4].replace("https://raw.githubusercontent.com/Dastardllydwarf/Emerald-Animated-Sprites/main/", '').replace("https://play.pokemonshowdown.com/sprites/gen3/", ''), is_back=True)
        new_mon2_link = simple_sprite_gen(embs[1].image.url[:-4].replace("https://play.pokemonshowdown.com/sprites/gen3-back/", ''), is_back=False)
        embs[1].set_thumbnail(url=new_mon2_link)
        embs[1].set_image(url=new_mon1_link)
        await interaction.response.edit_message(embeds=embs, view=self.view)


class ReplayViewerView(View):
    """unified viewer + doesn't care about which source the replay is from"""
    def __init__(self, user_id, replay_texts, replay_texts2, replay_images, format_text, battle_format, theme, p1, p2):
        self.user_id = user_id
        # consists of each turn's text and images
        self.texts = replay_texts
        self.texts2 = replay_texts2
        self.images = replay_images  # multiple images can be present for a particular turn due to switch in/switch out
        self.format_text = format_text
        self.theme = theme
        self.p1 = p1
        self.p2 = p2

        self.battle_format = battle_format
        self.total_turns = len(replay_texts) - 1
        super().__init__(timeout=840)  # 14 minute timeout

        self.build_main_ui(clear=False)

    def build_main_ui(self, clear=True):
        if clear:
            self.clear_items()
        self.add_item(PreviousTurnButton(row=0))
        self.add_item(CurrentTurnButton(self.total_turns, row=0))
        self.add_item(NextTurnButton(row=0))
        self.add_item(FirstTurnButton(row=1))
        self.add_item(GoToTurnButton(row=1))
        self.add_item(LastTurnButton(row=1))
        self.add_item(SwapViewButton(self.p1, row=1))

    async def interaction_check(self, interaction):
        return interaction.user.id == self.user_id

    @staticmethod
    def set_jumped_emb_img(emb, turn_texts, previous_turn, new_turn):
        """Need a go through of all turns based on jump for finalising image"""
        opposing_mon_pattern = re.compile(r'(?:\n|^)?.+?sent out.+?\*\*(.+?)\*\*\)?!(?:\n|$)')
        player_mon_pattern = re.compile(r'(?:\n|^)?Go!.+?\*\*(.+?)\*\*\)?!(?:\n|$)')
        # jumped ahead
        if new_turn > previous_turn:
            # we need text from previous turn till the latest turn
            finding_text = "".join(turn_texts[previous_turn:new_turn+1])
            if player_mon_pattern.findall(finding_text):
                mon1 = player_mon_pattern.findall(finding_text)[-1]
                emb.set_image(url=simple_sprite_gen(mon1, is_back=True))

            if opposing_mon_pattern.findall(finding_text):
                mon2 = opposing_mon_pattern.findall(finding_text)[-1]
                emb.set_thumbnail(url=simple_sprite_gen(mon2, is_back=False))
        else:  # includes equals case
            # we need text from 0th turn till the latest turn
            finding_text = "".join(turn_texts[:new_turn+1])
            if player_mon_pattern.findall(finding_text):
                mon1 = player_mon_pattern.findall(finding_text)[-1]
                emb.set_image(url=simple_sprite_gen(mon1, is_back=True))

            if opposing_mon_pattern.findall(finding_text):
                mon2 = opposing_mon_pattern.findall(finding_text)[-1]
                emb.set_thumbnail(url=simple_sprite_gen(mon2, is_back=False))

    @staticmethod
    def set_emb_img(turn_text, emb):
        """Modify only if found modifying trigger text, cannot deal with doubles properly"""
        # pattern is to prevent trainer names from leading to issues for example a player named "sent out **Gyarados**!"
        opposing_mon_pattern = re.compile(r'(?:\n|^)?.+?sent out.+?\*\*(.+?)\*\*\)?!(?:\n|$)')
        player_mon_pattern = re.compile(r'(?:\n|^)?Go!.+?\*\*(.+?)\*\*\)?!(?:\n|$)')
        # p1: "Go! **Pikachu**!"
        # p2: "Player 2 name sent out **Bulbasaur**!"
        if player_mon_pattern.findall(turn_text):
            mon1 = player_mon_pattern.findall(turn_text)[-1]  # last one here
            emb.set_image(url=simple_sprite_gen(mon1, is_back=True))

        if opposing_mon_pattern.findall(turn_text):
            mon2 = opposing_mon_pattern.findall(turn_text)[-1]
            emb.set_thumbnail(url=simple_sprite_gen(mon2, is_back=False))

    def go_forward_turn(self, embeds, jump: int = None):
        turn_btn = self.children[1]
        current_turn = int(turn_btn.label[5:].split('/')[0])
        emb1, emb2 = embeds
        if current_turn == 0:
            # modify embeds
            self.children[0].disabled = False  # previous turn button
            self.children[3].disabled = False  # first turn button
            emb1.description = None
            emb1.title = f"{self.battle_format}: {emb1.title}"

        if jump is not None:
            if self.children[6].label == self.p1:
                emb2.description = self.texts[jump]
                self.set_jumped_emb_img(emb2, self.texts, current_turn, jump)
            else:
                emb2.description = self.texts2[jump]
                self.set_jumped_emb_img(emb2, self.texts2, current_turn, jump)
            turn_btn.label = f"Turn {jump}/{self.total_turns}"
            current_turn = jump
        else:
            current_turn += 1
            if self.children[6].label == self.p1:
                emb2.description = self.texts[current_turn]
            else:
                emb2.description = self.texts2[current_turn]
            self.set_emb_img(emb2.description, emb2)
            turn_btn.label = f"Turn {current_turn}/{self.total_turns}"

        if current_turn == self.total_turns:
            self.children[2].disabled = True  # next turn btn
            self.children[5].disabled = True  # last turn btn

    def go_previous_turn(self, embeds, jump: int = None):
        turn_btn = self.children[1]
        current_turn = int(turn_btn.label[5:].split('/')[0])
        emb1, emb2 = embeds
        if current_turn == self.total_turns:
            # modify embeds
            self.children[2].disabled = False  # next turn button
            self.children[5].disabled = False  # last turn button

        if jump is not None:
            if self.children[6].label == self.p1:
                emb2.description = self.texts[jump]
                self.set_jumped_emb_img(emb2, self.texts, current_turn, jump)
            else:
                emb2.description = self.texts2[jump]
                self.set_jumped_emb_img(emb2, self.texts2, current_turn, jump)
            turn_btn.label = f"Turn {jump}/{self.total_turns}"
            current_turn = jump
        else:
            current_turn -= 1
            if self.children[6].label == self.p1:
                emb2.description = self.texts[current_turn]
            else:
                emb2.description = self.texts2[current_turn]
            self.set_emb_img(emb2.description, emb2)
            turn_btn.label = f"Turn {current_turn}/{self.total_turns}"

        if current_turn == 0:
            self.children[0].disabled = True  # previous turn btn
            self.children[3].disabled = True  # first turn btn
            emb1.description = self.format_text
            emb1.title = ': '.join(emb1.title.split(': ')[1:])


def html_battle_parser(html_text):
    """
    Handles the parsing of html battle text log (not same as .log file)
    """
    # Filtering at the very beginning
    text = html_text.replace('<div class="battle-options"></div><div class="inner message-log">', '').replace('</div><div class="inner-preempt message-log"></div>', '')
    # Now there are four types of divs at the topmost layer, along with h2 tags
    # - chat
    # - spacer
    # - battle-history
    # with the fourth being no class divs that are present at turn 0 stating the format and clauses,mods
    # Take out all chat from the replays  i.e. joins/leaves/timers/chat messages
    chat_pattern = re.compile(r'<div class="chat.*?">.*?</div>')
    text = chat_pattern.sub('', text)
    # Replace all of the spacer divs with a newline
    text = text.replace('<div class="spacer battle-history"><br></div>', '\n')
    # Find strong tags
    strong_pattern = re.compile(r'<strong.*?>(.*?)</strong>')
    text = strong_pattern.sub(lambda match: f'**{match.group(1)}**', text)
    # clear out any small tags
    text = text.replace('<small>', '').replace('</small>', '')
    # Now separate the battle-history and no class divs
    battle_start_index = text.find('<div class="battle-history">')
    format_text = text[:battle_start_index]
    text = text[battle_start_index:]
    # Find em tags
    em_pattern = re.compile(r'<em.*?>(.*?)</em>')
    text = em_pattern.sub(lambda match: f'_{match.group(1)}_', text)
    format_text = em_pattern.sub(lambda match: f'- {match.group(1)}', format_text)
    # Replace all of the divs with a newline
    div_pattern = re.compile(r'<div.*?>(.*?)</div>')
    text = div_pattern.sub(lambda match: f'{match.group(1)}\n', text)
    format_text = div_pattern.sub(lambda match: f'{match.group(1)}\n', format_text)
    # replace br tags where it doesn't end at a newline with newline
    br_pattern = re.compile(r'<br>(?!\n)')
    text = br_pattern.sub('\n', text)
    # now clear out any remaining br tags
    text = text.replace('<br>', '')
    format_text = format_text.replace('<br>', '')
    # Replace all of the abbr in battle text with percentages
    abbr_pattern = re.compile(r'<abbr.*?>(.*?)</abbr>')
    text = abbr_pattern.sub(lambda match: f'{match.group(1)}', text)
    # finally lets parse out the h2 tags for turn text
    h2_pattern = re.compile(r'<h2.*?>(.*?)</h2>')
    # ^-^  random encoding to split on
    text = h2_pattern.sub(lambda match: f'^-^ ## ```{match.group(1)}```', text)

    # now separate the text into a list of turns, while keeping the turn text
    text = [t.strip() for t in text.split('^-^ ')]
    # don't forget turn 0
    text[0] = "## ```Turn 0```\n" + text[0]
    # some formatting
    format_text = format_text.strip()
    return format_text, text


class Replay(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def save_replay_to_db(self, url: str, format_text, text1, text2):
        formatted_url = '.com/'.join(url.split('.com/')[1:])  # scraps out https://replay.pokemonshowdown.com/
        query = "INSERT INTO psreplays (replayid, format_text, battle_text1, battle_text2) VALUES (?, ?, ?, ?)"
        async with self.bot.pool.acquire() as conn:
            # yet another random encoding to split on
            params = (formatted_url, format_text, "\n-\n-\n".join(text1), "\n-\n-\n".join(text2))
            await conn.execute(query, params)
            await conn.commit()

    async def get_replay_from_db(self, url: str):
        """
        psreplays table
        replayid  format_text  battle_text1  battle_text2  sc_img  gen_img
        """
        formatted_url = '.com/'.join(url.split('.com/')[1:])  # scraps out https://replay.pokemonshowdown.com/
        # get from database, or return None
        query = "SELECT format_text, battle_text1, battle_text2 FROM psreplays WHERE replayid = ?"
        async with self.bot.pool.acquire() as conn:
            async with conn.execute(query, (formatted_url,)) as cursor:
                row = await cursor.fetchone()
                return row

    @taskcache(ttl=100)
    async def fetch_replay(self, url: str):
        page = await self.bot.browser.new_page()
        try:
            await page.goto(url=url)
            skip = page.get_by_role("button", name="Skip to end")
            await skip.click()
            battle_log = page.get_by_role("log")
            html_log = await battle_log.inner_html()
            viewpoint = page.get_by_role("button", name="viewpoint")
            await viewpoint.click()
            battle_log2 = page.get_by_role("log")
            html_log2 = await battle_log2.inner_html()
        except PlaywrightTimeoutError:
            await page.close()
            return PlaywrightTimeoutError
        except Exception as e:
            print(e)
            await page.close()
            return
        await page.close()
        return html_battle_parser(html_log), html_battle_parser(html_log2)

    @commands.hybrid_command()
    @slash.describe(
        url="Enter showdown replay link"
    )
    @slash.choices(
        theme=[
            slash.Choice(name="🔴 Normal (default)", value="normal"),
            slash.Choice(name="🟢 Compact", value="simple"),
            slash.Choice(name="🔵 Pixel", value="pixel")
        ]
    )
    async def replay(self, ctx, url: str, theme: str = "advanced"):
        """View ps replays onto discord!"""
        await ctx.defer()
        # The ps replay viewer should act as an archive of ps replays through snapshots
        # while also being able to give users on discord a lazier way to watch the replay

        # normal theme involves saving the replay visually as well and is resource expensive for pokearena
        # compact theme involves no image archive
        # pixel theme involves image archive but through pillow generated images for each turn
        if not url.startswith('https://replay.pokemonshowdown.com/'):
            return await ctx.send('Please enter a valid showdown replay link.', ephemeral=True)
        if url.endswith(('.json', '.log')):
            url = url[:url.rfind('.')]
        try:
            async with self.bot.session.get(url + '.json') as response:
                if response.status != HTTPStatus.OK:
                    return await ctx.send('I could not access the url..', ephemeral=True)
                battle_data = await response.json()
                p1, p2 = battle_data["players"]
                battle_format = battle_data["format"]
                views = battle_data["views"]
                upload_time = discord.utils.format_dt(datetime.fromtimestamp(int(battle_data["uploadtime"])))
        except aiohttp.InvalidURL:
            return await ctx.send('Please provide a valid replay link.', ephemeral=True)
        except Exception as e:
            # todo: customised error for replays with invalid password
            print(e)
            return await ctx.send('An error occurred, please ensure the provided url is valid.', ephemeral=True)

        battle_texts = await self.get_replay_from_db(url)
        if battle_texts is not None:
            format_text, turn_texts, turn_texts2 = battle_texts
            turn_texts = turn_texts.split("\n-\n-\n")
            turn_texts2 = turn_texts2.split("\n-\n-\n")
        else:
            m = await ctx.send('<a:loading_blue:1222017888769151018> Please wait while tyranitar saves the replay..')
            res = await self.fetch_replay(url)
            if res == PlaywrightTimeoutError:
                return await ctx.send('Replay website timed out! Please redo the command.')
            elif not res:
                return await ctx.send('An error occurred, I was unable to open the replay site properly. Please report it to Intenzi.')
            else:
                format_text, turn_texts = res[0]
                format_text2, turn_texts2 = res[1]
                await self.save_replay_to_db(url, format_text, turn_texts, turn_texts2)

        # Parse the html file
        winner = p1 if p1 == turn_texts[-1].splitlines()[-1].replace('**', '')[:-16] else p2
        # Initial embed
        emb = discord.Embed(color=discord.Color.pink(), description=format_text, url=url)
        emb.title = f"{p1} vs. {p2}"
        # rating
        if battle_data.get('rating', 0):
            emb.add_field(name="Rating", value=battle_data["rating"])
        emb.add_field(name="Views", value=views)
        emb.add_field(name="Uploaded", value=upload_time)
        emb.add_field(name="Winner", value=f"||{winner}||")

        emb_2 = discord.Embed(color=discord.Color.gold())
        emb_2.description = turn_texts[0]
        ReplayViewerView.set_emb_img(emb_2.description, emb_2)

        # TODO: Add team count to p1 and p2 team
        # TODO: Add pokemon emotes and pokeball emotes
        # TODO: Add team count to p1 and p2 team

        view = ReplayViewerView(ctx.author.id, turn_texts, turn_texts2, [], format_text, battle_format, theme, p1, p2)
        await ctx.send(embeds=[emb, emb_2], view=view)
        if battle_texts is None:
            await m.delete()


async def setup(bot):
    await bot.add_cog(Replay(bot))
    print("Replay Cog loaded")
