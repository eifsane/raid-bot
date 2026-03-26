import os
import json
import datetime
import discord
from discord.ext import commands
from discord import app_commands

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_RAW = os.getenv("GUILD_ID")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN не найден в Railway Variables.")
if not GUILD_ID_RAW:
    raise RuntimeError("GUILD_ID не найден в Railway Variables.")

try:
    GUILD_ID = int(GUILD_ID_RAW.strip())
except ValueError:
    raise RuntimeError("GUILD_ID должен быть числом.")

GUILD = discord.Object(id=GUILD_ID)
DATA_FILE = "raids.json"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

ROLE_LIMITS = {
    5: {"tank": 1, "heal": 1, "dps": 3, "reserve": 3},
    10: {"tank": 2, "heal": 2, "dps": 6, "reserve": 5},
}

ROLE_LABELS = {
    "tank": "Танки",
    "heal": "Хилы",
    "dps": "ДД",
    "reserve": "Резерв",
}

def load_raids():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_raids(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def parse_msk_datetime(date_str: str, time_str: str) -> datetime.datetime:
    day, month, year = map(int, date_str.split("-"))
    hour, minute = map(int, time_str.split(":"))
    msk = datetime.timezone(datetime.timedelta(hours=3))
    return datetime.datetime(year, month, day, hour, minute, tzinfo=msk)

def ts_for_discord(date_str: str, time_str: str) -> str:
    dt = parse_msk_datetime(date_str, time_str)
    return f"<t:{int(dt.timestamp())}:F>"

def mention_text(user_ids):
    return "\n".join(f"<@{uid}>" for uid in user_ids) if user_ids else "—"

def make_embed(raid_id: str, raid: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"Рейд: {raid['title']}",
        color=discord.Color.blurple()
    )
    embed.add_field(name="Формат", value=f"{raid['size']} человек", inline=True)
    embed.add_field(name="Дата", value=raid["date"], inline=True)
    embed.add_field(
        name="Время",
        value=f"{raid['time']} МСК\n{ts_for_discord(raid['date'], raid['time'])}",
        inline=False
    )

    for role in ["tank", "heal", "dps", "reserve"]:
        users = raid["signups"][role]
        embed.add_field(
            name=f"{ROLE_LABELS[role]} ({len(users)}/{ROLE_LIMITS[raid['size']][role]})",
            value=mention_text(users),
            inline=False
        )

    embed.set_footer(text=f"ID: {raid_id}")
    return embed

class RaidView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def update_raid_message(self, raid_id: str):
        raids = load_raids()
        raid = raids.get(raid_id)
        if not raid:
            return

        channel = bot.get_channel(int(raid["channel_id"]))
        if channel is None:
            channel = await bot.fetch_channel(int(raid["channel_id"]))

        message = await channel.fetch_message(int(raid["message_id"]))
        await message.edit(embed=make_embed(raid_id, raid), view=self)

    async def join_role(self, interaction: discord.Interaction, role: str):
        if not interaction.message or not interaction.message.embeds:
            await interaction.response.send_message("Не удалось найти рейд.", ephemeral=True)
            return

        footer = interaction.message.embeds[0].footer.text or ""
        if not footer.startswith("ID: "):
            await interaction.response.send_message("Не удалось найти ID рейда.", ephemeral=True)
            return

        raid_id = footer.replace("ID: ", "").strip()
        raids = load_raids()
        raid = raids.get(raid_id)

        if not raid:
            await interaction.response.send_message("Рейд не найден.", ephemeral=True)
            return

        user_id = interaction.user.id

        for r in ["tank", "heal", "dps", "reserve"]:
            if user_id in raid["signups"][r]:
                raid["signups"][r].remove(user_id)

        if role == "leave":
            text = "Ты отписался(ась) от рейда."
        else:
            limit = ROLE_LIMITS[raid["size"]][role]
            if len(raid["signups"][role]) < limit:
                raid["signups"][role].append(user_id)
                text = f"Ты записан(а) в {ROLE_LABELS[role]}."
            else:
                reserve_limit = ROLE_LIMITS[raid["size"]]["reserve"]
                if len(raid["signups"]["reserve"]) < reserve_limit:
                    raid["signups"]["reserve"].append(user_id)
                    text = "Основное место занято, ты добавлен(а) в резерв."
                else:
                    text = "Нет мест: основной состав и резерв заполнены."

        save_raids(raids)
        await self.update_raid_message(raid_id)
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="Танк", style=discord.ButtonStyle.primary, custom_id="raid_tank")
    async def tank_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.join_role(interaction, "tank")

    @discord.ui.button(label="Хил", style=discord.ButtonStyle.success, custom_id="raid_heal")
    async def heal_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.join_role(interaction, "heal")

    @discord.ui.button(label="ДД", style=discord.ButtonStyle.secondary, custom_id="raid_dps")
    async def dps_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.join_role(interaction, "dps")

    @discord.ui.button(label="Отписаться", style=discord.ButtonStyle.danger, custom_id="raid_leave")
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.join_role(interaction, "leave")

class RaidModal(discord.ui.Modal, title="Создать рейд"):
    raid_title = discord.ui.TextInput(label="Название рейда", max_length=100)
    raid_date = discord.ui.TextInput(label="Дата (ДД-ММ-ГГГГ)", placeholder="27-03-2026", max_length=10)
    raid_time = discord.ui.TextInput(label="Время по МСК (ЧЧ:ММ)", placeholder="18:00", max_length=5)

    def __init__(self, size: int):
        super().__init__()
        self.size = size

    async def on_submit(self, interaction: discord.Interaction):
        try:
            parse_msk_datetime(str(self.raid_date), str(self.raid_time))
        except Exception:
            await interaction.response.send_message(
                "Проверь дату и время. Формат даты: ДД-ММ-ГГГГ, время: ЧЧ:ММ",
                ephemeral=True
            )
            return

        raid_id = str(int(datetime.datetime.now().timestamp() * 1000))
        raids = load_raids()

        raids[raid_id] = {
            "id": raid_id,
            "title": str(self.raid_title),
            "size": self.size,
            "date": str(self.raid_date),
            "time": str(self.raid_time),
            "channel_id": str(interaction.channel_id),
            "message_id": "",
            "thread_id": "",
            "signups": {"tank": [], "heal": [], "dps": [], "reserve": []},
        }

        view = RaidView()
        message = await interaction.channel.send(embed=make_embed(raid_id, raids[raid_id]), view=view)
        raids[raid_id]["message_id"] = str(message.id)

        try:
            thread = await message.create_thread(name=f"Обсуждение: {raids[raid_id]['title']}")
            raids[raid_id]["thread_id"] = str(thread.id)
        except Exception as e:
            print(f"Не удалось создать ветку: {e}")

        save_raids(raids)
        await interaction.response.send_message("Рейд создан.", ephemeral=True)

class RaidSizeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Рейд на 5", style=discord.ButtonStyle.primary)
    async def five_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RaidModal(5))

    @discord.ui.button(label="Рейд на 10", style=discord.ButtonStyle.success)
    async def ten_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RaidModal(10))

class RaidBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(RaidView())
        # Очень простая и надежная синхронизация только в один сервер
        synced = await self.tree.sync(guild=GUILD)
        print(f"✅ Synced {len(synced)} command(s) to guild {GUILD_ID}")
        for cmd in synced:
            print(f"   - /{cmd.name}")

bot = RaidBot(command_prefix="!", intents=intents)

@bot.tree.command(name="raid_create", description="Создать рейд", guild=GUILD)
async def raid_create(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Выбери формат рейда:",
        view=RaidSizeView(),
        ephemeral=True
    )

@bot.event
async def on_ready():
    print(f"🤖 Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"🏠 Guild ID: {GUILD_ID}")

bot.run(DISCORD_TOKEN)
