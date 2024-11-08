import asyncio
import logging
import os
import re
import textwrap as tw
from datetime import datetime
from datetime import timezone as tz
from functools import partial
from functools import wraps
from html import escape
from pathlib import Path
from pprint import pformat
from traceback import format_exc

import aiosqlite
from telethon import events
from telethon import errors
from telethon import TelegramClient
from telethon import types
from telethon import utils
from telethon.sessions.sqlite import sqlite3
from telethon.tl.custom.message import Message

logging.basicConfig(format='[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s',
                    level=logging.WARNING)

Event = events.NewMessage.Event

datadir = Path("data")
sessions = datadir / "sessions"
sessions.mkdir(exist_ok=True, parents=True)

dbfile = datadir / "twitter_posts.sqlite3"

pf = partial(pformat, sort_dicts=False, width=35)

sleep_time = 10

def event2dict(obj):
    if isinstance(obj, dict):
        return {k: event2dict(v) for k, v in obj.items() if v}
    if isinstance(obj, (list, tuple)):
        return type(obj)(v for v in obj if v)
    if hasattr(obj, "to_dict"):
        return event2dict(obj.to_dict())
    return obj

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
        try:
            self.log_channel = int(log_channel)
        except ValueError:
            self.log_channel = log_channel
        self.commands = {}
        me = self.tg.loop.run_until_complete(
            self.tg.get_me())
        assert me
        self.me = me

    async def startup(self, init=None, *init_args, **init_kwargs):
        username = self.me.username
        if init is not None:
            await init(*init_args, **init_kwargs)
        await self.tg.catch_up()
        await self.log(
            "Connected as @%s" % username)

    async def log(self, text, file=None, *args, **kwargs):
        fdate = datetime.now(tz=tz.utc).ctime()
        text = "%s\n%s" % (fdate, text)
        if file:
            text = "%s\nfile:%s" % (text, file)
        print(text)
        return await self.tg.send_message(
            self.log_channel, text,
            file=file,  # type: ignore
            parse_mode="html", *args, **kwargs)

    @staticmethod
    def displayname(chat, showid=False, username=False, clickable=False):
        if isinstance(chat, int):
            if showid:
                return f"<code>{chat}</code>"
            else:
                return ""
        try:
            if hasattr(chat, "username") and chat.username is not None and username:
                name = f"@{chat.username}"
            elif hasattr(chat, "title") and chat.title is not None:
                name = chat.title
                if clickable:
                    name = link_template % ("t.me/%s" % chat.username, name)
            else:
                fullname = chat.first_name + \
                    (f" {chat.last_name}" if chat.last_name else "")
                name = f"<a href='tg://user?id={chat.id}'>{fullname}</a>"
            if showid:
                return f"{name}(<code>{chat.id}</code>)"
            else:
                return name
        except Exception as e:
            print(f"Error {e}:\n{format_exc()}")
            return f"<code>{chat.id}</code>"

    async def get_title(self, event, **kwargs):
        real_chat_id, _ = utils.resolve_id(event.chat_id)
        topic_id, topic_name = await get_topic(event)
        if topic_name is None:
            topic_rep = ""
        else:
            topic_rep = topic_template % (
                real_chat_id, topic_id, "#" + topic_name)
        chat_and_topic = "".join((self.displayname(await event.get_chat(), clickable=True, **kwargs),
                     topic_rep))
        if event.chat_id == event.sender_id:
            return "<b>[%s]</b>:" % chat_and_topic
        return "<b>[%s]\n%s</b>:" % (
            chat_and_topic,
            self.displayname(await event.get_sender(), **kwargs, clickable=True))

    async def log_msg(self, event, msg):
        title = await self.get_title(event, showid=True)
        await self.log("%s\n%s\n└➤%s" % (title, to_html(event),
                                         tw.indent(to_html(msg), "  ")),
                       file=event.media if event.photo else None)

    def run(self, init=None, *init_args, **init_kwargs):
        with self.tg:
            self.tg.loop.run_until_complete(self.startup(
                init=init, *init_args, **init_kwargs))
            self.tg.run_until_disconnected()

    def cmd(self, func=None, /, on=None, **params):
        if func is None:
            return partial(self.cmd, on=on, **params)
        on = on or partial(events.NewMessage, incoming=True)

        @self.tg.on(on(**params))
        @wraps(func)
        async def wrapper(event: Event):
            try:
                print(event.message.date.ctime())
            except AttributeError:
                print(datetime.now().ctime())
            return await self.tryf(func, event)
        return wrapper

    async def tryf(self, coro, *args, allow_exc=True, allow_excs=None,
                   **kwargs):
        try:
            return await coro(*args, **kwargs)
        except Exception as e:
            if isinstance(e, events.StopPropagation):
                raise
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

raid_topic: int

async def init_db():
    async with aiosqlite.connect(dbfile) as conn:
        await conn.execute(
            "PRAGMA foreign_keys = ON")
        async with conn.execute("PRAGMA journal_mode = WAL") as cur:
            row = await cur.fetchone()
            assert row is not None
            mode, = row
            assert mode == 'wal', "Could not enable WAL mode..."
        await conn.execute(
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
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS topics ("
            "topic_chat INTEGER NOT NULL, "
            "topic_id INTEGER NOT NULL, "
            "topic_name TEXT NOT NULL)"
        )
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS raid_topics ("
            "topic_chat INTEGER NOT NULL UNIQUE, "
            "topic_id INTEGER NOT NULL, "
            "FOREIGN KEY (topic_chat) "
            "REFERENCES topics (topic_chat), "
            "FOREIGN KEY (topic_id) "
            "REFERENCES topics (topic_id))"
        )
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS linked_chats ("
            "linked_chat_id INTEGER NOT NULL, "
            "chat_id INTEGER NOT NULL, "
            "FOREIGN KEY (chat_id) "
            "REFERENCES topics (topic_chat),"
            "UNIQUE (chat_id, linked_chat_id))"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tw_posts "
            "ON tw_posts (tw_username, tw_post_id, tg_msg_chat)")
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_topics "
            "ON topics (topic_chat, topic_id)")
        async with conn.execute("SELECT * FROM topics") as cur:
            async for row in cur:
                chat_id, topic_id, topic_name = row
                _topicid2name_cache[(chat_id, topic_id)] = topic_name
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_linked_chats "
            "ON linked_chats (chat_id, linked_chat_id)")
        async with conn.execute("SELECT * FROM raid_topics") as cur:
            async for row in cur:
                chat_id, topic_id = row
                raid_topics[chat_id] = topic_id
        async with conn.execute("SELECT * FROM linked_chats") as cur:
            async for row in cur:
                linked_chat_id, chat_id = row
                linked_chats[linked_chat_id] = chat_id
        await conn.commit()

chat_types = {}
async def get_chat_type(event: Event):
    msg: Message = event.message if isinstance(event, Event) else event
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
            return event
        event = await event.get_reply_message()
    return event

_topicid2name_cache = {}

async def topicid2name(event: Event,
                       topic_id):
    key = (event.chat_id, topic_id)
    if key in _topicid2name_cache:
        return _topicid2name_cache[key]
    if topic_id == 1:
        topic_name = "General"
    else:
        topic_name = (await first_reply(event, stop_at=topic_id)
                      ).action.title  # type: ignore
    try:
        async with aiosqlite.connect(dbfile) as conn:
            await conn.execute(
                "INSERT INTO topics (topic_chat, topic_id, topic_name) "
                "VALUES (?, ?, ?)",
                (*key, topic_name))
            await conn.commit()
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
    msg: Message = event.message if isinstance(event, Event) else event
    reply_to = msg.reply_to
    if not reply_to or not reply_to.forum_topic:  # type: ignore
        topic_id = 1
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
        url = msg_url_template % (real_chat_id, tg_msg_id)
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

link_template = '<a href="%s">%s</a>'
dup_template = link_template % ('%s', 'Marked as duplicate')
tw_url_template = "https://x.com/%s/status/%s"
quote_template = "<blockquote>%s</blockquote>"
msg_url_template = "https://t.me/c/%s/%s"
topic_template = link_template % (msg_url_template, '%s')

hr = HalfRed(username=os.environ["TG_BOT_USERNAME"],
             api_id=os.environ["TG_API_ID"],
             api_hash=os.environ["TG_API_HASH"],
             bot_token=os.environ["TG_BOT_TOKEN"],
             log_channel=os.environ["TG_LOG_CHANNEL"])

@hr.cmd(on=events.Raw)
async def _raw(event: Event):
    if not hasattr(event, "message"):
        return
    msg = event.message
    if not isinstance(msg, types.MessageService):
        return
    action = msg.action
    if not isinstance(action, (types.MessageActionTopicEdit,
                               types.MessageActionTopicCreate)):
        return
    topic_name = action.title
    if topic_name is None:
        # ignore topics close
        return
    chat_id = utils.get_peer_id(
        types.PeerChannel(msg.peer_id.channel_id))  # type: ignore
    if msg.reply_to:
        topic_id = msg.reply_to.reply_to_msg_id  # type: ignore
    else:
        topic_id = msg.id  # type: ignore
    key = chat_id, topic_id
    _topicid2name_cache[key] = topic_name
    async with aiosqlite.connect(dbfile) as conn:
        await conn.execute(
            "INSERT INTO topics (topic_chat, topic_id, topic_name) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT (topic_chat, topic_id) "
            "DO UPDATE SET topic_name = ?",
            (*key, topic_name, topic_name))
        await conn.commit()

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
            res = await event.reply(
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
            await asyncio.sleep(sleep_time)
            await hr.tryf(hr.tg.delete_messages,
                          chat_id, (event.id, res.id))
            return False
    return True

@hr.cmd(pattern=rf"^/ignore_duplicate(?:@{hr.username})?(?:\s+(?P<msg>.*))?")
async def _ignore_duplicate(event: Event):
    if not await has_permission(event, is_admin=True, change_info=True):
        return
    m = event.pattern_match
    if not m:
        return
    g = m.groupdict()
    msg = g["msg"]
    if not msg:
        res = await event.reply("\n\n".join([
            "<b>Error:</b>",
            "/ignore_duplicate <i>needs a message after the command</i>",
        ]),
            parse_mode="html")
        await asyncio.sleep(sleep_time)
        return await hr.tryf(hr.tg.delete_messages,
                             event.chat_id, (event.id, res.id))
    await dedup(event, ignore_duplicate=True,
                ignore_prefix=r"/ignore_duplicate")
    await hr.tryf(event.delete)
    raise events.StopPropagation

@hr.cmd
async def _dedup(event: Event):
    real_chat_id, _ = utils.resolve_id(event.chat_id)
    if real_chat_id in linked_chats:
        linked_chat_id = linked_chats[real_chat_id]
        raid_topic = raid_topics.get(linked_chat_id, None)
        if raid_topic:
            msg = await hr.tg.send_message(
                linked_chat_id,
                "%s\n%s" % (
                    await hr.get_title(event),
                    to_html(event),
                ),
                reply_to=raid_topic,
                parse_mode="html")
            return await dedup(msg, ignore_duplicate=True, skip_repost=True)
    return await dedup(event)

async def dedup(event: Event, ignore_duplicate=False,
                ignore_prefix=None, skip_repost=False):
    text = to_html(event)
    if ignore_prefix:
        text = text.lstrip(ignore_prefix).lstrip("@%s" % hr.username).strip()
    if not text:
        return
    if event.chat_id not in raid_topics:
        return
    tg_msg_by, tg_msg_at, tg_msg_chat, topic, tg_msg_id, url = await extract_info(event)
    tg_msg_topic, topic_name = topic
    event_topic = tg_msg_topic
    raid_topic = raid_topics[tg_msg_chat]
    response_ok = []
    response_duplicate = []
    match = ""
    has_duplicates, has_more_text, has_url = False, False, False
    last_end, start, end = 0, 0, 0
    for m in twitter_url_pattern.finditer(text):
        if not m:
            continue
        has_url = True
        start, end = m.span()
        match = text[start:end]
        g = m.groupdict()
        tw_username = g["tw_username"]
        tw_post_id = g["tw_post_id"]
        try:
            async with aiosqlite.connect(dbfile) as conn:
                await conn.execute(
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
                await conn.commit()
            response_ok.append(text[last_end:end])
            more_text = text[last_end:start]
            if more_text.strip():
                has_more_text = True
        except sqlite3.IntegrityError:
            has_duplicates = True
            async with aiosqlite.connect(dbfile) as conn:
                cur = await conn.execute(
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
            # if end != 0 and last_end != end:
            more_text = text[last_end:start]
            if more_text.strip():
                has_more_text = True
            if event.is_private:
                if ignore_duplicate:
                    response_ok.append(text[last_end:end])
                else:
                    response_ok.append("%s%s" % (
                        more_text,
                        dup_template % escape(
                            tw_url_template % (
                                tw_username,
                                tw_post_id))))
            elif ignore_duplicate:
                response_ok.append(text[last_end:end])
            else:
                response_ok.append("%s%s" % (
                    more_text,
                    dup_template % escape(url)))
            response_duplicate.append("%s\n" % (
                match))
        last_end = end
    has_more_text = has_more_text or match and end != len(text)
    response = "".join((*response_ok, text[end:]))
    response = "\n".join((await hr.get_title(event), response))
    if skip_repost:
        return
    if event_topic == raid_topic:
        if has_duplicates:
            if has_more_text or ignore_duplicate:
                await event.reply(response, parse_mode="html")
            if not ignore_duplicate:
                duplicates = await event.reply(
                    "\n\n".join([
                        "Duplicate posts:",
                        *(r.strip() for r in response_duplicate),
                        "This message will self-destruct in %ss" % sleep_time
                    ]),
                    parse_mode="html")
                await asyncio.sleep(sleep_time)
                await hr.tryf(hr.tg.delete_messages,
                              event.chat_id, (event.id, duplicates.id))

    elif has_url and (not has_duplicates or ignore_duplicate):
        await hr.tg.send_message(
            event.chat_id, response,
            reply_to=raid_topic,
            parse_mode="html")

raid_topics: dict[int, int] = {}
@hr.cmd(pattern="/set_raid_topic")
async def _set_raid_topic(event: Event):
    if await get_chat_type(event) != "topics":
        res = await event.reply("\n\n".join([
            "<b>Error:</b>",
            ("/set_raid_topic <i>can only be used in "
             "group chats with topics enabled.</i>"),
        ]),
            parse_mode="html")
        await asyncio.sleep(sleep_time)
        return await hr.tryf(hr.tg.delete_messages,
                             event.chat_id, (event.id, res.id))
    if not await has_permission(event, is_admin=True, change_info=True):
        return
    topic_id, topic_name = await get_topic(event)
    async with aiosqlite.connect(dbfile) as conn:
        await conn.execute(
            "INSERT INTO raid_topics (topic_chat, topic_id) "
            "VALUES (?, ?) "
            "ON CONFLICT (topic_chat) "
            "DO UPDATE SET topic_id = ?",
            (event.chat_id, topic_id, topic_id))
        await conn.commit()
    raid_topics[event.chat_id] = topic_id
    real_chat_id, _ = utils.resolve_id(event.chat_id)
    res = await event.reply(
        "<i>Raid topic</i> set to <b>%s</b>" % topic_template % (
            real_chat_id, topic_id, topic_name),
        parse_mode="html")
    await hr.log_msg(event, res)
    await asyncio.sleep(sleep_time)
    await hr.tryf(hr.tg.delete_messages,
                  event.chat_id, (event.id, res.id))

@hr.cmd(pattern="/raid_topic")
async def _raid_topic(event: Event):
    if await get_chat_type(event) != "topics":
        res = await event.reply("\n\n".join([
            "<b>Error:</b>",
            ("/raid_topic <i>can only be used in "
             "group chats with topics enabled.</i>"),
        ]),
            parse_mode="html")
        await asyncio.sleep(sleep_time)
        return await hr.tryf(hr.tg.delete_messages,
                             event.chat_id, (event.id, res.id))
    if not await has_permission(event, is_admin=True, change_info=True):
        return
    chat_id = event.chat_id
    if chat_id not in raid_topics:
        res = await event.reply(
            "\n\n".join((
                "Raid topic is not set",
                "Use /set_raid_topic in a topic to set it as raid topic",
            )),
            parse_mode="html")
        await asyncio.sleep(sleep_time)
        return await hr.tryf(hr.tg.delete_messages,
                             chat_id, (event.id, res.id))
    topic_id = raid_topics[chat_id]
    topic_name = await topicid2name(event, topic_id)
    real_chat_id, _ = utils.resolve_id(chat_id)
    res = await event.reply(
        "<i>Raid topic</i> is <b>%s</b>" % topic_template % (
            real_chat_id, topic_id, topic_name),
        parse_mode="html")
    await hr.log_msg(event, res)
    await asyncio.sleep(sleep_time)
    await hr.tryf(hr.tg.delete_messages,
                  chat_id, (event.id, res.id))

linked_chats: dict[int, int] = {}
pattern_linked_chat = (
    r"(?:\s+(?P<chat_info>"
        r"(?P<chat_id>\d+)"
        r"|(?P<chat_username>@\w+)"
        r"|(?P<chat_link>(?:https?://)?t\.me/\S+)"
    "))?"
)
un = r"(?P<undo>un)?"
@hr.cmd(pattern=rf"/{un}link_chat(?:@{hr.username})?{pattern_linked_chat}")
async def _link_chat(event: Event):
    if not await has_permission(event, is_admin=True, change_info=True):
        return
    g = event.pattern_match.groupdict()
    undo = bool(g["undo"])
    chat_info = g["chat_info"]
    chat_link = chat_info
    if g["chat_id"]:
        linked_chat_id = int(g["chat_id"])
        chat = await hr.tg.get_entity(linked_chat_id)
        chat_link = f"t.me/{chat.username}"
    elif chat_info:
        if "+" in chat_info:
            return await event.reply("Linked chat must be public")
        if "@" in chat_info:
            chat = await hr.tg.get_entity(chat_info)
            chat_link = f"t.me/{chat.username}"
        if g["chat_link"]:
            chat_link = g["chat_link"]
        chat = await hr.tg.get_entity(chat_info)
        try:
            await hr.tg.get_permissions(chat.id, hr.me.id)
        except errors.UserNotParticipantError:
            return await event.reply("Bot must be member of this chat")
        linked_chat_id = chat.id
    else:
        return await event.reply("Invalid chat id")
    event_chat_id, _ = utils.resolve_id(event.chat_id)
    if linked_chat_id == event_chat_id:
        return await event.reply("Cannot link to the same chat")
    action = "Linked"
    try:
        async with aiosqlite.connect(dbfile) as conn:
            if undo:
                action = "Un" + action.lower()
                await conn.execute(
                    "DELETE FROM linked_chats "
                    "WHERE chat_id = ? "
                    "AND linked_chat_id = ?",
                    (event.chat_id, linked_chat_id))
                await conn.commit()
                linked_chats.pop(linked_chat_id)
            else:
                await conn.execute(
                    "INSERT INTO linked_chats (linked_chat_id, chat_id) "
                    "VALUES (?, ?)",
                    (linked_chat_id, event.chat_id))
                await conn.commit()
                linked_chats[linked_chat_id] = event.chat_id
    except (sqlite3.IntegrityError, KeyError):
        txt = f"Chat already {action.lower()}"
        msg = await event.reply(txt)
        return await hr.log_msg(event, msg)
    linked_chat_title = hr.displayname(chat)
    txt = f"{action} chat %s" % link_template % (
            chat_link, linked_chat_title)
    msg = await event.reply(txt, parse_mode="html")
    await hr.log_msg(event, msg)

def main():
    hr.run(init=init_db)

if __name__ == "__main__":
    main()
