import discord
from discord import app_commands
from discord.ext import tasks, commands
import asyncio
import random
from datetime import datetime
import pytz
import os
import json

# ==========================================
# CONFIGURATION
# ==========================================
TOKEN = os.environ.get('DISCORD_TOKEN', '')

SETTINGS_FILE = 'settings.json'
DEFAULT_SETTINGS = {
    'CHANNEL_ID': 0,
    'MAX_SLOTS': 10,
    'START_MINUTE': 25,
    'DRAW_MINUTE': 35,
    'END_MINUTE': 40,
    'PRIORITY_ROLE_ID': None,
    'VC_REMIND_MINUTE': None,
    'MONITOR_VC_ID': None,
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                data = json.load(f)
                for key, val in DEFAULT_SETTINGS.items():
                    if key not in data:
                        data[key] = val
                return data
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings():
    data = {
        'CHANNEL_ID': CHANNEL_ID,
        'MAX_SLOTS': MAX_SLOTS,
        'START_MINUTE': START_MINUTE,
        'DRAW_MINUTE': DRAW_MINUTE,
        'END_MINUTE': END_MINUTE,
        'PRIORITY_ROLE_ID': PRIORITY_ROLE_ID,
        'VC_REMIND_MINUTE': VC_REMIND_MINUTE,
        'MONITOR_VC_ID': MONITOR_VC_ID,
    }
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

_s = load_settings()
CHANNEL_ID        = _s['CHANNEL_ID']
MAX_SLOTS         = _s['MAX_SLOTS']
START_MINUTE      = _s['START_MINUTE']
DRAW_MINUTE       = _s['DRAW_MINUTE']
END_MINUTE        = _s['END_MINUTE']
PRIORITY_ROLE_ID  = _s['PRIORITY_ROLE_ID']
VC_REMIND_MINUTE  = _s['VC_REMIND_MINUTE']
MONITOR_VC_ID     = _s['MONITOR_VC_ID']

BLACKLIST_USERS = set()
BAN_USERS = set()

# Timezone (Croatia = Europe/Zagreb)
TIMEZONE = pytz.timezone('Europe/Zagreb')

# ==========================================
# INTERNAL STATE
# ==========================================
last_winner_id = None
winner_history = []
current_participants = []
participant_names = {}  # uid -> display_name, populated when user joins
event_active = False
join_button_locked = False
current_event_message = None
inf_bot_online = None

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# ==========================================
# HELPERS
# ==========================================
def _is_admin(guild: discord.Guild, user_id: int) -> bool:
    if guild is None:
        return False
    member = guild.get_member(user_id)
    return bool(member and member.guild_permissions.administrator)

async def _disable_event_message():
    """Disable the join/leave buttons on the current event message."""
    global current_event_message
    if current_event_message:
        try:
            disabled_view = discord.ui.View()
            btn_join = discord.ui.Button(label="🔘 Udi na listu", style=discord.ButtonStyle.success, disabled=True)
            btn_leave = discord.ui.Button(label="🚪 Izađi s liste", style=discord.ButtonStyle.danger, disabled=True)
            disabled_view.add_item(btn_join)
            disabled_view.add_item(btn_leave)
            await current_event_message.edit(view=disabled_view)
        except Exception as e:
            print(f"⚠️ _disable_event_message: {e}")
        current_event_message = None

# ==========================================
# UI: BUTTON + EMBED
# ==========================================
def build_embed():
    if not current_participants:
        participant_text = "🎯 *No one has joined yet*"
    else:
        channel = bot.get_channel(CHANNEL_ID)
        guild = channel.guild if channel else None
        vc_member_ids = set()
        if MONITOR_VC_ID and guild:
            vc_channel = guild.get_channel(MONITOR_VC_ID)
            if vc_channel and isinstance(vc_channel, discord.VoiceChannel):
                vc_member_ids = {m.id for m in vc_channel.members[:40]}

        lines = []
        for idx, uid in enumerate(current_participants[:MAX_SLOTS], start=1):
            name = participant_names.get(uid)
            if name is None:
                member = guild.get_member(uid) if guild else None
                name = member.display_name if member else f"<@{uid}>"
                if name:
                    participant_names[uid] = name
            member = guild.get_member(uid) if guild else None
            has_priority = PRIORITY_ROLE_ID and member and any(r.id == PRIORITY_ROLE_ID for r in member.roles)
            star = "⭐ " if has_priority else ""
            if MONITOR_VC_ID:
                lamp = "🟢" if uid in vc_member_ids else "🔴"
                lines.append(f"{idx}. {lamp} {star}{name}")
            else:
                lines.append(f"{idx}. {star}{name}")
        participant_text = "\n".join(lines)

    status = "🔓 OPEN" if not join_button_locked else "🔒 LOCKED"
    embed = discord.Embed(
        title="🚛 inf lista",
        description=(
            f"**⏰ Duration:** :{str(START_MINUTE).zfill(2)} — :{str(END_MINUTE).zfill(2)}\n"
            f"**👥 First {MAX_SLOTS} are on the list, priority roles have advantage**\n"
            f"**🏆 Prize:** Random winner drives the Ammo Car\n"
            f"**📊 Status:** {status}\n\n"
            f"**Participants ({len(current_participants)}/{MAX_SLOTS}):**\n"
            f"{participant_text}\n\n"
            f"*Izvlačenje u :{str(DRAW_MINUTE).zfill(2)}, lista se zatvara u :{str(END_MINUTE).zfill(2)}*"
        ),
        color=0xFF5500
    )
    embed.set_footer(text="Click the button below to enter!")
    return embed


# ==========================================
# SETUP WIZARD: MODALS + VIEW
# ==========================================
class SetupModal(discord.ui.Modal, title="⚙️ INF Bot — Konfiguracija"):
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.kanal_id = discord.ui.TextInput(
            label="Kanal ID (desni klik na kanal → Kopiraj ID)",
            placeholder="npr. 123456789012345678",
            default=str(CHANNEL_ID) if CHANNEL_ID != 0 else "",
            required=False,
            max_length=25,
        )
        self.vremena = discord.ui.TextInput(
            label="Start i kraj (dvije minute, razmak između)",
            placeholder="npr. 25 40",
            default=f"{START_MINUTE} {END_MINUTE}",
            required=False,
            max_length=10,
        )
        self.izvlacenje = discord.ui.TextInput(
            label="Izvlačenje (minuta između starta i kraja)",
            placeholder="npr. 35",
            default=str(DRAW_MINUTE),
            required=False,
            max_length=3,
        )
        self.slotovi = discord.ui.TextInput(
            label="Max slotova (1–100)",
            placeholder="npr. 10",
            default=str(MAX_SLOTS),
            required=False,
            max_length=3,
        )
        self.vc_remind = discord.ui.TextInput(
            label="VC podsjetnik (minuta) — prazno = isključeno",
            placeholder="npr. 32 — ili ostavi prazno za isključiti",
            default=str(VC_REMIND_MINUTE) if VC_REMIND_MINUTE is not None else "",
            required=False,
            max_length=3,
        )
        self.add_item(self.kanal_id)
        self.add_item(self.vremena)
        self.add_item(self.izvlacenje)
        self.add_item(self.slotovi)
        self.add_item(self.vc_remind)

    async def on_submit(self, interaction: discord.Interaction):
        global CHANNEL_ID, START_MINUTE, END_MINUTE, DRAW_MINUTE, MAX_SLOTS, VC_REMIND_MINUTE

        guild = bot.get_guild(self.guild_id)
        errors = []

        new_channel_id = None
        raw_ch = self.kanal_id.value.strip()
        if raw_ch:
            try:
                cid = int(raw_ch)
                ch = guild.get_channel(cid) if guild else None
                if ch is None:
                    errors.append("❌ Kanal s tim ID-om nije pronađen.")
                else:
                    new_channel_id = cid
            except ValueError:
                errors.append("❌ Kanal ID mora biti broj.")

        new_start = new_end = None
        raw_vr = self.vremena.value.strip()
        if raw_vr:
            parts = raw_vr.split()
            if len(parts) != 2:
                errors.append("❌ Vremena: upiši dva broja razdvojena razmakom (npr. `25 40`).")
            else:
                try:
                    s, e = int(parts[0]), int(parts[1])
                    if not (0 <= s <= 59 and 0 <= e <= 59):
                        errors.append("❌ Vremena: minute moraju biti 0–59.")
                    elif s == e:
                        errors.append("❌ Vremena: start i kraj ne mogu biti isti.")
                    elif event_active:
                        errors.append("⚠️ Vremena: ne možeš mijenjati dok event traje (`/force_end` prvo).")
                    else:
                        new_start, new_end = s, e
                except ValueError:
                    errors.append("❌ Vremena: upiši dva broja (npr. `25 40`).")

        eff_start = new_start if new_start is not None else START_MINUTE
        eff_end   = new_end   if new_end   is not None else END_MINUTE

        new_draw = None
        raw_dr = self.izvlacenje.value.strip()
        if raw_dr:
            try:
                d = int(raw_dr)
                if not (0 <= d <= 59):
                    errors.append("❌ Izvlačenje: minuta mora biti 0–59.")
                elif d <= eff_start or d >= eff_end:
                    errors.append(f"❌ Izvlačenje mora biti između :{str(eff_start).zfill(2)} i :{str(eff_end).zfill(2)}.")
                elif event_active:
                    errors.append("⚠️ Izvlačenje: ne možeš mijenjati dok event traje.")
                else:
                    new_draw = d
            except ValueError:
                errors.append("❌ Izvlačenje: upiši broj (npr. `35`).")

        new_slots = None
        raw_sl = self.slotovi.value.strip()
        if raw_sl:
            try:
                sl = int(raw_sl)
                if not (1 <= sl <= 100):
                    errors.append("❌ Slotovi: broj mora biti 1–100.")
                elif event_active:
                    errors.append("⚠️ Slotovi: ne možeš mijenjati dok event traje.")
                else:
                    new_slots = sl
            except ValueError:
                errors.append("❌ Slotovi: upiši broj (npr. `10`).")

        new_vc: int | None = -1  # -1 = not changed; None = disable; int = new value
        raw_vc = self.vc_remind.value.strip()
        if raw_vc:
            try:
                vr = int(raw_vc)
                if not (0 <= vr <= 59):
                    errors.append("❌ VC podsjetnik: minuta mora biti 0–59.")
                else:
                    new_vc = vr
            except ValueError:
                errors.append("❌ VC podsjetnik: upiši broj ili ostavi prazno.")
        else:
            new_vc = None  # explicit clear

        if errors:
            await interaction.response.send_message(
                "⚠️ **Greške — ništa nije spremljeno:**\n" + "\n".join(errors),
                ephemeral=True,
            )
            return

        applied = []

        if new_channel_id is not None:
            CHANNEL_ID = new_channel_id
            applied.append(f"📡 Kanal: <#{CHANNEL_ID}>")

        if new_start is not None:
            START_MINUTE, END_MINUTE = new_start, new_end
            applied.append(f"⏰ Start/kraj: :{str(new_start).zfill(2)} → :{str(new_end).zfill(2)}")

        if new_draw is not None:
            DRAW_MINUTE = new_draw
            applied.append(f"🎲 Izvlačenje: :{str(new_draw).zfill(2)}")

        if new_slots is not None:
            MAX_SLOTS = new_slots
            applied.append(f"👥 Max slotova: {new_slots}")

        if new_vc != -1:
            if new_vc is None and VC_REMIND_MINUTE is not None:
                VC_REMIND_MINUTE = None
                applied.append("🎙️ VC podsjetnik: isključen")
            elif new_vc is not None:
                VC_REMIND_MINUTE = new_vc
                applied.append(f"🎙️ VC podsjetnik: :{str(new_vc).zfill(2)}")

        if applied:
            save_settings()

        await interaction.response.send_message(
            ("✅ **Primijenjeno:**\n" + "\n".join(applied)) if applied else "Nije promijenjeno ništa.",
            ephemeral=True,
        )


class MonitorVCModal(discord.ui.Modal, title="🎙️ VC Lampice — Praćeni Kanal"):
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.vc_id = discord.ui.TextInput(
            label="Voice kanal ID — prazno = isključi lampice",
            placeholder="Desni klik na voice kanal → Kopiraj ID — prazno za isključiti",
            default=str(MONITOR_VC_ID) if MONITOR_VC_ID else "",
            required=False,
            max_length=25,
        )
        self.add_item(self.vc_id)

    async def on_submit(self, interaction: discord.Interaction):
        global MONITOR_VC_ID
        guild = bot.get_guild(self.guild_id)
        raw = self.vc_id.value.strip()
        if not raw:
            MONITOR_VC_ID = None
            save_settings()
            await interaction.response.send_message("✅ VC lampice isključene.", ephemeral=True)
            return
        try:
            vid = int(raw)
            vc = guild.get_channel(vid) if guild else None
            if vc is None or not isinstance(vc, discord.VoiceChannel):
                await interaction.response.send_message(
                    "❌ Voice kanal s tim ID-om nije pronađen.\n"
                    "Desni klik na voice kanal u listi kanala → Kopiraj ID.",
                    ephemeral=True,
                )
                return
            MONITOR_VC_ID = vid
            save_settings()
            await interaction.response.send_message(
                f"✅ VC lampice postavljene na: **{vc.name}**\n🟢 = u kanalu  🔴 = nije u kanalu",
                ephemeral=True,
            )
        except ValueError:
            await interaction.response.send_message(
                "❌ ID mora biti broj. Desni klik na voice kanal → Kopiraj ID.", ephemeral=True
            )


class PriorityRoleModal(discord.ui.Modal, title="⭐ Priority Rola"):
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.role_id = discord.ui.TextInput(
            label="ID priority role — prazno = ukloni",
            placeholder="Desni klik na rolu → Kopiraj ID — prazno za ukloniti",
            default=str(PRIORITY_ROLE_ID) if PRIORITY_ROLE_ID else "",
            required=False,
            max_length=25,
        )
        self.add_item(self.role_id)

    async def on_submit(self, interaction: discord.Interaction):
        global PRIORITY_ROLE_ID
        guild = bot.get_guild(self.guild_id)
        raw = self.role_id.value.strip()
        if not raw:
            PRIORITY_ROLE_ID = None
            save_settings()
            await interaction.response.send_message("✅ Priority rola uklonjena.", ephemeral=True)
            return
        try:
            rid = int(raw)
            role = guild.get_role(rid) if guild else None
            if role is None:
                await interaction.response.send_message(
                    "❌ Rola s tim ID-om nije pronađena.\n"
                    "Desni klik na rolu u Server Settings → Kopiraj ID.",
                    ephemeral=True,
                )
                return
            PRIORITY_ROLE_ID = rid
            save_settings()
            await interaction.response.send_message(
                f"✅ Priority rola postavljena: **{role.name}**", ephemeral=True
            )
        except ValueError:
            await interaction.response.send_message(
                "❌ ID mora biti broj. Desni klik na rolu → Kopiraj ID.", ephemeral=True
            )


class SetupView(discord.ui.View):
    def __init__(self, guild_id: int, author_id: int):
        super().__init__(timeout=600)  # 10 minutes
        self.guild_id = guild_id
        self.author_id = author_id

    def _check_author(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="🔧 Postavi konfiguraciju", style=discord.ButtonStyle.primary)
    async def open_setup_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_author(interaction):
            await interaction.response.send_message("❌ Samo osoba koja je pokrenula /setup može koristiti ovu formu.", ephemeral=True)
            return
        await interaction.response.send_modal(SetupModal(self.guild_id))

    @discord.ui.button(label="⭐ Priority rola", style=discord.ButtonStyle.secondary)
    async def open_priority_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_author(interaction):
            await interaction.response.send_message("❌ Samo osoba koja je pokrenula /setup može koristiti ovu formu.", ephemeral=True)
            return
        await interaction.response.send_modal(PriorityRoleModal(self.guild_id))

    @discord.ui.button(label="🎙️ VC lampice", style=discord.ButtonStyle.secondary)
    async def open_monitor_vc_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_author(interaction):
            await interaction.response.send_message("❌ Samo osoba koja je pokrenula /setup može koristiti ovu formu.", ephemeral=True)
            return
        await interaction.response.send_modal(MonitorVCModal(self.guild_id))

    @discord.ui.select(
        placeholder="🔛 INF Bot status — odaberi...",
        options=[
            discord.SelectOption(label="✅ INF Bot ON", value="on", description="Bot šalje: INF bot uključen budite spremni."),
            discord.SelectOption(label="❌ INF Bot OFF", value="off", description="Bot šalje: Nažalost izgubili smo neformalnu..."),
        ]
    )
    async def inf_status_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        global inf_bot_online
        if not self._check_author(interaction):
            await interaction.response.send_message("❌ Samo osoba koja je pokrenula /setup može koristiti ovu formu.", ephemeral=True)
            return
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            await interaction.response.send_message(
                "❌ Event kanal nije postavljen. Postavi ga prvo u 🔧 konfiguraciji.", ephemeral=True
            )
            return
        if select.values[0] == "on":
            inf_bot_online = True
            await channel.send("INF bot uključen budite spremni.")
            await interaction.response.send_message("✅ INF Bot uključen.", ephemeral=True)
        else:
            inf_bot_online = False
            await channel.send("Nažalost izgubili smo neformalnu bot neradi dok ne dobijemo neformalnu nazad")
            await interaction.response.send_message("❌ INF Bot isključen.", ephemeral=True)


class JoinButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔘 Udi na listu", style=discord.ButtonStyle.success, custom_id="ammo_join")
    async def join_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        global current_participants, join_button_locked

        try:
            if not event_active:
                await interaction.response.send_message(f"❌ Event nije aktivan! Sljedeći kreće u :{str(START_MINUTE).zfill(2)}.", ephemeral=True)
                return

            if join_button_locked:
                await interaction.response.send_message("🔒 Lista je zaključana!", ephemeral=True)
                return

            if interaction.user.id in BAN_USERS:
                await interaction.response.send_message("🚫 Baniran/a si i ne možeš ući na listu.", ephemeral=True)
                return

            if interaction.user.id in current_participants:
                await interaction.response.send_message("⚠️ Već si na listi!", ephemeral=True)
                return

            guild = interaction.guild
            member = guild.get_member(interaction.user.id) if guild else None
            nick = member.display_name if member else interaction.user.display_name
            has_priority = bool(PRIORITY_ROLE_ID and member and any(r.id == PRIORITY_ROLE_ID for r in member.roles))

            if len(current_participants) >= MAX_SLOTS:
                if has_priority:
                    bumped_uid = None
                    for uid in reversed(current_participants):
                        m = guild.get_member(uid) if guild else None
                        if not m or not any(r.id == PRIORITY_ROLE_ID for r in m.roles):
                            bumped_uid = uid
                            break

                    if bumped_uid is None:
                        await interaction.response.send_message("❌ Lista je puna i svi imaju priority rol. Nema mjesta.", ephemeral=True)
                        return

                    current_participants.remove(bumped_uid)
                    current_participants.append(interaction.user.id)
                    participant_names[interaction.user.id] = nick
                    bumped_member = guild.get_member(bumped_uid) if guild else None
                    bumped_name = bumped_member.display_name if bumped_member else participant_names.get(bumped_uid, f"<@{bumped_uid}>")
                    await interaction.response.send_message(f"⭐ Ušao/la priority rolom! **{bumped_name}** je izbačen/a.", ephemeral=True)
                    await update_message()
                    ch = bot.get_channel(CHANNEL_ID)
                    if ch:
                        await ch.send(f"⭐ **{nick}** je ušao/la priority rolom i izbacio/la **{bumped_name}** s liste!")
                else:
                    await interaction.response.send_message(f"❌ Lista je puna ({MAX_SLOTS}/{MAX_SLOTS}). Pričekaj do :{str(END_MINUTE).zfill(2)}, možda neko izađe!", ephemeral=True)
                return

            current_participants.append(interaction.user.id)
            participant_names[interaction.user.id] = nick
            prefix = "⭐ " if has_priority else ""
            await interaction.response.send_message(f"✅ **{prefix}{nick}** na listi! ({len(current_participants)}/{MAX_SLOTS})", ephemeral=True)
            await update_message()

        except Exception as e:
            print(f"❌ Greška u join_callback: {e}")
            try:
                await interaction.response.send_message("❌ Došlo je do greške. Pokušaj ponovo.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="🚪 Izađi s liste", style=discord.ButtonStyle.danger, custom_id="ammo_leave")
    async def leave_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        global current_participants, join_button_locked

        try:
            if not event_active:
                await interaction.response.send_message("❌ Nema aktivnog eventa.", ephemeral=True)
                return

            if join_button_locked:
                await interaction.response.send_message("🔒 Lista je zaključana, ne možeš izaći.", ephemeral=True)
                return

            if interaction.user.id not in current_participants:
                await interaction.response.send_message("⚠️ Nisi na listi!", ephemeral=True)
                return

            current_participants.remove(interaction.user.id)
            guild = interaction.guild
            member = guild.get_member(interaction.user.id) if guild else None
            nick = member.display_name if member else interaction.user.display_name
            await interaction.response.send_message(f"✅ **{nick}** skinut/a s liste.", ephemeral=True)
            await update_message()

        except Exception as e:
            print(f"❌ Greška u leave_callback: {e}")
            try:
                await interaction.response.send_message("❌ Došlo je do greške. Pokušaj ponovo.", ephemeral=True)
            except Exception:
                pass


async def update_message():
    if current_event_message:
        embed = build_embed()
        view = JoinButtonView()
        await current_event_message.edit(embed=embed, view=view)


async def send_vc_reminders():
    """DM every current participant reminding them to join the INF VC."""
    if not current_participants:
        return 0, 0
    deadline = str(DRAW_MINUTE).zfill(2)
    sent = 0
    failed = 0
    for uid in list(current_participants):
        user = bot.get_user(uid)
        if user is None:
            try:
                user = await bot.fetch_user(uid)
            except Exception:
                failed += 1
                continue
        try:
            await user.send(
                f"🎙️ **INF podsjetnik** — moraš biti u **INF VC** do **:{deadline}** ili gubiš mjesto na listi! 🚛"
            )
            sent += 1
        except discord.Forbidden:
            failed += 1
        except Exception:
            failed += 1
    print(f"📨 VC reminders sent: {sent} ok, {failed} failed")
    return sent, failed


# ==========================================
# SCHEDULER: RUNS EVERY MINUTE
# ==========================================
@tasks.loop(seconds=5)
async def vc_status_refresh():
    """Refresh the embed every 5s while event is active so VC lampice stay current."""
    if event_active and MONITOR_VC_ID and current_event_message:
        await update_message()


@tasks.loop(minutes=1)
async def event_scheduler():
    global event_active, join_button_locked, current_participants, current_event_message

    now = datetime.now(TIMEZONE)
    minute = now.minute

    if CHANNEL_ID == 0:
        return

    # REMINDER 5 MINUTES BEFORE START
    reminder_minute = (START_MINUTE - 5) % 60
    if minute == reminder_minute and not event_active:
        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            await channel.send("⏳ **INF - lista pocinje za 5 minuta.**")

    # START AT CONFIGURED MINUTE
    if minute == START_MINUTE and not event_active:
        event_active = True
        join_button_locked = False
        current_participants = []
        participant_names.clear()

        channel = bot.get_channel(CHANNEL_ID)
        if not channel:
            print(f"❌ Channel {CHANNEL_ID} not found! Check ID and bot permissions.")
            return

        embed = build_embed()
        view = JoinButtonView()
        msg = await channel.send(embed=embed, view=view)
        current_event_message = msg
        await channel.send("@everyone 🚨 INF lista je pocela! Prvih 10 ulazi, bira se ko vozi AMMO CAR! 🚛")
        print(f"✅ Event started at {now.strftime('%H:%M')}")

    # VC REMINDER AT CONFIGURED MINUTE
    if VC_REMIND_MINUTE is not None and minute == VC_REMIND_MINUTE and event_active:
        await send_vc_reminders()

    # DRAW AT DRAW_MINUTE
    if minute == DRAW_MINUTE and event_active:
        channel = bot.get_channel(CHANNEL_ID)

        if len(current_participants) == 0:
            await channel.send("😢 **Nitko nije na listi. Ajmo se aktivirat malo.**")
        else:
            eligible = [uid for uid in current_participants if uid not in BLACKLIST_USERS]
            if not eligible:
                await channel.send("⚠️ **Nitko od prijavljenih nije prihvatljiv za izvlačenje.** Svi sudionici su na blacklisti.")
            else:
                global last_winner_id
                winner_id = random.choice(eligible)
                last_winner_id = winner_id
                winner_history.append({"id": winner_id, "time": datetime.now(TIMEZONE).strftime("%d.%m. %H:%M")})
                if len(winner_history) > 5:
                    winner_history.pop(0)
                winner = bot.get_user(winner_id)
                winner_mention = winner.mention if winner else f"<@{winner_id}>"
                await channel.send(f"🚗💨 **Ammo car vozi {winner_mention}!** 🚗💨")

        print(f"🎲 Draw done at {now.strftime('%H:%M')}")

    # LOCK & CLOSE EVENT AT END_MINUTE
    if minute == END_MINUTE and event_active:
        join_button_locked = True
        await update_message()

        channel = bot.get_channel(CHANNEL_ID)
        if channel and current_participants:
            guild = channel.guild if channel else None
            name_parts = []
            for uid in current_participants[:MAX_SLOTS]:
                name = participant_names.get(uid)
                if name is None:
                    member = guild.get_member(uid) if guild else None
                    name = member.display_name if member else f"<@{uid}>"
                member = guild.get_member(uid) if guild else None
                has_priority = PRIORITY_ROLE_ID and member and any(r.id == PRIORITY_ROLE_ID for r in member.roles)
                star = "⭐ " if has_priority else ""
                name_parts.append(f"{star}{name}")
            list_text = ", ".join(name_parts)
            await channel.send(f"**Lista je:**\n{list_text}")

        event_active = False
        join_button_locked = False
        current_participants = []
        participant_names.clear()

        await _disable_event_message()

        print(f"🏁 Event finished at {now.strftime('%H:%M')}")


# ==========================================
# SLASH COMMANDS — ADMIN
# ==========================================

# --- Global error handler for missing permissions ---
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ Samo admini mogu koristiti ovu komandu.", ephemeral=True)
    else:
        print(f"❌ Slash command error: {error}")
        try:
            await interaction.response.send_message("❌ Došlo je do greške.", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="setup", description="Otvara interaktivni setup wizard za konfiguraciju bota.")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    channel = bot.get_channel(CHANNEL_ID)
    channel_val = channel.mention if channel else "❌ Nije postavljen"

    if PRIORITY_ROLE_ID:
        priority_role = interaction.guild.get_role(PRIORITY_ROLE_ID)
        priority_val = priority_role.mention if priority_role else f"ID {PRIORITY_ROLE_ID} *(nije pronađen)*"
    else:
        priority_val = "*nije postavljen*"

    vc_val = f":{str(VC_REMIND_MINUTE).zfill(2)}" if VC_REMIND_MINUTE is not None else "*isključen*"

    if inf_bot_online is True:
        inf_val = "✅ Uključen"
    elif inf_bot_online is False:
        inf_val = "❌ Isključen"
    else:
        inf_val = "❓ Nije postavljeno"

    embed = discord.Embed(
        title="⚙️ INF Bot — Setup",
        description=(
            "**Trenutne postavke** prikazane su ispod.\n"
            "Klikni **🔧 Postavi konfiguraciju** da otvoriš formu — polja su već popunjena trenutnim vrijednostima, "
            "samo promijeni što trebaš i potvrdi.\n"
            "Za priority rolu klikni **⭐ Priority rola**.\n"
            "INF Bot status postavi dolje u izborniku."
        ),
        color=0xFF5500,
    )
    embed.add_field(name="📡 Kanal", value=channel_val, inline=True)
    embed.add_field(name="⏰ Start / Kraj", value=f":{str(START_MINUTE).zfill(2)} → :{str(END_MINUTE).zfill(2)}", inline=True)
    embed.add_field(name="🎲 Izvlačenje", value=f":{str(DRAW_MINUTE).zfill(2)}", inline=True)
    embed.add_field(name="👥 Max slotova", value=str(MAX_SLOTS), inline=True)
    embed.add_field(name="🎙️ VC podsjetnik", value=vc_val, inline=True)
    embed.add_field(name="⭐ Priority rola", value=priority_val, inline=True)
    embed.add_field(name="🔛 INF Bot", value=inf_val, inline=True)
    embed.add_field(name="🚫 Blacklista", value=f"{len(BLACKLIST_USERS)} korisnika", inline=True)
    embed.add_field(name="🔨 Banirani", value=f"{len(BAN_USERS)} korisnika", inline=True)
    embed.set_footer(text="Vidljivo samo tebi • /helpinf za sve komande")

    view = SetupView(guild_id=interaction.guild.id, author_id=interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="force_start", description="Ručno pokreće event odmah.")
@app_commands.default_permissions(administrator=True)
async def force_start(interaction: discord.Interaction):
    global event_active, join_button_locked, current_participants, current_event_message

    if event_active:
        await interaction.response.send_message("⚠️ Event već traje! Pričekaj kraj ili koristi /force_end.", ephemeral=True)
        return

    event_active = True
    join_button_locked = False
    current_participants = []
    participant_names.clear()

    embed = build_embed()
    view = JoinButtonView()
    msg = await interaction.channel.send(embed=embed, view=view)
    current_event_message = msg
    await interaction.channel.send(f"@everyone 🚨 **Inf lista je pocela imate do :{str(END_MINUTE).zfill(2)} da udete i pobjednik vozi ammo!**")

    await interaction.response.send_message("✅ Event pokrenut.", ephemeral=True)

    # Background: draw after 15 minutes
    async def _auto_end():
        global event_active, current_participants
        await asyncio.sleep(900)
        if not event_active:
            return
        join_button_locked_ref = True
        await update_message()
        ch = interaction.channel
        if len(current_participants) == 0:
            await ch.send("😢 No one joined. Event cancelled.")
        else:
            eligible = [uid for uid in current_participants if uid not in BLACKLIST_USERS]
            if not eligible:
                await ch.send("⚠️ **No eligible participants** — all are on the blacklist.")
            else:
                global last_winner_id
                winner_id = random.choice(eligible)
                last_winner_id = winner_id
                winner_history.append({"id": winner_id, "time": datetime.now(TIMEZONE).strftime("%d.%m. %H:%M")})
                if len(winner_history) > 5:
                    winner_history.pop(0)
                winner = bot.get_user(winner_id)
                winner_mention = winner.mention if winner else f"<@{winner_id}>"
                await ch.send(f"🎉 **WINNER:** {winner_mention} drives the Ammo Car! 🚛")
        event_active = False
        current_participants = []
        participant_names.clear()
        await _disable_event_message()

    asyncio.create_task(_auto_end())


@bot.tree.command(name="force_end", description="Zaustavlja trenutni event bez izvlačenja pobjednika.")
@app_commands.default_permissions(administrator=True)
async def force_end(interaction: discord.Interaction):
    global event_active, current_participants, join_button_locked

    if not event_active:
        await interaction.response.send_message("❌ Nema aktivnog eventa.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    event_active = False
    join_button_locked = False
    current_participants = []
    participant_names.clear()
    await _disable_event_message()

    await interaction.followup.send("⏹️ Event force-stopan.", ephemeral=True)


@bot.tree.command(name="ping", description="Provjeri radi li bot i latenciju.")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! Latencija: **{latency}ms**", ephemeral=True)


@bot.tree.command(name="remind", description="Ručno šalje podsjetnik u event kanal da lista uskoro počinje.")
@app_commands.default_permissions(administrator=True)
async def remind(interaction: discord.Interaction):
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("❌ Event kanal nije pronađen.", ephemeral=True)
        return
    await channel.send(f"⏳ **INF lista počinje za malo — :{str(START_MINUTE).zfill(2)}! Budite spremni! 🚛**")
    await interaction.response.send_message("✅ Podsjetnik poslan.", ephemeral=True)


@bot.tree.command(name="infon", description="Bot piše u kanal: INF bot uključen budite spremni.")
@app_commands.default_permissions(administrator=True)
async def infon(interaction: discord.Interaction):
    global inf_bot_online
    inf_bot_online = True
    channel = bot.get_channel(CHANNEL_ID)
    target = channel if channel else interaction.channel
    await target.send("INF bot uključen budite spremni.")
    await interaction.response.send_message("✅ Poruka poslana.", ephemeral=True)


@bot.tree.command(name="infof", description="Bot piše u kanal: Nažalost izgubili smo neformalnu...")
@app_commands.default_permissions(administrator=True)
async def infof(interaction: discord.Interaction):
    global inf_bot_online
    inf_bot_online = False
    channel = bot.get_channel(CHANNEL_ID)
    target = channel if channel else interaction.channel
    await target.send("Nažalost izgubili smo neformalnu bot neradi dok ne dobijemo neformalnu nazad")
    await interaction.response.send_message("✅ Poruka poslana.", ephemeral=True)


@bot.tree.command(name="infostatus", description="Prikazuje trenutni INF bot status u kanalu.")
@app_commands.default_permissions(administrator=True)
async def infostatus(interaction: discord.Interaction):
    channel = bot.get_channel(CHANNEL_ID)
    target = channel if channel else interaction.channel
    if inf_bot_online is True:
        await target.send("✅ **INF bot status:** Uključen — budite spremni.")
    elif inf_bot_online is False:
        await target.send("❌ **INF bot status:** Isključen — nema neformalne dok ne dobijemo nazad.")
    else:
        await target.send("❓ **INF bot status:** Status još nije postavljen. Koristi `/infon` ili `/infof`.")
    await interaction.response.send_message("✅ Status objavljen.", ephemeral=True)


@bot.tree.command(name="reroll", description="Bira novog pobjednika — lista ostaje ista.")
@app_commands.default_permissions(administrator=True)
async def reroll(interaction: discord.Interaction):
    global last_winner_id
    if len(current_participants) == 0:
        await interaction.response.send_message("😢 **Lista je prazna. Nema koga birati!**", ephemeral=True)
        return

    eligible = [uid for uid in current_participants if uid not in BLACKLIST_USERS]
    if not eligible:
        await interaction.response.send_message("⚠️ **Nitko nije prihvatljiv za reroll.** Svi su na blacklisti.", ephemeral=True)
        return

    winner_id = random.choice(eligible)
    last_winner_id = winner_id
    winner_history.append({"id": winner_id, "time": datetime.now(TIMEZONE).strftime("%d.%m. %H:%M")})
    if len(winner_history) > 5:
        winner_history.pop(0)
    winner = bot.get_user(winner_id)
    winner_mention = winner.mention if winner else f"<@{winner_id}>"
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        await channel.send(f"🔁 **REROLL!** Novi vozač Ammo Cara je... {winner_mention} 🎉🚗💨")
    await interaction.response.send_message(f"✅ Reroll izvršen — pobjednik: {winner_mention}", ephemeral=True)


@bot.tree.command(name="add", description="Dodaj korisnika na listu dok je event aktivan.")
@app_commands.describe(member="Korisnik kojeg dodaješ na listu")
async def add_to_list(interaction: discord.Interaction, member: discord.Member):
    global current_participants, join_button_locked

    if not event_active:
        await interaction.response.send_message("❌ Nema aktivnog eventa.", ephemeral=True)
        return

    if join_button_locked:
        await interaction.response.send_message("🔒 Lista je zaključana.", ephemeral=True)
        return

    if member.id in BAN_USERS:
        await interaction.response.send_message(f"🚫 **{member.display_name}** je baniran/a i ne može ući na listu.", ephemeral=True)
        return

    if member.id in current_participants:
        await interaction.response.send_message(f"⚠️ **{member.display_name}** već je na listi.", ephemeral=True)
        return

    guild = interaction.guild
    m = guild.get_member(member.id) if guild else None
    has_priority = bool(PRIORITY_ROLE_ID and m and any(r.id == PRIORITY_ROLE_ID for r in m.roles))

    if len(current_participants) >= MAX_SLOTS:
        if has_priority:
            bumped_uid = None
            for uid in reversed(current_participants):
                bm = guild.get_member(uid) if guild else None
                if not bm or not any(r.id == PRIORITY_ROLE_ID for r in bm.roles):
                    bumped_uid = uid
                    break

            if bumped_uid is None:
                await interaction.response.send_message("❌ Lista je puna i svi imaju priority rol. Nema mjesta.", ephemeral=True)
                return

            current_participants.remove(bumped_uid)
            current_participants.append(member.id)
            participant_names[member.id] = member.display_name
            bumped_member = guild.get_member(bumped_uid) if guild else None
            bumped_name = bumped_member.display_name if bumped_member else participant_names.get(bumped_uid, f"<@{bumped_uid}>")
            await interaction.response.send_message(f"⭐ **{member.display_name}** dodan priority rolom! **{bumped_name}** je izbačen/a.", ephemeral=True)
            await update_message()
        else:
            await interaction.response.send_message(f"❌ Lista je puna ({MAX_SLOTS}/{MAX_SLOTS}).", ephemeral=True)
        return

    current_participants.append(member.id)
    participant_names[member.id] = member.display_name
    prefix = "⭐ " if has_priority else ""
    await interaction.response.send_message(f"✅ **{prefix}{member.display_name}** dodan/a na listu! ({len(current_participants)}/{MAX_SLOTS})", ephemeral=True)
    await update_message()


@bot.tree.command(name="kick_from_list", description="Makni korisnika s liste dok je event aktivan.")
@app_commands.describe(member="Korisnik kojeg makneš s liste")
@app_commands.default_permissions(administrator=True)
async def kick_from_list(interaction: discord.Interaction, member: discord.Member):
    if not event_active:
        await interaction.response.send_message("❌ Nema aktivnog eventa.", ephemeral=True)
        return

    if member.id not in current_participants:
        await interaction.response.send_message(f"⚠️ **{member.display_name}** nije na listi.", ephemeral=True)
        return

    current_participants.remove(member.id)
    await interaction.response.send_message(f"✅ **{member.display_name}** je maknut/a s liste.", ephemeral=True)
    await update_message()


@bot.tree.command(name="ban", description="Zabranjuje korisniku ulaz na listu.")
@app_commands.describe(member="Korisnik kojeg baniš")
@app_commands.default_permissions(administrator=True)
async def ban_user(interaction: discord.Interaction, member: discord.Member):
    if member.id in BAN_USERS:
        await interaction.response.send_message(f"⚠️ **{member.display_name}** već je baniran/a.", ephemeral=True)
        return

    BAN_USERS.add(member.id)
    if member.id in current_participants:
        current_participants.remove(member.id)
        await update_message()
        await interaction.response.send_message(f"🔨 **{member.display_name}** je baniran/a i maknut/a s liste.", ephemeral=True)
    else:
        await interaction.response.send_message(f"🔨 **{member.display_name}** je baniran/a — ne može ući na listu.", ephemeral=True)


@bot.tree.command(name="unban", description="Uklanja ban — korisnik može ponovo ući na listu.")
@app_commands.describe(member="Korisnik kojem skidaš ban")
@app_commands.default_permissions(administrator=True)
async def unban_user(interaction: discord.Interaction, member: discord.Member):
    if member.id not in BAN_USERS:
        await interaction.response.send_message(f"⚠️ **{member.display_name}** nije baniran/a.", ephemeral=True)
        return

    BAN_USERS.discard(member.id)
    await interaction.response.send_message(f"✅ **{member.display_name}** je unbaniran/a — može ponovo ući na listu.", ephemeral=True)


@bot.tree.command(name="banlist", description="Prikaži sve trenutno banirane korisnike.")
@app_commands.default_permissions(administrator=True)
async def banlist(interaction: discord.Interaction):
    if not BAN_USERS:
        await interaction.response.send_message("✅ Nema banirani korisnika.", ephemeral=True)
        return

    guild = interaction.guild
    lines = []
    for uid in BAN_USERS:
        member = guild.get_member(uid) if guild else None
        name = member.display_name if member else f"<@{uid}> *(nije na serveru)*"
        lines.append(f"• {name} (`{uid}`)")

    embed = discord.Embed(
        title="🔨 Banirani korisnici",
        description="\n".join(lines),
        color=0x880000
    )
    embed.set_footer(text=f"{len(BAN_USERS)} korisnik(a) je baniran/a.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="set_channel", description="Postavi kanal za event.")
@app_commands.describe(channel="Kanal u koji bot šalje event listu")
@app_commands.default_permissions(administrator=True)
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    global CHANNEL_ID

    if event_active:
        await interaction.response.send_message("⚠️ Ne možeš mijenjati kanal dok event traje. Koristi `/force_end` prvo.", ephemeral=True)
        return

    old_id = CHANNEL_ID
    CHANNEL_ID = channel.id
    save_settings()
    old_channel = bot.get_channel(old_id)
    old_mention = old_channel.mention if old_channel else "*(nije bio postavljen)*"
    await interaction.response.send_message(
        f"✅ Event kanal postavljen: {old_mention} → {channel.mention}\n✅ Snimljeno trajno — ostaje i nakon restarta.",
        ephemeral=True,
    )


@bot.tree.command(name="set_slots", description="Mijenja max broj mjesta na listi.")
@app_commands.describe(number="Broj mjesta (1–100)")
@app_commands.default_permissions(administrator=True)
async def set_slots(interaction: discord.Interaction, number: int):
    global MAX_SLOTS

    if number < 1 or number > 100:
        await interaction.response.send_message("❌ Broj mora biti između 1 i 100.", ephemeral=True)
        return

    if event_active:
        await interaction.response.send_message("⚠️ Ne možeš mijenjati slotove dok event traje. Koristi `/force_end` prvo.", ephemeral=True)
        return

    old = MAX_SLOTS
    MAX_SLOTS = number
    save_settings()
    await interaction.response.send_message(f"✅ Max slotova: **{old}** → **{MAX_SLOTS}**", ephemeral=True)


@bot.tree.command(name="set_priority_role", description="Postavi priority rolu — ti korisnici izbacuju zadnjeg bez role kad je lista puna.")
@app_commands.describe(role="Rola s prioritetom")
@app_commands.default_permissions(administrator=True)
async def set_priority_role(interaction: discord.Interaction, role: discord.Role):
    global PRIORITY_ROLE_ID
    PRIORITY_ROLE_ID = role.id
    save_settings()
    await interaction.response.send_message(
        f"✅ Priority rol postavljen na **{role.name}**!\n"
        f"Kad je lista puna, korisnici s ovim rolom izbacuju zadnjeg bez njega. ⭐",
        ephemeral=True,
    )


@bot.tree.command(name="clear_priority_role", description="Uklanja priority rolu — svi su ravnopravni.")
@app_commands.default_permissions(administrator=True)
async def clear_priority_role(interaction: discord.Interaction):
    global PRIORITY_ROLE_ID
    if not PRIORITY_ROLE_ID:
        await interaction.response.send_message("ℹ️ Priority rol već nije postavljen.", ephemeral=True)
        return
    PRIORITY_ROLE_ID = None
    save_settings()
    await interaction.response.send_message("✅ Priority rol uklonjen. Svi su ravnopravni.", ephemeral=True)


@bot.tree.command(name="blacklist_user", description="Korisnik može ući na listu ali neće biti biran za Ammo Car.")
@app_commands.describe(member="Korisnik kojeg stavljaš na blacklistu")
@app_commands.default_permissions(administrator=True)
async def blacklist_user(interaction: discord.Interaction, member: discord.Member):
    if member.id in BLACKLIST_USERS:
        await interaction.response.send_message(f"⚠️ **{member.display_name}** već je na blacklisti.", ephemeral=True)
        return

    BLACKLIST_USERS.add(member.id)
    await interaction.response.send_message(
        f"🚫 **{member.display_name}** dodan/a na blacklistu.\n"
        f"Može se prijaviti na listu, ali neće biti biran/a za Ammo Car.",
        ephemeral=True,
    )


@bot.tree.command(name="unblacklist_user", description="Uklanja korisnika s blackliste.")
@app_commands.describe(member="Korisnik kojeg skidaš s blackliste")
@app_commands.default_permissions(administrator=True)
async def unblacklist_user(interaction: discord.Interaction, member: discord.Member):
    if member.id not in BLACKLIST_USERS:
        await interaction.response.send_message(f"⚠️ **{member.display_name}** nije na blacklisti.", ephemeral=True)
        return

    BLACKLIST_USERS.discard(member.id)
    await interaction.response.send_message(f"✅ **{member.display_name}** uklonjen/a s blackliste. Može biti biran/a za Ammo Car.", ephemeral=True)


@bot.tree.command(name="blacklist_list", description="Prikaži sve korisnike na blacklisti.")
@app_commands.default_permissions(administrator=True)
async def blacklist_list(interaction: discord.Interaction):
    if not BLACKLIST_USERS:
        await interaction.response.send_message("✅ Blacklista je prazna — svi sudionici su prihvatljivi za izvlačenje.", ephemeral=True)
        return

    guild = interaction.guild
    lines = []
    for uid in BLACKLIST_USERS:
        member = guild.get_member(uid) if guild else None
        name = member.display_name if member else f"<@{uid}> *(nije na serveru)*"
        lines.append(f"• {name} (`{uid}`)")

    embed = discord.Embed(
        title="🚫 Blacklista — Ammo Car",
        description="\n".join(lines),
        color=0xAA0000
    )
    embed.set_footer(text=f"{len(BLACKLIST_USERS)} korisnik(a) na blacklisti.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="set_monitor_vc", description="Prati koji su učesnici u VC-u — lampice 🟢/🔴 pored imena.")
@app_commands.describe(channel="Voice kanal za praćenje")
@app_commands.default_permissions(administrator=True)
async def set_monitor_vc(interaction: discord.Interaction, channel: discord.VoiceChannel):
    global MONITOR_VC_ID
    MONITOR_VC_ID = channel.id
    save_settings()
    await interaction.response.send_message(
        f"✅ VC praćenje postavljeno na: **{channel.name}**\nLampice 🟢/🔴 pored imena pokazuju tko je u tom VC-u.",
        ephemeral=True,
    )


@bot.tree.command(name="set_monitor_vc_off", description="Isključuje VC lampice s liste.")
@app_commands.default_permissions(administrator=True)
async def set_monitor_vc_off(interaction: discord.Interaction):
    global MONITOR_VC_ID
    if MONITOR_VC_ID is None:
        await interaction.response.send_message("ℹ️ VC praćenje je već isključeno.", ephemeral=True)
        return
    MONITOR_VC_ID = None
    save_settings()
    await interaction.response.send_message("✅ VC praćenje isključeno — lampice više neće biti prikazane.", ephemeral=True)


@bot.tree.command(name="set_vc_remind", description="Automatski DM svima na listi da moraju biti u INF VC.")
@app_commands.describe(minute="Minuta u kojoj se šalje DM (0–59)")
@app_commands.default_permissions(administrator=True)
async def set_vc_remind(interaction: discord.Interaction, minute: int):
    global VC_REMIND_MINUTE

    if not (0 <= minute <= 59):
        await interaction.response.send_message("❌ Minuta mora biti između 0 i 59.", ephemeral=True)
        return

    old = VC_REMIND_MINUTE
    VC_REMIND_MINUTE = minute
    save_settings()
    old_str = f":{str(old).zfill(2)}" if old is not None else "*(isključeno)*"
    await interaction.response.send_message(
        f"✅ VC podsjetnik postavljen: {old_str} → :{str(VC_REMIND_MINUTE).zfill(2)}\n"
        f"Bot će u :{str(VC_REMIND_MINUTE).zfill(2)} poslati DM svima na listi da moraju biti u INF VC do :{str(DRAW_MINUTE).zfill(2)}.",
        ephemeral=True,
    )


@bot.tree.command(name="set_vc_remind_off", description="Isključuje automatski VC podsjetnik.")
@app_commands.default_permissions(administrator=True)
async def set_vc_remind_off(interaction: discord.Interaction):
    global VC_REMIND_MINUTE
    if VC_REMIND_MINUTE is None:
        await interaction.response.send_message("ℹ️ VC podsjetnik već je isključen.", ephemeral=True)
        return
    VC_REMIND_MINUTE = None
    save_settings()
    await interaction.response.send_message("✅ VC podsjetnik isključen.", ephemeral=True)


@bot.tree.command(name="vc_remind", description="Odmah šalje DM podsjetnik svima na listi.")
@app_commands.default_permissions(administrator=True)
async def vc_remind_now(interaction: discord.Interaction):
    if not event_active:
        await interaction.response.send_message("❌ Nema aktivnog eventa.", ephemeral=True)
        return
    if not current_participants:
        await interaction.response.send_message("😢 Nitko nije na listi.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    sent, failed = await send_vc_reminders()
    await interaction.followup.send(
        f"✅ Podsjetnik poslan: **{sent}** primilo, **{failed}** nije moglo primiti DM.",
        ephemeral=True,
    )


@bot.tree.command(name="set_time", description="Mijenja minute starta i kraja eventa.")
@app_commands.describe(start="Minuta starta (0–59)", end="Minuta kraja (0–59)")
@app_commands.default_permissions(administrator=True)
async def set_time(interaction: discord.Interaction, start: int, end: int):
    global START_MINUTE, END_MINUTE

    if not (0 <= start <= 59) or not (0 <= end <= 59):
        await interaction.response.send_message("❌ Minuta mora biti između 0 i 59.", ephemeral=True)
        return

    if start == end:
        await interaction.response.send_message("❌ Start i end ne mogu biti isti.", ephemeral=True)
        return

    if event_active:
        await interaction.response.send_message("⚠️ Ne možeš mijenjati vrijeme dok event traje. Koristi `/force_end` prvo.", ephemeral=True)
        return

    old_start, old_end = START_MINUTE, END_MINUTE
    START_MINUTE = start
    END_MINUTE = end
    save_settings()
    await interaction.response.send_message(
        f"✅ Vrijeme updateano!\n"
        f"**Start:** :{str(old_start).zfill(2)} → :{str(START_MINUTE).zfill(2)}\n"
        f"**End:** :{str(old_end).zfill(2)} → :{str(END_MINUTE).zfill(2)}\n"
        f"Svaki sat bot šalje u :{str(START_MINUTE).zfill(2)} i zaključava u :{str(END_MINUTE).zfill(2)}.",
        ephemeral=True,
    )


@bot.tree.command(name="set_draw", description="Mijenja minutu izvlačenja pobjednika.")
@app_commands.describe(minute="Minuta izvlačenja — mora biti između starta i kraja")
@app_commands.default_permissions(administrator=True)
async def set_draw(interaction: discord.Interaction, minute: int):
    global DRAW_MINUTE

    if not (0 <= minute <= 59):
        await interaction.response.send_message("❌ Minuta mora biti između 0 i 59.", ephemeral=True)
        return

    if minute >= END_MINUTE:
        await interaction.response.send_message(f"❌ Minuta izvlačenja mora biti prije kraja (:{str(END_MINUTE).zfill(2)}). Odaberi manju minutu.", ephemeral=True)
        return

    if minute <= START_MINUTE:
        await interaction.response.send_message(f"❌ Minuta izvlačenja mora biti nakon starta (:{str(START_MINUTE).zfill(2)}). Odaberi veću minutu.", ephemeral=True)
        return

    if event_active:
        await interaction.response.send_message("⚠️ Ne možeš mijenjati vrijeme izvlačenja dok event traje. Koristi `/force_end` prvo.", ephemeral=True)
        return

    old = DRAW_MINUTE
    DRAW_MINUTE = minute
    save_settings()
    await interaction.response.send_message(
        f"✅ Minuta izvlačenja updateana: :{str(old).zfill(2)} → :{str(DRAW_MINUTE).zfill(2)}\n"
        f"Raspored: start :{str(START_MINUTE).zfill(2)} → izvlačenje :{str(DRAW_MINUTE).zfill(2)} → kraj :{str(END_MINUTE).zfill(2)}",
        ephemeral=True,
    )


@bot.tree.command(name="winner", description="Ponovo objavljuje zadnjeg pobjednika u event kanalu.")
@app_commands.default_permissions(administrator=True)
async def winner_cmd(interaction: discord.Interaction):
    if last_winner_id is None:
        await interaction.response.send_message("❌ Nema zabilježenog pobjednika od kad je bot pokrenut.", ephemeral=True)
        return
    winner = bot.get_user(last_winner_id)
    winner_mention = winner.mention if winner else f"<@{last_winner_id}>"
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("❌ Event kanal nije pronađen.", ephemeral=True)
        return
    await channel.send(f"🏆 **Podsjetnik — zadnji pobjednik Ammo Cara:** {winner_mention} 🚗💨")
    await interaction.response.send_message(f"✅ Pobjednik ponovo objavljen: {winner_mention}", ephemeral=True)


@bot.tree.command(name="clearwinner", description="Resetira zabilježenog pobjednika.")
@app_commands.default_permissions(administrator=True)
async def clearwinner(interaction: discord.Interaction):
    global last_winner_id
    if last_winner_id is None:
        await interaction.response.send_message("ℹ️ Nema zabilježenog pobjednika — već je čisto.", ephemeral=True)
        return
    last_winner_id = None
    await interaction.response.send_message("✅ Zadnji pobjednik resetiran.", ephemeral=True)


@bot.tree.command(name="history", description="Prikazuje zadnjih 5 pobjednika.")
@app_commands.default_permissions(administrator=True)
async def history(interaction: discord.Interaction):
    if not winner_history:
        await interaction.response.send_message("ℹ️ Nema zabilježenih pobjednika od kad je bot pokrenut.", ephemeral=True)
        return
    embed = discord.Embed(title="🏆 Zadnjih 5 pobjednika", color=0xFF5500)
    lines = []
    for i, entry in enumerate(reversed(winner_history), 1):
        user = bot.get_user(entry["id"])
        name = user.display_name if user else f"ID {entry['id']}"
        lines.append(f"`{i}.` **{name}** — {entry['time']}")
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Pokazuje stanje eventa — koliko je ljudi ušlo i kada kreće sljedeći.")
@app_commands.default_permissions(administrator=True)
async def status(interaction: discord.Interaction):
    now = datetime.now(TIMEZONE)
    minute = now.minute

    if not event_active:
        mins_until = (START_MINUTE - minute) % 60
        desc = (
            f"**📭 No event running**\n"
            f"Next auto-start in **{mins_until} minute(s)** (at :{str(START_MINUTE).zfill(2)})\n\n"
            f"Use `/force_start` to start one now."
        )
        color = 0x888888
    else:
        mins_until_lock = (END_MINUTE - minute) % 60
        mins_until_draw = (DRAW_MINUTE - minute) % 60
        lock_status = "🔒 Locked" if join_button_locked else f"🔓 Open — closes in **{mins_until_lock} min**"

        if current_participants:
            names = []
            for i, uid in enumerate(current_participants, start=1):
                user = bot.get_user(uid)
                name = user.display_name if user else f"<@{uid}>"
                names.append(f"{i}. {name}")
            participant_list = "\n".join(names)
        else:
            participant_list = "*No one yet*"

        draw_info = f"Draw at :{str(DRAW_MINUTE).zfill(2)} (in **{mins_until_draw} min**)" if not join_button_locked else f"Draw already done (:{str(DRAW_MINUTE).zfill(2)})"
        desc = (
            f"**🚛 Event is ACTIVE**\n"
            f"**Join window:** {lock_status}\n"
            f"**🎲 Draw:** {draw_info}\n"
            f"**Participants:** {len(current_participants)}/{MAX_SLOTS}\n\n"
            f"{participant_list}"
        )
        color = 0xFF5500 if not join_button_locked else 0xAA2200

    embed = discord.Embed(title="📊 Ammo Car Event Status", description=desc, color=color)
    embed.set_footer(text=f"Checked at {now.strftime('%H:%M')} ({TIMEZONE})")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="helpinf", description="Prikazuje sve admin komande.")
@app_commands.default_permissions(administrator=True)
async def help_command(interaction: discord.Interaction):
    priority_status = "nije postavljen"
    if PRIORITY_ROLE_ID:
        r = interaction.guild.get_role(PRIORITY_ROLE_ID)
        priority_status = r.name if r else f"ID {PRIORITY_ROLE_ID}"

    vc_remind_val = f":{str(VC_REMIND_MINUTE).zfill(2)}" if VC_REMIND_MINUTE is not None else "*(isključeno)*"

    embed1 = discord.Embed(title="📋 Inf Lista — Admin Komande (1/2)", color=0xFF5500)
    embed1.add_field(name="/setup", value="Interaktivni wizard — postavi kanal, vremena, slotove, VC podsjetnik i priority rolu kroz formu.", inline=False)
    embed1.add_field(name="/force_start", value="Ručno pokreće event odmah.", inline=False)
    embed1.add_field(name="/force_end", value="Zaustavlja trenutni event bez izvlačenja pobjednika.", inline=False)
    embed1.add_field(name="/reroll", value="Bira novog pobjednika — lista ostaje ista.", inline=False)
    embed1.add_field(name="/winner", value="Ponovo objavljuje zadnjeg pobjednika u event kanalu.", inline=False)
    embed1.add_field(name="/clearwinner", value="Resetira zabilježenog pobjednika.", inline=False)
    embed1.add_field(name="/history", value="Prikazuje zadnjih 5 pobjednika s vremenima.", inline=False)
    embed1.add_field(name="/remind", value="Ručno šalje podsjetnik u event kanal da lista uskoro počinje.", inline=False)
    embed1.add_field(name="/infon", value="Bot piše u kanal: **INF bot uključen budite spremni.**", inline=False)
    embed1.add_field(name="/infof", value="Bot piše u kanal: **Nažalost izgubili smo neformalnu...**", inline=False)
    embed1.add_field(name="/infostatus", value="Prikazuje trenutni status bota u kanalu.", inline=False)
    embed1.add_field(name="/ping", value="Provjeri radi li bot i latenciju. *(svi mogu koristiti)*", inline=False)
    embed1.add_field(name="/status", value="Pokazuje stanje eventa — koliko je ljudi ušlo i kada kreće sljedeći.", inline=False)
    embed1.add_field(name="/add @korisnik", value="Dodaj korisnika na listu dok je event aktivan.", inline=False)
    embed1.add_field(name="/kick_from_list @korisnik", value="Makni korisnika s liste dok je event aktivan.", inline=False)
    embed1.set_footer(text="Nastavak u sljedećoj poruci →")

    embed2 = discord.Embed(title="📋 Inf Lista — Admin Komande (2/2)", color=0xFF5500)
    embed2.add_field(name="/ban @korisnik", value="Zabranjuje korisniku ulaz na listu.", inline=False)
    embed2.add_field(name="/unban @korisnik", value="Uklanja ban — korisnik može ponovo ući na listu.", inline=False)
    embed2.add_field(name="/banlist", value="Prikaži sve trenutno banirane korisnike.", inline=False)
    embed2.add_field(name="/set_time start end", value=f"Mijenja minute starta i kraja. Trenutno: :{str(START_MINUTE).zfill(2)} → :{str(END_MINUTE).zfill(2)}", inline=False)
    embed2.add_field(name="/set_draw minute", value=f"Mijenja minutu izvlačenja. Trenutno: :{str(DRAW_MINUTE).zfill(2)}", inline=False)
    embed2.add_field(name="/set_slots number", value=f"Mijenja max broj mjesta. Trenutno: {MAX_SLOTS}", inline=False)
    embed2.add_field(name="/set_channel #kanal", value="Mijenja kanal za event.", inline=False)
    embed2.add_field(name="/set_priority_role @Rol", value=f"Rol koji izbacuje zadnjeg bez njega kad je lista puna. Trenutno: **{priority_status}**", inline=False)
    embed2.add_field(name="/clear_priority_role", value="Uklanja priority rol.", inline=False)
    monitor_vc_val = "*(isključeno)*"
    if MONITOR_VC_ID:
        mvc = interaction.guild.get_channel(MONITOR_VC_ID)
        monitor_vc_val = f"**{mvc.name}**" if mvc else f"ID {MONITOR_VC_ID}"
    embed2.add_field(name="/set_vc_remind minute", value=f"Automatski DM svima na listi — biti u INF VC do :{str(DRAW_MINUTE).zfill(2)}. Trenutno: **{vc_remind_val}**", inline=False)
    embed2.add_field(name="/set_vc_remind_off", value="Isključuje automatski VC podsjetnik.", inline=False)
    embed2.add_field(name="/vc_remind", value="Odmah šalje DM podsjetnik svima na listi.", inline=False)
    embed2.add_field(name="/set_monitor_vc #kanal", value=f"Prati koji su učesnici u VC-u — 🟢 u kanalu, 🔴 nije. Trenutno: {monitor_vc_val}", inline=False)
    embed2.add_field(name="/set_monitor_vc_off", value="Isključuje VC lampice s liste.", inline=False)
    embed2.set_footer(text="Sve admin komande vidljive su samo tebi.")

    await interaction.response.send_message(embed=embed1, ephemeral=True)
    await interaction.followup.send(embed=embed2, ephemeral=True)


# ==========================================
# BOT EVENTS
# ==========================================
@bot.event
async def on_ready():
    print(f"✅ LOGGED IN AS {bot.user} (ID: {bot.user.id})")
    if CHANNEL_ID == 0:
        print("⚠️  Kanal nije postavljen. Admin treba koristiti /set_channel na serveru.")
    else:
        print(f"📡 CHANNEL TARGET: {CHANNEL_ID}")
        print(f"🚛 BOT AKTIVAN — sljedeći event u :{str(START_MINUTE).zfill(2)}")
    bot.add_view(JoinButtonView())
    event_scheduler.start()
    vc_status_refresh.start()
    await bot.tree.sync()
    print("✅ Slash komande sinkronizirane.")


@bot.event
async def on_guild_join(guild):
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages:
            embed = discord.Embed(
                title="🚛 Ammo Car Bot — Setup",
                description=(
                    "Hvala što si me dodao/la! Koristi slash komande za postavljanje:\n\n"
                    f"**1.** `/set_channel` — odaberi kanal za events\n"
                    f"**2.** `/set_time` — postavi minute starta i kraja\n"
                    f"**3.** `/set_draw` — postavi minutu izvlačenja\n\n"
                    f"Nakon toga bot automatski pokreće event svaki sat.\n"
                    f"Upiši `/helpinf` za sve komande."
                ),
                color=0xFF5500
            )
            await channel.send(embed=embed)
            break


# ==========================================
# RUN THE BOT
# ==========================================
if __name__ == "__main__":
    if not TOKEN:
        print("❌ ERROR: DISCORD_TOKEN secret is not set!")
    else:
        bot.run(TOKEN)
