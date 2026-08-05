import discord
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
# UI: BUTTON + EMBED
# ==========================================
def build_embed():
    if not current_participants:
        participant_text = "🎯 *No one has joined yet*"
    else:
        channel = bot.get_channel(CHANNEL_ID)
        guild = channel.guild if channel else None
        # Build set of member IDs currently in the monitored VC (up to 40 members)
        vc_member_ids = set()
        if MONITOR_VC_ID and guild:
            vc_channel = guild.get_channel(MONITOR_VC_ID)
            if vc_channel and isinstance(vc_channel, discord.VoiceChannel):
                vc_member_ids = {m.id for m in vc_channel.members[:40]}

        lines = []
        for idx, uid in enumerate(current_participants[:MAX_SLOTS], start=1):
            # Use stored name first; fall back to live cache lookup, then mention
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

def _is_admin(guild: discord.Guild, user_id: int) -> bool:
    """Check admin perms via guild member (works from both guild and DM interactions)."""
    if guild is None:
        return False
    member = guild.get_member(user_id)
    return bool(member and member.guild_permissions.administrator)


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

        # ── Parse all fields first (no globals touched yet) ──────────────────

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
                        errors.append("⚠️ Vremena: ne možeš mijenjati dok event traje (`!force_end` prvo).")
                    else:
                        new_start, new_end = s, e
                except ValueError:
                    errors.append("❌ Vremena: upiši dva broja (npr. `25 40`).")

        # Use staged start/end for cross-field validation if changed
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

        eff_draw = new_draw if new_draw is not None else DRAW_MINUTE

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

        # ── Stop on errors — nothing is applied ──────────────────────────────
        if errors:
            await interaction.response.send_message(
                "⚠️ **Greške — ništa nije spremljeno:**\n" + "\n".join(errors),
                ephemeral=True,
            )
            return

        # ── Apply all validated changes ───────────────────────────────────────
        applied = []

        if new_channel_id is not None:
            CHANNEL_ID = new_channel_id
            ch = guild.get_channel(CHANNEL_ID)
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
                f"✅ VC lampice postavljene na: **{vc.name}**\n🟢 = u kanalu  🔴 = nije u kanalu", ephemeral=True
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
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.author_id = author_id

    def _check_admin(self, interaction: discord.Interaction) -> bool:
        guild = bot.get_guild(self.guild_id)
        return _is_admin(guild, interaction.user.id)

    @discord.ui.button(label="🔧 Postavi konfiguraciju", style=discord.ButtonStyle.primary)
    async def open_setup_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_admin(interaction):
            await interaction.response.send_message("❌ Samo admini.", ephemeral=True)
            return
        await interaction.response.send_modal(SetupModal(self.guild_id))

    @discord.ui.button(label="⭐ Priority rola", style=discord.ButtonStyle.secondary)
    async def open_priority_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_admin(interaction):
            await interaction.response.send_message("❌ Samo admini.", ephemeral=True)
            return
        await interaction.response.send_modal(PriorityRoleModal(self.guild_id))

    @discord.ui.button(label="🎙️ VC lampice", style=discord.ButtonStyle.secondary)
    async def open_monitor_vc_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_admin(interaction):
            await interaction.response.send_message("❌ Samo admini.", ephemeral=True)
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
        if not self._check_admin(interaction):
            await interaction.response.send_message("❌ Samo admini.", ephemeral=True)
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
        return
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


async def private_reply(ctx, content=None, embed=None):
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass
    try:
        if embed:
            await ctx.author.send(embed=embed)
        else:
            await ctx.author.send(content)
    except discord.Forbidden:
        if embed:
            await ctx.send(embed=embed)
        else:
            await ctx.send(content)


# ==========================================
# SCHEDULER: RUNS EVERY MINUTE
# ==========================================
@tasks.loop(seconds=5)
async def vc_status_refresh():
    """Refresh the embed every 15s while event is active so VC lampice stay current."""
    if event_active and MONITOR_VC_ID and current_event_message:
        await update_message()


@tasks.loop(minutes=1)
async def event_scheduler():
    global event_active, join_button_locked, current_participants, current_event_message

    now = datetime.now(TIMEZONE)
    minute = now.minute

    # Skip everything if channel not configured
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

    # DRAW AT DRAW_MINUTE (lista ostaje otvorena)
    if minute == DRAW_MINUTE and event_active:
        channel = bot.get_channel(CHANNEL_ID)

        if len(current_participants) == 0:
            await channel.send("😢 **Nitko nije na listi. Ajmo se aktivirat malo.**")
        else:
            eligible = [uid for uid in current_participants if uid not in BLACKLIST_USERS]
            if not eligible:
                await channel.send("⚠️ **Nitko od prijavljenih nije prihvatljiv za izvlačenje.** Svi sudionici su na blacklisti.")
            else:
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

        # Post plain-text list before clearing state
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

        if current_event_message:
            old_view = discord.ui.View.from_message(current_event_message)
            for child in old_view.children:
                child.disabled = True
            await current_event_message.edit(view=old_view)
            current_event_message = None

        print(f"🏁 Event finished at {now.strftime('%H:%M')}")


# ==========================================
# ADMIN COMMANDS
# ==========================================
@bot.command(name="force_start")
@commands.has_permissions(administrator=True)
async def force_start(ctx):
    global event_active, join_button_locked, current_participants, current_event_message

    if event_active:
        await private_reply(ctx, "⚠️ Event already running! Wait for it to finish.")
        return

    event_active = True
    join_button_locked = False
    current_participants = []
    participant_names.clear()

    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass

    embed = build_embed()
    view = JoinButtonView()
    msg = await ctx.channel.send(embed=embed, view=view)
    current_event_message = msg
    await ctx.channel.send(f"@everyone 🚨 **Inf lista je pocela imate do :{str(END_MINUTE).zfill(2)} da udete i pobjednik vozi ammo!**")

    await asyncio.sleep(900)
    if event_active:
        join_button_locked = True
        await update_message()
        if len(current_participants) == 0:
            await ctx.channel.send("😢 No one joined. Event cancelled.")
        else:
            eligible = [uid for uid in current_participants if uid not in BLACKLIST_USERS]
            if not eligible:
                await ctx.channel.send("⚠️ **No eligible participants** — all are on the blacklist.")
            else:
                winner_id = random.choice(eligible)
                last_winner_id = winner_id
                winner_history.append({"id": winner_id, "time": datetime.now(TIMEZONE).strftime("%d.%m. %H:%M")})
                if len(winner_history) > 5:
                    winner_history.pop(0)
                winner = bot.get_user(winner_id)
                winner_mention = winner.mention if winner else f"<@{winner_id}>"
                await ctx.channel.send(f"🎉 **WINNER:** {winner_mention} drives the Ammo Car! 🚛")
        event_active = False
        current_participants = []
        participant_names.clear()


@bot.command(name="force_end")
@commands.has_permissions(administrator=True)
async def force_end(ctx):
    global event_active, current_participants, current_event_message
    if not event_active:
        await private_reply(ctx, "❌ Nema aktivnog eventa.")
        return
    event_active = False
    current_participants = []
    participant_names.clear()
    if current_event_message:
        old_view = discord.ui.View.from_message(current_event_message)
        for child in old_view.children:
            child.disabled = True
        await current_event_message.edit(view=old_view)
        current_event_message = None
    await private_reply(ctx, "⏹️ Event force-stopped.")


@bot.command(name="ping")
async def ping(ctx):
    latency = round(bot.latency * 1000)
    await private_reply(ctx, f"🏓 Pong! Latencija: **{latency}ms**")


@bot.command(name="remind")
@commands.has_permissions(administrator=True)
async def remind(ctx):
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        await private_reply(ctx, "❌ Event kanal nije pronađen.")
        return
    await channel.send(f"⏳ **INF lista počinje za malo — :{str(START_MINUTE).zfill(2)}! Budite spremni! 🚛**")
    await private_reply(ctx, "✅ Podsjetnik poslan.")


@bot.command(name="infon")
@commands.has_permissions(administrator=True)
async def infon(ctx):
    global inf_bot_online
    inf_bot_online = True
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass
    channel = bot.get_channel(CHANNEL_ID)
    target = channel if channel else ctx.channel
    await target.send("INF bot uključen budite spremni.")


@bot.command(name="infof")
@commands.has_permissions(administrator=True)
async def infof(ctx):
    global inf_bot_online
    inf_bot_online = False
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass
    channel = bot.get_channel(CHANNEL_ID)
    target = channel if channel else ctx.channel
    await target.send("Nažalost izgubili smo neformalnu bot neradi dok ne dobijemo neformalnu nazad")


@bot.command(name="infostatus")
@commands.has_permissions(administrator=True)
async def infostatus(ctx):
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass
    channel = bot.get_channel(CHANNEL_ID)
    target = channel if channel else ctx.channel
    if inf_bot_online is True:
        await target.send("✅ **INF bot status:** Uključen — budite spremni.")
    elif inf_bot_online is False:
        await target.send("❌ **INF bot status:** Isključen — nema neformalne dok ne dobijemo nazad.")
    else:
        await target.send("❓ **INF bot status:** Status još nije postavljen. Koristi `!infon` ili `!infof`.")


@bot.command(name="reroll")
@commands.has_permissions(administrator=True)
async def reroll(ctx):
    if len(current_participants) == 0:
        await private_reply(ctx, "😢 **Lista je prazna. Nema koga birati!**")
        return

    eligible = [uid for uid in current_participants if uid not in BLACKLIST_USERS]
    if not eligible:
        await private_reply(ctx, "⚠️ **Nitko nije prihvatljiv za reroll.** Svi su na blacklisti.")
        return

    winner_id = random.choice(eligible)
    last_winner_id = winner_id
    winner_history.append({"id": winner_id, "time": datetime.now(TIMEZONE).strftime("%d.%m. %H:%M")})
    if len(winner_history) > 5:
        winner_history.pop(0)
    winner = bot.get_user(winner_id)
    winner_mention = winner.mention if winner else f"<@{winner_id}>"
    channel = bot.get_channel(CHANNEL_ID)
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass
    if channel:
        await channel.send(f"🔁 **REROLL!** Novi vozač Ammo Cara je... {winner_mention} 🎉🚗💨")


@bot.command(name="add")
async def add_to_list(ctx, member: discord.Member = None):
    global current_participants, join_button_locked

    if not event_active:
        await private_reply(ctx, "❌ Nema aktivnog eventa.")
        return

    if join_button_locked:
        await private_reply(ctx, "🔒 Lista je zaključana.")
        return

    if member is None:
        await private_reply(ctx, "❌ Navedi korisnika. Primjer: `!add @korisnik`")
        return

    if member.id in BAN_USERS:
        await private_reply(ctx, f"🚫 **{member.display_name}** je baniran/a i ne može ući na listu.")
        return

    if member.id in current_participants:
        await private_reply(ctx, f"⚠️ **{member.display_name}** već je na listi.")
        return

    guild = ctx.guild
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
                await private_reply(ctx, "❌ Lista je puna i svi imaju priority rol. Nema mjesta.")
                return

            current_participants.remove(bumped_uid)
            current_participants.append(member.id)
            participant_names[member.id] = member.display_name
            bumped_member = guild.get_member(bumped_uid) if guild else None
            bumped_name = bumped_member.display_name if bumped_member else participant_names.get(bumped_uid, f"<@{bumped_uid}>")
            await private_reply(ctx, f"⭐ **{member.display_name}** dodan priority rolom! **{bumped_name}** je izbačen/a.")
            await update_message()
        else:
            await private_reply(ctx, f"❌ Lista je puna ({MAX_SLOTS}/{MAX_SLOTS}).")
        return

    current_participants.append(member.id)
    participant_names[member.id] = member.display_name
    prefix = "⭐ " if has_priority else ""
    await private_reply(ctx, f"✅ **{prefix}{member.display_name}** dodan/a na listu! ({len(current_participants)}/{MAX_SLOTS})")
    await update_message()


@bot.command(name="kick_from_list")
@commands.has_permissions(administrator=True)
async def kick_from_list(ctx, member: discord.Member = None):
    if not event_active:
        await private_reply(ctx, "❌ Nema aktivnog eventa.")
        return

    if member is None:
        await private_reply(ctx, "❌ Navedi korisnika. Primjer: `!kick_from_list @korisnik`")
        return

    if member.id not in current_participants:
        await private_reply(ctx, f"⚠️ **{member.display_name}** nije na listi.")
        return

    current_participants.remove(member.id)
    await private_reply(ctx, f"✅ **{member.display_name}** je maknut/a s liste.")
    await update_message()


@bot.command(name="ban")
@commands.has_permissions(administrator=True)
async def ban_user(ctx, member: discord.Member = None):
    global BAN_USERS

    if member is None:
        await private_reply(ctx, "❌ Navedi korisnika. Primjer: `!ban @korisnik`")
        return

    if member.id in BAN_USERS:
        await private_reply(ctx, f"⚠️ **{member.display_name}** već je baniran/a.")
        return

    BAN_USERS.add(member.id)
    if member.id in current_participants:
        current_participants.remove(member.id)
        await update_message()
        await private_reply(ctx, f"🔨 **{member.display_name}** je baniran/a i maknut/a s liste.")
    else:
        await private_reply(ctx, f"🔨 **{member.display_name}** je baniran/a — ne može ući na listu.")


@bot.command(name="unban")
@commands.has_permissions(administrator=True)
async def unban_user(ctx, member: discord.Member = None):
    global BAN_USERS

    if member is None:
        await private_reply(ctx, "❌ Navedi korisnika. Primjer: `!unban @korisnik`")
        return

    if member.id not in BAN_USERS:
        await private_reply(ctx, f"⚠️ **{member.display_name}** nije baniran/a.")
        return

    BAN_USERS.discard(member.id)
    await private_reply(ctx, f"✅ **{member.display_name}** je unbaniran/a — može ponovo ući na listu.")


@bot.command(name="banlist")
@commands.has_permissions(administrator=True)
async def banlist(ctx):
    if not BAN_USERS:
        await private_reply(ctx, "✅ Nema banirani korisnika.")
        return

    guild = ctx.guild
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
    await private_reply(ctx, embed=embed)


@bot.command(name="set_channel")
@commands.has_permissions(administrator=True)
async def set_channel(ctx, channel: discord.TextChannel = None):
    global CHANNEL_ID

    if channel is None:
        current = bot.get_channel(CHANNEL_ID)
        current_mention = current.mention if current else f"`{CHANNEL_ID}` *(not found)*"
        await private_reply(ctx, f"ℹ️ Current event channel: {current_mention}\nUsage: `!set_channel #channel-name`")
        return

    if event_active:
        await private_reply(ctx, "⚠️ Can't change channel while an event is running. Use `!force_end` first.")
        return

    old_id = CHANNEL_ID
    CHANNEL_ID = channel.id
    save_settings()
    old_channel = bot.get_channel(old_id)
    old_mention = old_channel.mention if old_channel else f"*(nije bio postavljen)*"
    await private_reply(ctx, f"✅ Event kanal postavljen: {old_mention} → {channel.mention}\n✅ Snimljeno trajno — ostaje i nakon restarta.")


@bot.command(name="set_slots")
@commands.has_permissions(administrator=True)
async def set_slots(ctx, number: int = None):
    global MAX_SLOTS

    if number is None:
        await private_reply(ctx, f"ℹ️ Current max slots: **{MAX_SLOTS}**\nUsage: `!set_slots <number>` (e.g. `!set_slots 20`)")
        return

    if number < 1 or number > 100:
        await private_reply(ctx, "❌ Number must be between 1 and 100.")
        return

    if event_active:
        await private_reply(ctx, "⚠️ Can't change slots while an event is running. Use `!force_end` first.")
        return

    old = MAX_SLOTS
    MAX_SLOTS = number
    save_settings()
    await private_reply(ctx, f"✅ Max slots updated: **{old}** → **{MAX_SLOTS}**")


@bot.command(name="set_priority_role")
@commands.has_permissions(administrator=True)
async def set_priority_role(ctx, role: discord.Role = None):
    global PRIORITY_ROLE_ID

    if role is None:
        if PRIORITY_ROLE_ID:
            guild = ctx.guild
            r = guild.get_role(PRIORITY_ROLE_ID)
            mention = r.mention if r else f"`{PRIORITY_ROLE_ID}` *(nije pronađen)*"
            await private_reply(ctx, f"ℹ️ Trenutni priority rol: {mention}\nKorištenje: `!set_priority_role @Rol`")
        else:
            await private_reply(ctx, "ℹ️ Priority rol nije postavljen.\nKorištenje: `!set_priority_role @Rol`")
        return

    PRIORITY_ROLE_ID = role.id
    save_settings()
    await private_reply(ctx,
        f"✅ Priority rol postavljen na **{role.name}**!\n"
        f"Kad je lista puna, korisnici s ovim rolom izbacuju zadnjeg bez njega. ⭐"
    )


@bot.command(name="clear_priority_role")
@commands.has_permissions(administrator=True)
async def clear_priority_role(ctx):
    global PRIORITY_ROLE_ID
    if not PRIORITY_ROLE_ID:
        await private_reply(ctx, "ℹ️ Priority rol već nije postavljen.")
        return
    PRIORITY_ROLE_ID = None
    save_settings()
    await private_reply(ctx, "✅ Priority rol uklonjen. Svi su ravnopravni.")


@bot.command(name="blacklist_user")
@commands.has_permissions(administrator=True)
async def blacklist_user(ctx, member: discord.Member = None):
    global BLACKLIST_USERS

    if member is None:
        await private_reply(ctx, "❌ Navedi korisnika. Primjer: `!blacklist_user @korisnik`")
        return

    if member.id in BLACKLIST_USERS:
        await private_reply(ctx, f"⚠️ **{member.display_name}** već je na blacklisti.")
        return

    BLACKLIST_USERS.add(member.id)
    await private_reply(ctx,
        f"🚫 **{member.display_name}** dodan/a na blacklistu.\n"
        f"Može se prijaviti na listu, ali neće biti biran/a za Ammo Car."
    )


@bot.command(name="unblacklist_user")
@commands.has_permissions(administrator=True)
async def unblacklist_user(ctx, member: discord.Member = None):
    global BLACKLIST_USERS

    if member is None:
        await private_reply(ctx, "❌ Navedi korisnika. Primjer: `!unblacklist_user @korisnik`")
        return

    if member.id not in BLACKLIST_USERS:
        await private_reply(ctx, f"⚠️ **{member.display_name}** nije na blacklisti.")
        return

    BLACKLIST_USERS.discard(member.id)
    await private_reply(ctx, f"✅ **{member.display_name}** uklonjen/a s blackliste. Može biti biran/a za Ammo Car.")


@bot.command(name="blacklist_list")
@commands.has_permissions(administrator=True)
async def blacklist_list(ctx):
    if not BLACKLIST_USERS:
        await private_reply(ctx, "✅ Blacklista je prazna — svi sudionici su prihvatljivi za izvlačenje.")
        return

    guild = ctx.guild
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
    await private_reply(ctx, embed=embed)


@bot.command(name="set_monitor_vc")
@commands.has_permissions(administrator=True)
async def set_monitor_vc(ctx, channel: discord.VoiceChannel = None):
    global MONITOR_VC_ID
    if channel is None:
        if MONITOR_VC_ID:
            vc = ctx.guild.get_channel(MONITOR_VC_ID)
            name = vc.name if vc else f"ID {MONITOR_VC_ID}"
            await private_reply(ctx, f"🎙️ Trenutno praćeni VC: **{name}**\nKoristi `!set_monitor_vc #kanal` za promjenu ili `!set_monitor_vc_off` za isključivanje.")
        else:
            await private_reply(ctx, "🎙️ VC praćenje je isključeno. Koristi `!set_monitor_vc #kanal` za postavljanje.")
        return
    MONITOR_VC_ID = channel.id
    save_settings()
    await private_reply(ctx, f"✅ VC praćenje postavljeno na: **{channel.name}**\nLampice 🟢/🔴 pored imena pokazuju tko je u tom VC-u.")

@bot.command(name="set_monitor_vc_off")
@commands.has_permissions(administrator=True)
async def set_monitor_vc_off(ctx):
    global MONITOR_VC_ID
    if MONITOR_VC_ID is None:
        await private_reply(ctx, "ℹ️ VC praćenje je već isključeno.")
        return
    MONITOR_VC_ID = None
    save_settings()
    await private_reply(ctx, "✅ VC praćenje isključeno — lampice više neće biti prikazane.")

@bot.command(name="set_vc_remind")
@commands.has_permissions(administrator=True)
async def set_vc_remind(ctx, minute: int = None):
    global VC_REMIND_MINUTE

    if minute is None:
        if VC_REMIND_MINUTE is not None:
            await private_reply(ctx,
                f"ℹ️ VC podsjetnik se šalje u :{str(VC_REMIND_MINUTE).zfill(2)}.\n"
                f"Korištenje: `!set_vc_remind <minuta>` — za isključivanje koristi `!set_vc_remind_off`."
            )
        else:
            await private_reply(ctx,
                "ℹ️ VC podsjetnik nije postavljen.\n"
                "Korištenje: `!set_vc_remind <minuta>` (npr. `!set_vc_remind 32`)"
            )
        return

    if not (0 <= minute <= 59):
        await private_reply(ctx, "❌ Minuta mora biti između 0 i 59.")
        return

    old = VC_REMIND_MINUTE
    VC_REMIND_MINUTE = minute
    save_settings()
    old_str = f":{str(old).zfill(2)}" if old is not None else "*(isključeno)*"
    await private_reply(ctx,
        f"✅ VC podsjetnik postavljen: {old_str} → :{str(VC_REMIND_MINUTE).zfill(2)}\n"
        f"Bot će u :{str(VC_REMIND_MINUTE).zfill(2)} poslati DM svima na listi da moraju biti u INF VC do :{str(DRAW_MINUTE).zfill(2)}."
    )


@bot.command(name="set_vc_remind_off")
@commands.has_permissions(administrator=True)
async def set_vc_remind_off(ctx):
    global VC_REMIND_MINUTE
    if VC_REMIND_MINUTE is None:
        await private_reply(ctx, "ℹ️ VC podsjetnik već je isključen.")
        return
    VC_REMIND_MINUTE = None
    save_settings()
    await private_reply(ctx, "✅ VC podsjetnik isključen.")


@bot.command(name="vc_remind")
@commands.has_permissions(administrator=True)
async def vc_remind_now(ctx):
    """Odmah šalje DM podsjetnik svima na listi."""
    if not event_active:
        await private_reply(ctx, "❌ Nema aktivnog eventa.")
        return
    if not current_participants:
        await private_reply(ctx, "😢 Nitko nije na listi.")
        return
    sent, failed = await send_vc_reminders()
    msg = f"✅ Podsjetnik poslan: **{sent}** primilo, **{failed}** nije moglo primiti DM."
    await private_reply(ctx, msg)


@bot.command(name="helpinf")
@commands.has_permissions(administrator=True)
async def help_command(ctx):
    priority_status = "nije postavljen"
    if PRIORITY_ROLE_ID:
        r = ctx.guild.get_role(PRIORITY_ROLE_ID)
        priority_status = r.name if r else f"ID {PRIORITY_ROLE_ID}"

    vc_remind_val = f":{str(VC_REMIND_MINUTE).zfill(2)}" if VC_REMIND_MINUTE is not None else "*(isključeno)*"

    embed1 = discord.Embed(
        title="📋 Inf Lista — Admin Komande (1/2)",
        color=0xFF5500
    )
    embed1.add_field(name="!setup", value="Interaktivni wizard — postavi kanal, vremena, slotove, VC podsjetnik i priority rolu kroz formu.", inline=False)
    embed1.add_field(name="!force_start", value="Ručno pokreće event odmah.", inline=False)
    embed1.add_field(name="!force_end", value="Zaustavlja trenutni event bez izvlačenja pobjednika.", inline=False)
    embed1.add_field(name="!reroll", value="Bira novog pobjednika — lista ostaje ista.", inline=False)
    embed1.add_field(name="!winner", value="Ponovo objavljuje zadnjeg pobjednika u event kanalu.", inline=False)
    embed1.add_field(name="!clearwinner", value="Resetira zabilježenog pobjednika.", inline=False)
    embed1.add_field(name="!history", value="Prikazuje zadnjih 5 pobjednika s vremenima.", inline=False)
    embed1.add_field(name="!remind", value="Ručno šalje podsjetnik u event kanal da lista uskoro počinje.", inline=False)
    embed1.add_field(name="!infon", value="Bot piše u kanal: **INF bot uključen budite spremni.**", inline=False)
    embed1.add_field(name="!infof", value="Bot piše u kanal: **Nažalost izgubili smo neformalnu...**", inline=False)
    embed1.add_field(name="!infostatus", value="Prikazuje trenutni status bota u kanalu.", inline=False)
    embed1.add_field(name="!ping", value="Provjeri radi li bot i latenciju. *(svi mogu koristiti)*", inline=False)
    embed1.add_field(name="!status", value="Pokazuje stanje eventa — koliko je ljudi ušlo i kada kreće sljedeći.", inline=False)
    embed1.add_field(name="!add @korisnik", value="Dodaj korisnika na listu dok je event aktivan. *(svi mogu koristiti)*", inline=False)
    embed1.add_field(name="!kick_from_list @korisnik", value="Makni korisnika s liste dok je event aktivan.", inline=False)
    embed1.set_footer(text="Nastavak u sljedećoj poruci →")

    embed2 = discord.Embed(
        title="📋 Inf Lista — Admin Komande (2/2)",
        color=0xFF5500
    )
    embed2.add_field(name="!ban @korisnik", value="Zabranjuje korisniku ulaz na listu.", inline=False)
    embed2.add_field(name="!unban @korisnik", value="Uklanja ban — korisnik može ponovo ući na listu.", inline=False)
    embed2.add_field(name="!banlist", value="Prikaži sve trenutno banirane korisnike.", inline=False)
    embed2.add_field(name="!set_time <start> <end>", value=f"Mijenja minute starta i kraja.\nPrimjer: `!set_time 25 40` — trenutno: :{str(START_MINUTE).zfill(2)} → :{str(END_MINUTE).zfill(2)}", inline=False)
    embed2.add_field(name="!set_draw <minuta>", value=f"Mijenja minutu izvlačenja.\nPrimjer: `!set_draw 35` — trenutno: :{str(DRAW_MINUTE).zfill(2)}", inline=False)
    embed2.add_field(name="!set_slots <broj>", value=f"Mijenja max broj mjesta. Trenutno: {MAX_SLOTS}", inline=False)
    embed2.add_field(name="!set_channel #kanal", value="Mijenja kanal za event. Bez argumenta = prikaži trenutni.", inline=False)
    embed2.add_field(name="!set_priority_role @Rol", value=f"Rol koji izbacuje zadnjeg bez njega kad je lista puna. Trenutno: **{priority_status}**", inline=False)
    embed2.add_field(name="!clear_priority_role", value="Uklanja priority rol.", inline=False)
    monitor_vc_val = "*(isključeno)*"
    if MONITOR_VC_ID:
        mvc = ctx.guild.get_channel(MONITOR_VC_ID)
        monitor_vc_val = f"**{mvc.name}**" if mvc else f"ID {MONITOR_VC_ID}"
    embed2.add_field(name="!set_vc_remind <minuta>", value=f"Automatski DM svima na listi — biti u INF VC do :{str(DRAW_MINUTE).zfill(2)}. Trenutno: **{vc_remind_val}**", inline=False)
    embed2.add_field(name="!set_vc_remind_off", value="Isključuje automatski VC podsjetnik.", inline=False)
    embed2.add_field(name="!vc_remind", value="Odmah šalje DM podsjetnik svima na listi.", inline=False)
    embed2.add_field(name="!set_monitor_vc #kanal", value=f"Prati koji su učesnici u VC-u — 🟢 u kanalu, 🔴 nije. Trenutno: {monitor_vc_val}", inline=False)
    embed2.add_field(name="!set_monitor_vc_off", value="Isključuje VC lampice s liste.", inline=False)
    embed2.set_footer(text="Sve komande su admin only. Odgovori su vidljivi samo tebi.")

    await private_reply(ctx, embed=embed1)
    await private_reply(ctx, embed=embed2)


@bot.command(name="set_time")
@commands.has_permissions(administrator=True)
async def set_time(ctx, start: int = None, end: int = None):
    global START_MINUTE, END_MINUTE

    if start is None or end is None:
        await private_reply(ctx,
            f"ℹ️ Trenutno: start :{str(START_MINUTE).zfill(2)} → end :{str(END_MINUTE).zfill(2)}\n"
            f"Korištenje: `!set_time <start> <end>` (npr. `!set_time 25 40`)\n"
            f"Oba broja moraju biti između 0 i 59."
        )
        return

    if not (0 <= start <= 59) or not (0 <= end <= 59):
        await private_reply(ctx, "❌ Minuta mora biti između 0 i 59.")
        return

    if start == end:
        await private_reply(ctx, "❌ Start i end ne mogu biti isti.")
        return

    if event_active:
        await private_reply(ctx, "⚠️ Ne možeš mijenjati vrijeme dok event traje. Koristi `!force_end` prvo.")
        return

    old_start, old_end = START_MINUTE, END_MINUTE
    START_MINUTE = start
    END_MINUTE = end
    save_settings()
    await private_reply(ctx,
        f"✅ Vrijeme updateano!\n"
        f"**Start:** :{str(old_start).zfill(2)} → :{str(START_MINUTE).zfill(2)}\n"
        f"**End:** :{str(old_end).zfill(2)} → :{str(END_MINUTE).zfill(2)}\n"
        f"Svaki sat bot šalje u :{str(START_MINUTE).zfill(2)} i zaključava u :{str(END_MINUTE).zfill(2)}."
    )


@bot.command(name="set_draw_time", aliases=["set_draw"])
@commands.has_permissions(administrator=True)
async def set_draw_time(ctx, minute: int = None):
    global DRAW_MINUTE

    if minute is None:
        await private_reply(ctx,
            f"ℹ️ Trenutno izvlačenje je u :{str(DRAW_MINUTE).zfill(2)}.\n"
            f"Korištenje: `!set_draw <minuta>` ili `!set_draw_time <minuta>` (npr. `!set_draw 35`)\n"
            f"Minuta mora biti između 0 i 59 i prije kraja (:{str(END_MINUTE).zfill(2)})."
        )
        return

    if not (0 <= minute <= 59):
        await private_reply(ctx, "❌ Minuta mora biti između 0 i 59.")
        return

    if minute >= END_MINUTE:
        await private_reply(ctx, f"❌ Minuta izvlačenja mora biti prije kraja (:{str(END_MINUTE).zfill(2)}). Odaberi manju minutu.")
        return

    if minute <= START_MINUTE:
        await private_reply(ctx, f"❌ Minuta izvlačenja mora biti nakon starta (:{str(START_MINUTE).zfill(2)}). Odaberi veću minutu.")
        return

    if event_active:
        await private_reply(ctx, "⚠️ Ne možeš mijenjati vrijeme izvlačenja dok event traje. Koristi `!force_end` prvo.")
        return

    old = DRAW_MINUTE
    DRAW_MINUTE = minute
    save_settings()
    await private_reply(ctx,
        f"✅ Minuta izvlačenja updateana: :{str(old).zfill(2)} → :{str(DRAW_MINUTE).zfill(2)}\n"
        f"Raspored: start :{str(START_MINUTE).zfill(2)} → izvlačenje :{str(DRAW_MINUTE).zfill(2)} → kraj :{str(END_MINUTE).zfill(2)}"
    )


@bot.command(name="winner")
@commands.has_permissions(administrator=True)
async def winner_cmd(ctx):
    """Ponovo objavljuje zadnjeg pobjednika u event kanalu."""
    if last_winner_id is None:
        await private_reply(ctx, "❌ Nema zabilježenog pobjednika od kad je bot pokrenut.")
        return
    winner = bot.get_user(last_winner_id)
    winner_mention = winner.mention if winner else f"<@{last_winner_id}>"
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        await private_reply(ctx, "❌ Event kanal nije pronađen.")
        return
    await channel.send(f"🏆 **Podsjetnik — zadnji pobjednik Ammo Cara:** {winner_mention} 🚗💨")
    await private_reply(ctx, f"✅ Pobjednik ponovo objavljen: {winner_mention}")


@bot.command(name="clearwinner")
@commands.has_permissions(administrator=True)
async def clearwinner(ctx):
    """Resetira zabilježenog pobjednika."""
    global last_winner_id
    if last_winner_id is None:
        await private_reply(ctx, "ℹ️ Nema zabilježenog pobjednika — već je čisto.")
        return
    last_winner_id = None
    await private_reply(ctx, "✅ Zadnji pobjednik resetiran.")


@bot.command(name="history")
@commands.has_permissions(administrator=True)
async def history(ctx):
    """Prikazuje zadnjih 5 pobjednika."""
    if not winner_history:
        await private_reply(ctx, "ℹ️ Nema zabilježenih pobjednika od kad je bot pokrenut.")
        return
    embed = discord.Embed(title="🏆 Zadnjih 5 pobjednika", color=0xFF5500)
    lines = []
    for i, entry in enumerate(reversed(winner_history), 1):
        user = bot.get_user(entry["id"])
        name = user.display_name if user else f"ID {entry['id']}"
        lines.append(f"`{i}.` **{name}** — {entry['time']}")
    embed.description = "\n".join(lines)
    await private_reply(ctx, embed=embed)


@bot.command(name="setup")
@commands.has_permissions(administrator=True)
async def setup(ctx):
    """Interaktivni setup wizard — otvara formu za postavljanje bota."""
    channel = bot.get_channel(CHANNEL_ID)
    channel_val = channel.mention if channel else "❌ Nije postavljen"

    if PRIORITY_ROLE_ID:
        priority_role = ctx.guild.get_role(PRIORITY_ROLE_ID)
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
    embed.set_footer(text="Vidljivo samo tebi • !helpinf za sve komande")

    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass

    view = SetupView(guild_id=ctx.guild.id, author_id=ctx.author.id)
    try:
        await ctx.author.send(embed=embed, view=view)
    except discord.Forbidden:
        await ctx.send(embed=embed, view=view)


@bot.command(name="status")
@commands.has_permissions(administrator=True)
async def status(ctx):
    now = datetime.now(TIMEZONE)
    minute = now.minute

    if not event_active:
        mins_until = (START_MINUTE - minute) % 60
        desc = (
            f"**📭 No event running**\n"
            f"Next auto-start in **{mins_until} minute(s)** (at :{str(START_MINUTE).zfill(2)})\n\n"
            f"Use `!force_start` to start one now."
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
    await private_reply(ctx, embed=embed)


@bot.event
async def on_ready():
    print(f"✅ LOGGED IN AS {bot.user} (ID: {bot.user.id})")
    if CHANNEL_ID == 0:
        print("⚠️  Kanal nije postavljen. Admin treba upisati !set_channel #kanal na serveru.")
    else:
        print(f"📡 CHANNEL TARGET: {CHANNEL_ID}")
        print(f"🚛 BOT AKTIVAN — sljedeći event u :{str(START_MINUTE).zfill(2)}")
    bot.add_view(JoinButtonView())
    event_scheduler.start()
    vc_status_refresh.start()


@bot.event
async def on_guild_join(guild):
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages:
            embed = discord.Embed(
                title="🚛 Ammo Car Bot — Setup",
                description=(
                    "Hvala što si me dodao/la! Koristi sljedeće admin komande za postavljanje:\n\n"
                    f"**1.** `!set_channel #kanal` — odaberi kanal za events\n"
                    f"**2.** `!set_time 25 40` — postavi minute starta i kraja\n"
                    f"**3.** `!set_draw 35` — postavi minutu izvlačenja\n\n"
                    f"Nakon toga bot automatski pokreće event svaki sat.\n"
                    f"Upiši `!helpinf` za sve komande."
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
