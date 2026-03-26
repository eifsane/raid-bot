import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import tasks

# =========================
# CONFIG
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: speeds up slash command registration

# Время рейдов всегда вводится и хранится в МСК
MOSCOW_TZ = timezone(timedelta(hours=3))

# Локальное время для показа в скобках.
# Пример для Греции: зимой 2, летом 3.
LOCAL_TIME_OFFSET = int(os.getenv("LOCAL_TIME_OFFSET", "2"))
LOCAL_TZ = timezone(timedelta(hours=LOCAL_TIME_OFFSET))

DATA_FILE = Path("raids.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("raid-bot")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing in environment variables")

# =========================
# DISCORD SETUP
# =========================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = False

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# =========================
# RAID DATA
# =========================
RAID_TEMPLATES = {
    5: {
        "tank": 1,
        "heal": 1,
        "dps": 3,
        "reserve": 3,
    },
    10: {
        "tank": 2,
        "heal": 2,
        "dps": 6,
        "reserve": 5,
    },
}

ROLE_LABELS = {
    "tank": "Танки",
    "heal": "Хилы",
    "dps": "ДД",
    "reserve": "Резерв",
}

CLASS_CHOICES = {
    "tank": "Танк",
    "heal": "Хил",
    "dps": "ДД",
}


@dataclass
class Signup:
    user_id: int
    display_name: str
    role: str
    joined_at: str


class RaidStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, dict] = {}
        self.load()

    def load(self):
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                self.data = json.load(f)
        else:
            self.data = {}

    def save(self):
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def create_raid(self, raid: dict):
        self.data[str(raid["message_id"])] = raid
        self.save()

    def get(self, message_id: int) -> Optional[dict]:
        return self.data.get(str(message_id))

    def update(self, message_id: int, raid: dict):
        self.data[str(message_id)] = raid
        self.save()

    def delete(self, message_id: int):
        self.data.pop(str(message_id), None)
        self.save()

    def all(self) -> List[dict]:
        return list(self.data.values())


store = RaidStore(DATA_FILE)


# =========================
# HELPERS
# =========================
def now_moscow() -> datetime:
    return datetime.now(MOSCOW_TZ)


def format_dt(dt_str: str) -> str:
    dt = datetime.fromisoformat(dt_str).astimezone(MOSCOW_TZ)
    return dt.strftime("%d.%m.%Y %H:%M")


def format_dt_with_local(dt_str: str) -> str:
    msk_dt = datetime.fromisoformat(dt_str).astimezone(MOSCOW_TZ)
    local_dt = msk_dt.astimezone(LOCAL_TZ)

    if LOCAL_TIME_OFFSET == 3:
        return f"{msk_dt.strftime('%d.%m.%Y %H:%M')} МСК"

    return (
        f"{msk_dt.strftime('%d.%m.%Y %H:%M')} МСК "
        f"({local_dt.strftime('%d.%m.%Y %H:%M')} локальное)"
    )


def parse_date_time(date_str: str, time_str: str) -> datetime:
    # Expected: 26.03.2026 and 20:30
    dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
    return dt.replace(tzinfo=MOSCOW_TZ)


def mention_list(signups: List[dict]) -> str:
    if not signups:
        return "никого"
    return " ".join(f"<@{entry['user_id']}>" for entry in signups)


def sort_signups(signups: List[dict]) -> Dict[str, List[dict]]:
    grouped = {"tank": [], "heal": [], "dps": [], "reserve": []}
    for s in signups:
        grouped[s["role"]].append(s)
    return grouped


def raid_embed(raid: dict) -> discord.Embed:
    config = RAID_TEMPLATES[raid["size"]]
    grouped = sort_signups(raid["signups"])

    embed = discord.Embed(
        title=f"⚔️ {raid['title']}",
        description=(
            f"**Дата и время:** {format_dt_with_local(raid['raid_datetime'])}\n"
            f"**Формат:** {raid['size']} человек\n"
            f"**Создал:** <@{raid['creator_id']}>\n\n"
            f"Нажми кнопку ниже, чтобы записаться."
        ),
    )

    for role in ["tank", "heal", "dps"]:
        current = grouped[role]
        cap = config[role]
        lines = []
        for idx, user in enumerate(current[:cap], start=1):
            lines.append(f"{idx}. <@{user['user_id']}> — {CLASS_CHOICES[role]}")
        if not lines:
            lines = ["—"]
        embed.add_field(
            name=f"{ROLE_LABELS[role]} [{len(current[:cap])}/{cap}]",
            value="\n".join(lines),
            inline=True,
        )

    reserve_lines = []
    reserve_cap = config["reserve"]
    for idx, user in enumerate(grouped["reserve"][:reserve_cap], start=1):
        reserve_lines.append(f"{idx}. <@{user['user_id']}>")
    if not reserve_lines:
        reserve_lines = ["—"]

    embed.add_field(
        name=f"{ROLE_LABELS['reserve']} [{len(grouped['reserve'][:reserve_cap])}/{reserve_cap}]",
        value="\n".join(reserve_lines),
        inline=False,
    )

    embed.set_footer(text="Если роль занята, бот отправит тебя в резерв.")
    return embed


async def update_raid_message(message_id: int):
    raid = store.get(message_id)
    if not raid:
        return

    channel = client.get_channel(raid["channel_id"])
    if not channel:
        channel = await client.fetch_channel(raid["channel_id"])

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        store.delete(message_id)
        return

    view = RaidSignupView(message_id)
    await message.edit(embed=raid_embed(raid), view=view)


async def create_discussion_thread(message: discord.Message, raid: dict):
    try:
        thread = await message.create_thread(
            name=f"Обсуждение: {raid['title']}",
            auto_archive_duration=1440,
        )
        await thread.send(
            f"Тема для обсуждения рейда **{raid['title']}**. Здесь можно писать детали, сбор, замены и т.д."
        )
        raid["thread_id"] = thread.id
        store.update(message.id, raid)
    except discord.Forbidden:
        logger.warning("No permission to create thread for message %s", message.id)
    except Exception as e:
        logger.exception("Failed to create thread: %s", e)


# =========================
# VIEWS / MODALS
# =========================
class RaidCreateModal(discord.ui.Modal, title="Создать рейд"):
    raid_title = discord.ui.TextInput(label="Название рейда", placeholder="Например: Замок Бури")
    raid_date = discord.ui.TextInput(label="Дата", placeholder="Например: 26.03.2026")
    raid_time = discord.ui.TextInput(label="Время (МСК)", placeholder="Например: 20:30")

    def __init__(self, raid_size: int):
        super().__init__()
        self.raid_size = raid_size

    async def on_submit(self, interaction: discord.Interaction):
        try:
            raid_dt = parse_date_time(str(self.raid_date), str(self.raid_time))
        except ValueError:
            await interaction.response.send_message(
                "Неверный формат. Дата должна быть **ДД.ММ.ГГГГ**, время — **ЧЧ:ММ**.",
                ephemeral=True,
            )
            return

        if raid_dt <= now_moscow():
            await interaction.response.send_message(
                "Дата/время рейда должны быть в будущем.", ephemeral=True
            )
            return

        raid = {
            "title": str(self.raid_title),
            "size": self.raid_size,
            "raid_datetime": raid_dt.isoformat(),
            "creator_id": interaction.user.id,
            "guild_id": interaction.guild_id,
            "channel_id": interaction.channel_id,
            "thread_id": None,
            "message_id": None,
            "signups": [],
            "notified_1h": False,
            "notified_start": False,
        }

        embed = raid_embed(raid)
        view = RaidSignupView(0)

        await interaction.response.send_message("Рейд создаю...", ephemeral=True)
        msg = await interaction.channel.send(embed=embed, view=view)
        raid["message_id"] = msg.id
        store.create_raid(raid)

        # Rebind the view with the real message id
        await msg.edit(view=RaidSignupView(msg.id))
        await create_discussion_thread(msg, raid)

        await interaction.followup.send(
            f"Готово: рейд **{raid['title']}** создан.", ephemeral=True
        )


class RaidSizeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Рейд на 5", style=discord.ButtonStyle.primary)
    async def five(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RaidCreateModal(5))

    @discord.ui.button(label="Рейд на 10", style=discord.ButtonStyle.success)
    async def ten(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RaidCreateModal(10))


class RoleSelect(discord.ui.Select):
    def __init__(self, message_id: int):
        self.message_id = message_id
        options = [
            discord.SelectOption(label="Танк", value="tank", emoji="🛡️"),
            discord.SelectOption(label="Хил", value="heal", emoji="💚"),
            discord.SelectOption(label="ДД", value="dps", emoji="⚔️"),
            discord.SelectOption(label="Резерв", value="reserve", emoji="🪑"),
        ]
        super().__init__(
            placeholder="Выбери свою роль",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        raid = store.get(self.message_id)
        if not raid:
            await interaction.response.send_message("Рейд не найден.", ephemeral=True)
            return

        chosen = self.values[0]
        config = RAID_TEMPLATES[raid["size"]]

        signups = raid["signups"]
        existing = next((s for s in signups if s["user_id"] == interaction.user.id), None)
        if existing:
            signups.remove(existing)

        if chosen == "reserve":
            final_role = "reserve"
        else:
            occupied = len([s for s in signups if s["role"] == chosen])
            if occupied < config[chosen]:
                final_role = chosen
            else:
                final_role = "reserve"

        signups.append(
            asdict(
                Signup(
                    user_id=interaction.user.id,
                    display_name=interaction.user.display_name,
                    role=final_role,
                    joined_at=now_moscow().isoformat(),
                )
            )
        )

        # Keep a nice order: tanks, heals, dps, reserve
        role_order = {"tank": 0, "heal": 1, "dps": 2, "reserve": 3}
        signups.sort(key=lambda x: (role_order[x["role"]], x["joined_at"]))

        raid["signups"] = signups
        store.update(self.message_id, raid)
        await update_raid_message(self.message_id)

        if final_role == "reserve" and chosen != "reserve":
            text = "Основной слот этой роли уже занят, ты записан(а) в **резерв**."
        else:
            text = f"Ты записан(а) как **{CLASS_CHOICES.get(final_role, 'Резерв')}**."

        await interaction.response.send_message(text, ephemeral=True)


class RaidSignupView(discord.ui.View):
    def __init__(self, message_id: int):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.add_item(RoleSelect(message_id))

    @discord.ui.button(label="Отписаться", style=discord.ButtonStyle.danger, custom_id="raid_unsubscribe")
    async def unsubscribe(self, interaction: discord.Interaction, button: discord.ui.Button):
        raid = store.get(self.message_id)
        if not raid:
            await interaction.response.send_message("Рейд не найден.", ephemeral=True)
            return

        original_count = len(raid["signups"])
        raid["signups"] = [s for s in raid["signups"] if s["user_id"] != interaction.user.id]

        if len(raid["signups"]) == original_count:
            await interaction.response.send_message("Ты не был(а) записан(а) на этот рейд.", ephemeral=True)
            return

        # promote reserve if slots opened
        config = RAID_TEMPLATES[raid["size"]]
        for role in ["tank", "heal", "dps"]:
            current = [s for s in raid["signups"] if s["role"] == role]
            reserves = [s for s in raid["signups"] if s["role"] == "reserve"]
            while len(current) < config[role] and reserves:
                promoted = reserves.pop(0)
                promoted["role"] = role
                current.append(promoted)

        # rebuild ordered list after promotions
        role_order = {"tank": 0, "heal": 1, "dps": 2, "reserve": 3}
        raid["signups"].sort(key=lambda x: (role_order[x["role"]], x["joined_at"]))

        store.update(self.message_id, raid)
        await update_raid_message(self.message_id)
        await interaction.response.send_message("Ты отписался(ась) от рейда.", ephemeral=True)


# =========================
# COMMANDS
# =========================
@tree.command(name="raid_create", description="Создать новый рейд")
async def raid_create(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Выбери формат рейда:", view=RaidSizeView(), ephemeral=True
    )


@tree.command(name="raid_list", description="Показать все активные рейды")
async def raid_list(interaction: discord.Interaction):
    raids = []
    now = now_moscow()
    for raid in store.all():
        dt = datetime.fromisoformat(raid["raid_datetime"])
        if dt >= now:
            raids.append(raid)

    if not raids:
        await interaction.response.send_message("Активных рейдов нет.", ephemeral=True)
        return

    raids.sort(key=lambda r: r["raid_datetime"])
    lines = [
        f"• **{r['title']}** — {format_dt_with_local(r['raid_datetime'])}, формат {r['size']}, [сообщение](https://discord.com/channels/{r['guild_id']}/{r['channel_id']}/{r['message_id']})"
        for r in raids
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="raid_delete", description="Удалить рейд (только создатель)")
@app_commands.describe(message_id="ID сообщения рейда")
async def raid_delete(interaction: discord.Interaction, message_id: str):
    try:
        mid = int(message_id)
    except ValueError:
        await interaction.response.send_message("message_id должен быть числом.", ephemeral=True)
        return

    raid = store.get(mid)
    if not raid:
        await interaction.response.send_message("Рейд не найден.", ephemeral=True)
        return

    if raid["creator_id"] != interaction.user.id and not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("Удалить этот рейд может только создатель или модератор.", ephemeral=True)
        return

    channel = client.get_channel(raid["channel_id"]) or await client.fetch_channel(raid["channel_id"])
    try:
        msg = await channel.fetch_message(mid)
        await msg.delete()
    except Exception:
        pass

    store.delete(mid)
    await interaction.response.send_message("Рейд удалён.", ephemeral=True)


# =========================
# NOTIFICATIONS
# =========================
@tasks.loop(minutes=1)
async def raid_notifier():
    now = now_moscow()
    for raid in store.all():
        raid_dt = datetime.fromisoformat(raid["raid_datetime"])
        channel = client.get_channel(raid["channel_id"])
        if channel is None:
            try:
                channel = await client.fetch_channel(raid["channel_id"])
            except Exception:
                continue

        signups = [s for s in raid["signups"] if s["role"] != "reserve"]
        mentions = mention_list(signups)

        if not raid["notified_1h"] and timedelta(minutes=0) <= (raid_dt - now) <= timedelta(minutes=60):
            await channel.send(
                f"⏰ {mentions}\nЧерез **час** начинается рейд **{raid['title']}** в **{format_dt_with_local(raid['raid_datetime'])}**."
            )
            raid["notified_1h"] = True
            store.update(raid["message_id"], raid)

        if not raid["notified_start"] and timedelta(minutes=-1) <= (raid_dt - now) <= timedelta(minutes=1):
            await channel.send(
                f"🚨 {mentions}\nРейд **{raid['title']}** начинается **сейчас**! Время рейда: **{format_dt_with_local(raid['raid_datetime'])}**."
            )
            raid["notified_start"] = True
            store.update(raid["message_id"], raid)

        if raid_dt < now - timedelta(hours=12):
            store.delete(raid["message_id"])


@raid_notifier.before_loop
async def before_notifier():
    await client.wait_until_ready()


# =========================
# EVENTS
# =========================
@client.event
async def on_ready():
    logger.info("Logged in as %s (%s)", client.user, client.user.id)

    # Persistent views for existing raids after restart
    for raid in store.all():
        client.add_view(RaidSignupView(raid["message_id"]))

    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        logger.info("Commands synced to guild %s", GUILD_ID)
    else:
        await tree.sync()
        logger.info("Global commands synced")

    if not raid_notifier.is_running():
        raid_notifier.start()


client.run(TOKEN)
