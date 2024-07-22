import asyncio
import os
import re
from datetime import datetime
from datetime import timezone as tz
from functools import partial
from html import escape
from pathlib import Path
from traceback import format_exc

from app.db import Database
from telethon import events
from telethon import TelegramClient
from telethon import types
from telethon import utils
from telethon.sessions.sqlite import sqlite3
from telethon.tl.custom.message import Message

Event = events.NewMessage.Event

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

    async def startup(self, init=None, *init_args, **init_kwargs):
        me = await self.tg.get_me()
        username = me.username  # type: ignore
        if init is not None:
            await init(*init_args, **init_kwargs)
        await self.tg.catch_up()
        await self.log(
            "Connected as @%s" % username)

    async def log(self, msg, file=None, *args, **kwargs):
        fdate = datetime.now(tz=tz.utc).isoformat()
        msg = "%s\n%s" % (fdate, msg)
        if file:
            msg = "%s\nfile:%s" % (msg, file)
        print(msg)
        return await self.tg.send_message(
            self.log_channel, msg,
            file=file,  # type: ignore
            parse_mode="html", *args, **kwargs)

    def run(self, init=None, *init_args, **init_kwargs):
        with self.tg:
            self.tg.loop.run_until_complete(self.startup(
                init=init, *init_args, **init_kwargs))
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

tw_username: str
tw_post_id: int
tg_msg_by: int
tg_msg_at: int
tg_msg_chat: int
tg_msg_id: int
tg_msg_topic: int | None
url: str | None
topic_chat: int
topic_id: int
topic_name: str

async def init_db(dbfile, debug=False, delete_before=False):
    await db.connect(dbfile, debug=debug, delete_before=delete_before)
    async with db.tx() as tx:
        await tx.execute(
            "PRAGMA foreign_keys = ON")
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
            "CREATE TABLE IF NOT EXISTS topics ("
            "topic_chat INTEGER NOT NULL, "
            "topic_id INTEGER NOT NULL, "
            "topic_name TEXT NOT NULL)"
        )
        await tx.execute(
            "CREATE TABLE IF NOT EXISTS raid_topics ("
            "topic_chat INTEGER NOT NULL UNIQUE, "
            "topic_id INTEGER NOT NULL, "
            "FOREIGN KEY (topic_chat) "
            "REFERENCES topics (topic_chat), "
            "FOREIGN KEY (topic_id) "
            "REFERENCES topics (topic_id))"
        )
        await tx.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tw_posts "
            "ON tw_posts (tw_username, tw_post_id, tg_msg_chat)")
        await tx.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_topics "
            "ON topics (topic_chat, topic_id)")
        async with tx.execute("SELECT * FROM topics") as cur:
            async for row in cur:
                chat_id, topic_id, topic_name = row
                _topicid2name_cache[(chat_id, topic_id)] = topic_name
        async with tx.execute("SELECT * FROM raid_topics") as cur:
            async for row in cur:
                chat_id, topic_id = row
                raid_topics[chat_id] = topic_id

chat_types = {}
async def get_chat_type(event: Event):
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

async def first_reply(event: Event,
                      stop_at=None):
    while event.id is not stop_at:
        if not event.is_reply:
            return None
        event = await event.get_reply_message()
    return event

_topicid2name_cache = {}

async def topicid2name(event: Event,
                       topic_id):
    key = (event.chat_id, topic_id)
    if key in _topicid2name_cache:
        return _topicid2name_cache[key]
    if topic_id == 0:
        topic_name = "General"
    else:
        topic_name = (await first_reply(event, stop_at=topic_id)
                      ).action.title  # type: ignore
    try:
        async with db.tx() as tx:
            await tx.execute(
                "INSERT INTO topics (topic_chat, topic_id, topic_name) "
                "VALUES (?, ?, ?)",
                (*key, topic_name))
        _topicid2name_cache[key] = topic_name
        return topic_name
    except sqlite3.IntegrityError:
        try:
            return _topicid2name_cache[key]
        except KeyError:
            await hr.log("key not found: %s " % key)

async def get_topic(event: Event):
    chat_type = await get_chat_type(event)
    if chat_type != "topics":
        return None, None
    msg: Message = event.message
    reply_to = msg.reply_to
    if not reply_to or not reply_to.forum_topic:  # type: ignore
        topic_id = 0
        topic_name = await topicid2name(event, topic_id)
        return topic_id, topic_name
    topic_id = reply_to.reply_to_msg_id  # type: ignore
    try:
        reply_to_top_id = reply_to.reply_to_top_id  # type: ignore
        if reply_to_top_id:
            topic_id = reply_to_top_id
        else:
            topic_id = reply_to.reply_to_msg_id  # type: ignore
    except AttributeError:
        ...
    topic_name = await topicid2name(event, topic_id)
    return topic_id, topic_name

async def extract_info(event: Event):
    tg_msg_by = event.sender_id
    tg_msg_at = int((event.date or datetime.now(tz=tz.utc)).timestamp())
    tg_msg_chat = event.chat_id
    real_chat_id, _ = utils.resolve_id(tg_msg_chat)
    tg_msg_topic, topic_name = await get_topic(event)
    tg_msg_id = event.id
    url = None
    if not event.is_private:
        url = f"https://t.me/c/{real_chat_id}/{tg_msg_id}"
    return (tg_msg_by, tg_msg_at,
            tg_msg_chat, (tg_msg_topic, topic_name), tg_msg_id,
            url)

twitter_url_pattern = re.compile(
    "<a href=\"(?P<href>"
    r"(?:https?://)?(?:x|twitter).com?/"
    r"(?P<tw_username>[^/]*)/status/"
    r"(?P<tw_post_id>\d+)([/?]?\S*)?"
    ")\">(?:.*?(?=</a>))</a>",
    re.MULTILINE | re.DOTALL | re.IGNORECASE
)

parse_mode = utils.sanitize_parse_mode("html")
def to_html(event: Event):
    return parse_mode.unparse(event.raw_text, event.entities)  # type: ignore

def from_html(text):
    return parse_mode.parse(text)  # type: ignore

inc_link_template = '<a href="%%s">%s</a>'
dup_template = inc_link_template % 'Marked as duplicate'
link_template = inc_link_template % '%s'
tw_url_template = "https://x.com/%s/status/%s"
quote_template = "<blockquote>%s</blockquote>"

hr = HalfRed(username=os.environ["TG_BOT_USERNAME"],
             api_id=os.environ["TG_API_ID"],
             api_hash=os.environ["TG_API_HASH"],
             bot_token=os.environ["TG_BOT_TOKEN"],
             log_channel=os.environ["TG_LOG_CHANNEL"])

@hr.tg.on(events.Raw)  # type: ignore
async def _(event: Event):
    try:
        msg = event.message
        if not isinstance(msg, types.MessageService):
            return
        action = msg.action
        if not isinstance(action, types.MessageActionTopicEdit):
            return
        topic_name = action.title

        chat_id = utils.get_peer_id(
            types.PeerChannel(msg.peer_id.channel_id))  # type: ignore
        topic_id = msg.reply_to.reply_to_msg_id  # type: ignore
        key = chat_id, topic_id
        _topicid2name_cache[key] = topic_name
        async with db.tx() as tx:
            await tx.execute(
                "INSERT INTO topics (topic_chat, topic_id, topic_name) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT (topic_chat, topic_id) "
                "DO UPDATE SET topic_name = ?",
                (*key, topic_name, topic_name))
    except Exception:
        ...

missing = object()

_permissions_cache = {}
async def get_permissions(chat, user):
    key = (chat, user)
    if key in _permissions_cache:
        permissions, ttl = _permissions_cache[key]
        if ttl < datetime.now().timestamp():
            return permissions
    permissions = await hr.tg.get_permissions(chat, user)
    ttl = datetime.now().timestamp() + 5 * 60
    _permissions_cache[key] = permissions, ttl
    return permissions

async def has_permission(event: Event, **perms):
    chat_id, sender_id = event.chat_id, event.sender_id
    permissions = await get_permissions(
        chat_id, sender_id)
    for perm_name, perm_value in perms.items():
        user_perm = getattr(permissions, perm_name, missing)
        if user_perm is missing:
            raise AttributeError("Permission %s is missing" %
                                 perm_name)
        if user_perm != perm_value:
            await event.reply(
                "\n\n".join([
                    "<b>Permission denied:</b>",
                    ("<i>To perform this action, "
                     "you need the following permissions</i>:"),
                    "\n".join(
                        "  - <b>%s</b>" % (
                            perm_name if perm_value
                            else "<i>not</i> %s" % perm_name)
                        for perm_name, perm_value in perms.items()
                    ),
                ]),
                parse_mode="html")
            return False
    return True

@hr.cmd
async def _(event: Event):
    text = to_html(event)
    if not text:
        return
    tg_msg_by, tg_msg_at, tg_msg_chat, topic, tg_msg_id, url = await extract_info(event)
    tg_msg_topic, topic_name = topic
    print(f"{topic_name=}")
    print(f"{url=}")
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
                    "INSERT INTO tw_posts ("
                    "tw_username, "
                    "tw_post_id, "
                    "tg_msg_by, "
                    "tg_msg_at, "
                    "tg_msg_chat, "
                    "tg_msg_id, "
                    "tg_msg_topic, "
                    "url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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

raid_topics = {}
@hr.cmd(pattern="/set_raid_topic")
async def set_raid_topic(event: Event):
    if await get_chat_type(event) != "topics":
        return await event.reply("\n\n".join([
            "<b>Error:</b>",
            ("/set_raid_topic <i>can only be used in "
             "group chats with topics enabled.</i>"),
        ]),
            parse_mode="html")
    if not await has_permission(event,
                                is_admin=True, change_info=True):
        return
    topic_id, topic_name = await get_topic(event)
    async with db.tx() as tx:
        await tx.execute(
            "INSERT INTO raid_topics (topic_chat, topic_id) "
            "VALUES (?, ?) "
            "ON CONFLICT (topic_chat) "
            "DO UPDATE SET topic_id = ?",
            (event.chat_id, topic_id, topic_id))
    raid_topics[event.chat_id] = topic_id
    return await event.reply(
        "<i>Raid topic set to #%s</i>" % topic_name,
        parse_mode="html")

def main():
    hr.run(init=init_db, dbfile=dbfile)

if __name__ == "__main__":
    main()
