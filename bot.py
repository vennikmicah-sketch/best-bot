"""
Drop / Gen bot
- Auto-drops text and files into channels at random intervals (minimum 3 hours)
- Multiple services, each with its own stock
- Every channel can have its own service(s) and its own timing
- /gen for members, restock announcements, /createchannel with permissions
"""
import io
import os
import pathlib
import random
import sqlite3
import time
import uuid
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: instant command sync in one server
MIN_HOURS = 3.0                   # hard minimum between drops
DATA = pathlib.Path(os.getenv("DATA_DIR", "data"))  # on Railway: point at your Volume mount
FILES = DATA / "files"
FILES.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
db = sqlite3.connect(DATA / "bot.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS services(
    name TEXT PRIMARY KEY,
    gen_cooldown_min INTEGER NOT NULL DEFAULT 60,
    restock_channel INTEGER
);
CREATE TABLE IF NOT EXISTS stock(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    text TEXT,
    path TEXT,
    filename TEXT
);
CREATE TABLE IF NOT EXISTS drops(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    service TEXT NOT NULL,
    min_h REAL NOT NULL,
    max_h REAL NOT NULL,
    mode TEXT NOT NULL DEFAULT 'consume',
    next_ts REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(channel_id, service)
);
CREATE TABLE IF NOT EXISTS gens(
    user_id INTEGER, service TEXT, ts REAL,
    PRIMARY KEY(user_id, service)
);
"""
)
db.commit()


def q(sql, *args):
    return db.execute(sql, args).fetchall()


def q1(sql, *args):
    return db.execute(sql, args).fetchone()


def ex(sql, *args):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def service_exists(name: str) -> bool:
    return q1("SELECT 1 FROM services WHERE name=?", name) is not None


def stock_count(service: str) -> int:
    return q1("SELECT COUNT(*) c FROM stock WHERE service=?", service)["c"]


def next_time(min_h: float, max_h: float) -> float:
    """Fully random delay between min_h and max_h hours (min is never < 3h)."""
    lo = max(MIN_HOURS, min_h)
    hi = max(lo, max_h)
    return time.time() + random.uniform(lo, hi) * 3600


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()
        drop_loop.start()

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")


bot = Bot()
tree = bot.tree
ADMIN = discord.Permissions(administrator=True)


async def service_ac(interaction: discord.Interaction, current: str):
    rows = q("SELECT name FROM services WHERE name LIKE ? LIMIT 25", f"%{current}%")
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]


async def send_item(dest, row, header: str):
    """Send one stock item (text or file) to a channel or user."""
    if row["path"] and os.path.exists(row["path"]):
        await dest.send(header, file=discord.File(row["path"], filename=row["filename"]))
        return
    text = row["text"] or ""
    if len(text) <= 1800:
        await dest.send(f"{header}\n```\n{text}\n```")
    else:
        await dest.send(header, file=discord.File(io.BytesIO(text.encode()), filename="drop.txt"))


def consume(row):
    if row["path"]:
        try:
            os.remove(row["path"])
        except OSError:
            pass
    ex("DELETE FROM stock WHERE id=?", row["id"])


async def announce_restock(service: str, added: int):
    svc = q1("SELECT * FROM services WHERE name=?", service)
    if not svc or not svc["restock_channel"]:
        return
    ch = bot.get_channel(svc["restock_channel"])
    if ch:
        e = discord.Embed(title="📦 Restock!", color=discord.Color.green())
        e.add_field(name="Service", value=service)
        e.add_field(name="Added", value=str(added))
        e.add_field(name="Now in stock", value=str(stock_count(service)))
        try:
            await ch.send(embed=e)
        except discord.HTTPException:
            pass


# --------------------------------------------------------------------------- #
# Auto-drop loop
# --------------------------------------------------------------------------- #
async def run_drop(d) -> bool:
    ch = bot.get_channel(d["channel_id"])
    if ch is None:
        return False
    items = q("SELECT * FROM stock WHERE service=?", d["service"])
    if not items:
        return False
    item = random.choice(items)
    try:
        await send_item(ch, item, f"🎁 **Drop · {d['service']}**")
    except discord.HTTPException:
        return False
    if d["mode"] == "consume":
        consume(item)
    return True


@tasks.loop(seconds=30)
async def drop_loop():
    now = time.time()
    for d in q("SELECT * FROM drops WHERE enabled=1 AND next_ts<=?", now):
        await run_drop(d)  # if empty/failed we still reschedule
        ex("UPDATE drops SET next_ts=? WHERE id=?", next_time(d["min_h"], d["max_h"]), d["id"])


@drop_loop.before_loop
async def _wait():
    await bot.wait_until_ready()


# --------------------------------------------------------------------------- #
# /service
# --------------------------------------------------------------------------- #
service_g = app_commands.Group(
    name="service", description="Manage services", default_permissions=ADMIN, guild_only=True
)


@service_g.command(name="add", description="Create a service (e.g. netflix, nitro, files)")
@app_commands.describe(name="Service name", gen_cooldown_minutes="/gen cooldown per user")
async def service_add(i: discord.Interaction, name: str, gen_cooldown_minutes: int = 60):
    name = name.lower().strip()
    if service_exists(name):
        return await i.response.send_message("Service already exists.", ephemeral=True)
    ex("INSERT INTO services(name, gen_cooldown_min) VALUES(?,?)", name, gen_cooldown_minutes)
    await i.response.send_message(f"✅ Service `{name}` created.", ephemeral=True)


@service_g.command(name="remove", description="Delete a service with its stock and drops")
@app_commands.autocomplete(name=service_ac)
async def service_remove(i: discord.Interaction, name: str):
    for r in q("SELECT path FROM stock WHERE service=? AND path IS NOT NULL", name):
        try:
            os.remove(r["path"])
        except OSError:
            pass
    ex("DELETE FROM stock WHERE service=?", name)
    ex("DELETE FROM drops WHERE service=?", name)
    ex("DELETE FROM services WHERE name=?", name)
    await i.response.send_message(f"🗑️ `{name}` removed.", ephemeral=True)


@service_g.command(name="list", description="List services")
async def service_list(i: discord.Interaction):
    rows = q("SELECT * FROM services")
    if not rows:
        return await i.response.send_message("No services yet.", ephemeral=True)
    lines = [
        f"• `{r['name']}` — stock {stock_count(r['name'])}, gen cooldown {r['gen_cooldown_min']}m"
        + (f", restock → <#{r['restock_channel']}>" if r["restock_channel"] else "")
        for r in rows
    ]
    await i.response.send_message("\n".join(lines), ephemeral=True)


@service_g.command(name="restock_channel", description="Where restock announcements go")
@app_commands.autocomplete(name=service_ac)
async def service_restock(i: discord.Interaction, name: str, channel: discord.TextChannel):
    if not service_exists(name):
        return await i.response.send_message("Unknown service.", ephemeral=True)
    ex("UPDATE services SET restock_channel=? WHERE name=?", channel.id, name)
    await i.response.send_message(f"✅ Restocks for `{name}` → {channel.mention}", ephemeral=True)


@service_g.command(name="cooldown", description="Change /gen cooldown for a service")
@app_commands.autocomplete(name=service_ac)
async def service_cd(i: discord.Interaction, name: str, minutes: int):
    ex("UPDATE services SET gen_cooldown_min=? WHERE name=?", minutes, name)
    await i.response.send_message("✅ Updated.", ephemeral=True)


tree.add_command(service_g)

# --------------------------------------------------------------------------- #
# /stock
# --------------------------------------------------------------------------- #
stock_g = app_commands.Group(
    name="stock", description="Manage stock", default_permissions=ADMIN, guild_only=True
)


@stock_g.command(name="add", description="Add one text item to a service's stock")
@app_commands.autocomplete(service=service_ac)
async def stock_add(i: discord.Interaction, service: str, text: str):
    if not service_exists(service):
        return await i.response.send_message("Unknown service.", ephemeral=True)
    ex("INSERT INTO stock(service, text) VALUES(?,?)", service, text)
    await i.response.send_message(f"✅ Added. Stock: {stock_count(service)}", ephemeral=True)
    await announce_restock(service, 1)


@stock_g.command(name="addfile", description="Add up to 3 files as stock")
@app_commands.autocomplete(service=service_ac)
async def stock_addfile(
    i: discord.Interaction,
    service: str,
    file1: discord.Attachment,
    file2: Optional[discord.Attachment] = None,
    file3: Optional[discord.Attachment] = None,
):
    if not service_exists(service):
        return await i.response.send_message("Unknown service.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    n = 0
    for a in (file1, file2, file3):
        if a is None:
            continue
        path = FILES / f"{uuid.uuid4().hex}_{pathlib.Path(a.filename).name}"
        await a.save(path)
        ex("INSERT INTO stock(service, path, filename) VALUES(?,?,?)", service, str(path), a.filename)
        n += 1
    await i.followup.send(f"✅ Added {n} file(s). Stock: {stock_count(service)}", ephemeral=True)
    await announce_restock(service, n)


@stock_g.command(name="bulk", description="Upload a .txt — every line becomes one stock item")
@app_commands.autocomplete(service=service_ac)
async def stock_bulk(i: discord.Interaction, service: str, file: discord.Attachment):
    if not service_exists(service):
        return await i.response.send_message("Unknown service.", ephemeral=True)
    if file.size > 2_000_000:
        return await i.response.send_message("File too big (max 2 MB).", ephemeral=True)
    await i.response.defer(ephemeral=True)
    raw = (await file.read()).decode("utf-8", errors="ignore")
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    db.executemany("INSERT INTO stock(service, text) VALUES(?,?)", [(service, l) for l in lines])
    db.commit()
    await i.followup.send(f"✅ Added {len(lines)} items. Stock: {stock_count(service)}", ephemeral=True)
    await announce_restock(service, len(lines))


@stock_g.command(name="clear", description="Delete all stock of a service")
@app_commands.autocomplete(service=service_ac)
async def stock_clear(i: discord.Interaction, service: str):
    for r in q("SELECT path FROM stock WHERE service=? AND path IS NOT NULL", service):
        try:
            os.remove(r["path"])
        except OSError:
            pass
    ex("DELETE FROM stock WHERE service=?", service)
    await i.response.send_message("🗑️ Stock cleared.", ephemeral=True)


tree.add_command(stock_g)


@tree.command(name="stocks", description="Show what's currently in stock")
async def stocks(i: discord.Interaction):
    rows = q("SELECT name FROM services")
    if not rows:
        return await i.response.send_message("Nothing here yet.", ephemeral=True)
    e = discord.Embed(title="📊 Stock", color=discord.Color.blurple())
    for r in rows:
        c = stock_count(r["name"])
        e.add_field(name=r["name"], value=f"{'🟢' if c else '🔴'} {c}", inline=True)
    await i.response.send_message(embed=e)


# --------------------------------------------------------------------------- #
# /gen (for members)
# --------------------------------------------------------------------------- #
@tree.command(name="gen", description="Generate an item from a service (sent via DM)")
@app_commands.autocomplete(service=service_ac)
async def gen(i: discord.Interaction, service: str):
    svc = q1("SELECT * FROM services WHERE name=?", service)
    if not svc:
        return await i.response.send_message("Unknown service.", ephemeral=True)
    last = q1("SELECT ts FROM gens WHERE user_id=? AND service=?", i.user.id, service)
    wait = svc["gen_cooldown_min"] * 60 - (time.time() - (last["ts"] if last else 0))
    if wait > 0:
        return await i.response.send_message(
            f"⏳ Cooldown: try again in {int(wait // 60) + 1} min.", ephemeral=True
        )
    items = q("SELECT * FROM stock WHERE service=?", service)
    if not items:
        return await i.response.send_message("🔴 Out of stock.", ephemeral=True)
    item = random.choice(items)
    try:
        await send_item(i.user, item, f"✅ **{service}**")
    except discord.Forbidden:
        return await i.response.send_message("❌ Open your DMs and try again.", ephemeral=True)
    consume(item)
    ex("INSERT OR REPLACE INTO gens(user_id, service, ts) VALUES(?,?,?)", i.user.id, service, time.time())
    await i.response.send_message("📬 Sent to your DMs!", ephemeral=True)


# --------------------------------------------------------------------------- #
# /drop (per-channel auto drops)
# --------------------------------------------------------------------------- #
drop_g = app_commands.Group(
    name="drop", description="Auto-drop settings", default_permissions=ADMIN, guild_only=True
)


@drop_g.command(name="setup", description="Auto-drop a service in a channel (random, min 3h)")
@app_commands.describe(
    min_hours="Minimum hours between drops (3 or more)",
    max_hours="Maximum hours between drops",
    mode="consume = item is removed after dropping, repeat = item stays in stock",
)
@app_commands.choices(
    mode=[app_commands.Choice(name="consume", value="consume"), app_commands.Choice(name="repeat", value="repeat")]
)
@app_commands.autocomplete(service=service_ac)
async def drop_setup(
    i: discord.Interaction,
    channel: discord.TextChannel,
    service: str,
    min_hours: float = 3.0,
    max_hours: float = 12.0,
    mode: app_commands.Choice[str] = None,
):
    if min_hours < MIN_HOURS:
        return await i.response.send_message(f"❌ Minimum is {MIN_HOURS:g} hours.", ephemeral=True)
    if max_hours < min_hours:
        return await i.response.send_message("❌ max_hours must be ≥ min_hours.", ephemeral=True)
    if not service_exists(service):
        return await i.response.send_message("Unknown service.", ephemeral=True)
    m = mode.value if mode else "consume"
    ex(
        "INSERT OR REPLACE INTO drops(channel_id, service, min_h, max_h, mode, next_ts, enabled) VALUES(?,?,?,?,?,?,1)",
        channel.id, service, min_hours, max_hours, m, next_time(min_hours, max_hours),
    )
    await i.response.send_message(
        f"✅ `{service}` will drop in {channel.mention} every {min_hours:g}–{max_hours:g}h (random, {m}).",
        ephemeral=True,
    )


@drop_g.command(name="remove", description="Stop a service from dropping in a channel")
@app_commands.autocomplete(service=service_ac)
async def drop_remove(i: discord.Interaction, channel: discord.TextChannel, service: str):
    ex("DELETE FROM drops WHERE channel_id=? AND service=?", channel.id, service)
    await i.response.send_message("🗑️ Removed.", ephemeral=True)


@drop_g.command(name="toggle", description="Pause / resume a channel's drop")
@app_commands.autocomplete(service=service_ac)
async def drop_toggle(i: discord.Interaction, channel: discord.TextChannel, service: str):
    d = q1("SELECT * FROM drops WHERE channel_id=? AND service=?", channel.id, service)
    if not d:
        return await i.response.send_message("No such drop.", ephemeral=True)
    new = 0 if d["enabled"] else 1
    ex("UPDATE drops SET enabled=? WHERE id=?", new, d["id"])
    await i.response.send_message("▶️ Resumed." if new else "⏸️ Paused.", ephemeral=True)


@drop_g.command(name="list", description="Show all drops and when the next one is")
async def drop_list(i: discord.Interaction):
    rows = q("SELECT * FROM drops")
    if not rows:
        return await i.response.send_message("No drops set up.", ephemeral=True)
    lines = [
        f"• <#{d['channel_id']}> · `{d['service']}` · {d['min_h']:g}–{d['max_h']:g}h · {d['mode']} · "
        f"{'next <t:%d:R>' % int(d['next_ts']) if d['enabled'] else 'paused'}"
        for d in rows
    ]
    await i.response.send_message("\n".join(lines), ephemeral=True)


@drop_g.command(name="now", description="Trigger a drop right now (testing)")
@app_commands.autocomplete(service=service_ac)
async def drop_now(i: discord.Interaction, channel: discord.TextChannel, service: str):
    d = q1("SELECT * FROM drops WHERE channel_id=? AND service=?", channel.id, service)
    if not d:
        return await i.response.send_message("No such drop configured.", ephemeral=True)
    ok = await run_drop(d)
    await i.response.send_message("✅ Dropped." if ok else "❌ Nothing to drop (empty stock?).", ephemeral=True)


tree.add_command(drop_g)

# --------------------------------------------------------------------------- #
# /createchannel + /setperms
# --------------------------------------------------------------------------- #
def build_overwrites(guild, private, can_send, targets):
    ow = {}
    if private:
        ow[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
    elif not can_send:
        ow[guild.default_role] = discord.PermissionOverwrite(send_messages=False)
    for t in targets:
        if t is not None:
            ow[t] = discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=can_send
            )
    ow[guild.me] = discord.PermissionOverwrite(
        view_channel=True, send_messages=True, attach_files=True, embed_links=True
    )
    return ow


@tree.command(name="createchannel", description="Create a channel with permissions (and optional auto-drop)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.describe(
    private="Hide from @everyone",
    can_send="Allowed roles/users can type (off = read-only drop channel)",
    service="Optional: start auto-dropping this service here",
)
@app_commands.autocomplete(service=service_ac)
async def createchannel(
    i: discord.Interaction,
    name: str,
    private: bool = True,
    role: Optional[discord.Role] = None,
    role2: Optional[discord.Role] = None,
    user: Optional[discord.Member] = None,
    can_send: bool = False,
    category: Optional[discord.CategoryChannel] = None,
    service: Optional[str] = None,
    min_hours: float = 3.0,
    max_hours: float = 12.0,
):
    if service:
        if not service_exists(service):
            return await i.response.send_message("Unknown service.", ephemeral=True)
        if min_hours < MIN_HOURS or max_hours < min_hours:
            return await i.response.send_message(
                f"❌ min_hours must be ≥ {MIN_HOURS:g} and max_hours ≥ min_hours.", ephemeral=True
            )
    try:
        ch = await i.guild.create_text_channel(
            name,
            category=category,
            overwrites=build_overwrites(i.guild, private, can_send, [role, role2, user]),
            reason=f"/createchannel by {i.user}",
        )
    except discord.Forbidden:
        return await i.response.send_message("❌ I need the Manage Channels permission.", ephemeral=True)
    msg = f"✅ Created {ch.mention}"
    if service:
        ex(
            "INSERT OR REPLACE INTO drops(channel_id, service, min_h, max_h, mode, next_ts, enabled) VALUES(?,?,?,?,?,?,1)",
            ch.id, service, min_hours, max_hours, "consume", next_time(min_hours, max_hours),
        )
        msg += f" · `{service}` drops every {min_hours:g}–{max_hours:g}h"
    await i.response.send_message(msg, ephemeral=True)


@tree.command(name="setperms", description="Allow or remove a role/user on a channel")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def setperms(
    i: discord.Interaction,
    channel: discord.TextChannel,
    role: Optional[discord.Role] = None,
    user: Optional[discord.Member] = None,
    view: bool = True,
    send: bool = False,
):
    target = role or user
    if target is None:
        return await i.response.send_message("Pick a role or a user.", ephemeral=True)
    await channel.set_permissions(
        target, view_channel=view, read_message_history=view, send_messages=send
    )
    await i.response.send_message(f"✅ Updated {target.mention} on {channel.mention}.", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env")
    bot.run(TOKEN)
