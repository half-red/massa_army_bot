import asyncio
import os
import re
from datetime import datetime
from datetime import timezone as tz
from functools import partial
from html import escape
from pathlib import Path
from pprint import pformat
from traceback import format_exc

from app.db import Database
from telethon import events
from telethon import TelegramClient
from telethon import utils
from telethon.sessions.sqlite import sqlite3
from telethon.tl.custom.message import Message

datadir = Path("data")
sessions = datadir / "sessions"
sessions.mkdir(exist_ok=True, parents=True)

dbfile = datadir / "twitter_posts.sqlite3"
db = Database()

class HalfRed:
    tg: TelegramClient

    def __init__(self, username, api_id, api_hash, bot_token,
                 log_channel,
                 **clientparams) -> None:
        self.username = username
        self.tg = TelegramClient(sessions / username, api_id=api_id,
                                 api_hash=api_hash,
                                 **clientparams)
        self.tg.start(bot_token=bot_token)
        self.log_channel = log_channel
        self.commands = {}

    async def startup(self):
        me = await self.tg.get_me()
        username = me.username  # type: ignore
        await self.log(
            "Connected as @%s" % username)

    async def log(self, msg, file=None, *args, **kwargs):
        fdate = datetime.now(tz=tz.utc).isoformat()
        msg = "%s\n%s" % (fdate, msg)
        if file:
            msg = "%s\nfile:%s" % (msg, file)
        # print(msg)
        return await self.tg.send_message(
            self.log_channel, msg,
            file=file,  # type: ignore
            parse_mode="html", *args, **kwargs)

    def run(self, startup=None, *startup_args, **startup_kwargs):
        with self.tg:
            loop = self.tg.loop
            loop.run_until_complete(self.startup())
            if startup is not None:
                loop.run_until_complete(
                    startup(*startup_args, **startup_kwargs))
            self.tg.run_until_disconnected()

    def cmd(self, func=None, /, **params):
        if func is None:
            return partial(self.cmd, **params)
        return self.tg.on(events.NewMessage(**params))(func)

    async def tryf(self, coro, *args, allow_exc=True, allow_excs=None,
                   **kwargs):
        try:
            return await coro(*args, **kwargs)
        except Exception as e:
            txt = f"Error {e}:\n<pre><code class='language-python'>{format_exc()}</code></pre>"
            await self.log(txt)
            if not allow_exc and (allow_excs is None or type(e) in allow_excs):
                raise
            return e

pf = partial(pformat, width=30, indent=2, sort_dicts=False)

tw_username: str
tw_post_id: int
tg_msg_by: int
tg_msg_at: int
tg_msg_chat: int
tg_msg_id: int
tg_msg_topic: int | None
url: str

async def init_db(dbfile, delete_before=False):
    await db.connect(dbfile, debug=False, delete_before=delete_before)
    async with db.tx() as tx:
        await tx.execute(
            "CREATE TABLE IF NOT EXISTS tw_posts ("
            "tw_username TEXT NOT NULL, "
            "tw_post_id INTEGER NOT NULL, "
            "tg_msg_by INTEGER NOT NULL, "
            "tg_msg_at INTEGER NOT NULL, "
            "tg_msg_chat INTEGER NOT NULL, "
            "tg_msg_id INTEGER NOT NULL, "
            "tg_msg_topic INTEGER, "
            "url TEXT)"
        )
        await tx.execute(
            "CREATE UNIQUE INDEX idx_tw_posts "
            "ON tw_posts (tw_username, tw_post_id, tg_msg_chat);")

chat_types = {}
async def get_chat_type(event: events.NewMessage.Event):
    msg: Message = event.message
    c_id = event.chat_id
    if c_id in chat_types:
        return chat_types[c_id]
    chat = await event.get_chat()
    assert chat
    if msg.is_private:
        chat_types[c_id] = "private"
    elif chat.forum:
        chat_types[c_id] = "topics"
    else:
        chat_types[c_id] = "group"
    return chat_types[c_id]

async def get_topic(event: events.NewMessage.Event):
    chat_type = await get_chat_type(event)
    print(f"{chat_type=}")
    if chat_type != "topics":
        return None
    msg: Message = event.message
    if not msg.reply_to:
        return "General"

async def extract_info(event: events.NewMessage.Event):
    tg_msg_by = event.sender_id
    tg_msg_at = int((event.date or datetime.now(tz=tz.utc)).timestamp())
    tg_msg_chat = event.chat_id
    real_chat_id, _ = utils.resolve_id(tg_msg_chat)
    tg_msg_topic = await get_topic(event)
    tg_msg_id = event.id
    url = None
    if not event.is_private:
        url = f"https://t.me/c/{real_chat_id}/{tg_msg_id}"
    return (tg_msg_by, tg_msg_at,
            tg_msg_chat, tg_msg_topic, tg_msg_id,
            url)

twitter_url_pattern = re.compile(
    "<a href=\"(?P<href>"
    r"(?:https?://)?(?:x|twitter).com?/"
    r"(?P<tw_username>[^/]*)/status/"
    r"(?P<tw_post_id>\d+)([/?]?\S*)?"
    ")\">(?:.*?(?=</a>))</a>",
    re.MULTILINE | re.DOTALL | re.IGNORECASE
)

print(f"{twitter_url_pattern=}")

parse_mode = utils.sanitize_parse_mode("html")
def to_html(event):
    return parse_mode.unparse(event.raw_text, event.entities)

def from_html(text):
    return parse_mode.parse(text)

inc_link_template = '<a href="%%s">%s</a>'
dup_template = inc_link_template % 'Marked as duplicate'
link_template = inc_link_template % '%s'
tw_url_template = "https://x.com/%s/status/%s"
quote_template = "<blockquote>%s</blockquote>"

def main():
    hr = HalfRed(username=os.environ["TG_BOT_USERNAME"],
                 api_id=os.environ["TG_API_ID"],
                 api_hash=os.environ["TG_API_HASH"],
                 bot_token=os.environ["TG_BOT_TOKEN"],
                 log_channel=os.environ["TG_LOG_CHANNEL"])

    @hr.cmd
    async def _(event: events.NewMessage.Event):
        text = to_html(event)
        if not text:
            return
        tg_msg_by, tg_msg_at, tg_msg_chat, tg_msg_id, tg_msg_topic, url = await extract_info(event)
        response_ok = []
        response_duplicate = []
        has_duplicates = False
        last_end, start, end = 0, 0, 0
        for m in twitter_url_pattern.finditer(text):
            if not m:
                continue
            start, end = m.span()
            match = text[start:end]
            g = m.groupdict()
            tw_username = g["tw_username"]
            tw_post_id = g["tw_post_id"]
            try:
                async with db.tx() as tx:
                    await tx.execute(
                        "INSERT INTO tw_posts ( "
                        "tw_username, "
                        "tw_post_id, "
                        "tg_msg_by, "
                        "tg_msg_at, "
                        "tg_msg_chat, "
                        "tg_msg_id, "
                        "tg_msg_topic, "
                        "url) "
                        "VALUES ("
                        "?, ?, ?, ?, ?, ?, ?, ?"
                        ");",
                        (tw_username, tw_post_id,
                         tg_msg_by, tg_msg_at,
                         tg_msg_chat, tg_msg_topic, tg_msg_id,
                         url))
                response_ok.append(text[last_end:end])
            except sqlite3.IntegrityError:
                has_duplicates = True
                async with db.tx() as tx:
                    cur = await tx.execute(
                        "SELECT * "
                        "FROM tw_posts "
                        "WHERE tw_username = ? "
                        "AND tw_post_id = ? "
                        "AND tg_msg_chat = ?",
                        (tw_username, tw_post_id, tg_msg_chat)
                    )
                    row = await cur.fetchone()
                    assert row
                    (tw_username, tw_post_id,
                     tg_msg_by, tg_msg_at,
                     tg_msg_chat, tg_msg_id, tg_msg_topic,
                     url) = row
                if event.is_private:
                    response_ok.append(
                        text[last_end:start]
                        + dup_template % escape(
                            tw_url_template % (
                                tw_username,
                                tw_post_id)))
                else:
                    response_ok.append(
                        text[last_end:start]
                        + dup_template % escape(url)
                    )
                response_duplicate.append(
                    "%s\n" % (
                        match))
            last_end = end
        if has_duplicates:
            if response_ok:
                await event.reply(
                    "".join((*response_ok, text[end:])),
                    parse_mode="html")
            duplicates = await event.reply(
                "\n\n".join([
                    "Duplicate posts:",
                    *response_duplicate,
                    "This message will self-destruct in 10s"
                ]),
                parse_mode="html")
            await hr.tryf(event.delete)
            await asyncio.sleep(10)
            await hr.tryf(duplicates.delete)
    hr.run(startup=init_db, dbfile=dbfile, delete_before=True)

if __name__ == "__main__":
    main()
