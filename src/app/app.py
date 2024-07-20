import asyncio
import os
from datetime import datetime
from datetime import timezone as tz
from functools import partial
from pathlib import Path
from traceback import format_exc

from telethon import events
from telethon import TelegramClient
from telethon.tl.custom.message import Message

datadir = Path("data")
sessions = datadir / "sessions"
sessions.mkdir(exist_ok=True, parents=True)

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
        print(msg)
        return await self.tg.send_message(
            self.log_channel, msg,
            file=file,  # type: ignore
            parse_mode="html", *args, **kwargs)

    def run(self, startup=None):
        with self.tg:
            loop = self.tg.loop
            loop.run_until_complete(self.startup())
            if startup is not None:
                loop.run_until_complete(startup())
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

def main():
    hr = HalfRed(username=os.environ["TG_BOT_USERNAME"],
                 api_id=os.environ["TG_API_ID"],
                 api_hash=os.environ["TG_API_HASH"],
                 bot_token=os.environ["TG_BOT_TOKEN"],
                 log_channel=os.environ["TG_LOG_CHANNEL"])

    @hr.cmd(pattern=r"")
    def grab_tweet(event):
        print(event)
    hr.run()

if __name__ == "__main__":
    main()
