import asyncio
import textwrap as tw
import traceback as tb
from contextlib import asynccontextmanager
from functools import partial
from functools import wraps
from itertools import chain
from pathlib import Path

import aiosqlite
from aiosqlite.context import contextmanager as aioscontextmanager

def repr_arg(arg):
    if callable(arg):
        return arg.__qualname__
    return repr(arg)

def repr_args(args, kwargs):
    return ", ".join(chain(
        (f"{repr_arg(a)}" for a in args),
        (f"{k}={repr_arg(v)}" for k, v in kwargs.items())))

def repr_call(func, *args, _indent='', **kwargs):
    return tw.indent(f"{func.__qualname__}({repr_args(args, kwargs)})", _indent)

def debuggable(func, proxy_root, func_is_dispatcher=False, indent=''):
    def wrapper(m, *a, debug=None, showresult=False, **kw):
        debug = proxy_root.debug if debug is None else debug
        if debug:
            if func_is_dispatcher:
                print(repr_call(m, *a, _indent=indent, **kw))
            else:
                print(repr_call(func, m, *a, _indent=indent, **kw))
        res = func(m, *a, **kw)
        if debug and showresult:
            print(tw.indent(f"->{repr_arg(res)}", indent))
        return res
    return wrapper

class AioSqliteError(Exception):
    ...

@asynccontextmanager
async def FakeLock():
    yield

class Database:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._no_lock = FakeLock
        self._con: aiosqlite.Connection = None  # type: ignore

    async def connect(self, db_file, debug=False, delete_before=False):
        assert self._con is None, "Database already connected"
        if debug:
            print(
                f"Database.connect({str(db_file)!r}, {debug=!r}, {delete_before=!r})")
        db_file = Path(db_file)
        wal_file = Path(str(db_file) + "-wal")
        shm_file = Path(str(db_file) + "-shm")
        if delete_before:
            db_file.unlink(missing_ok=True)
            wal_file.unlink(missing_ok=True)
            shm_file.unlink(missing_ok=True)
            print("Deleted db")
        self._con = await aiosqlite.connect(db_file, isolation_level=None)
        self.debug = debug
        # inject debuggable wrapper to the self._con execute* methods
        _debuggable = partial(debuggable, proxy_root=self,
                              func_is_dispatcher=True, indent="| ")
        self._con._execute = _debuggable(self._con._execute)  # type: ignore
        # executemany autocommits before execting, interferes with self.tx()
        self._con.executemany = None  # type: ignore
        # patches self._con.cursor to produce a Cursor with debuggable execute* methods

        class Cursor(aiosqlite.Cursor):
            @classmethod
            def __prepare__(cls, name, bases, **kwargs):
                sup = super().__prepare__(name, bases, **kwargs)
                return {'_execute': _debuggable(sup['execute'])}

            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.executemany = None  # type: ignore

        @aioscontextmanager
        @wraps(self._con.cursor)
        async def customcursor(self, *a, **kw):
            return Cursor(*a, **kw)
        self._con.cursor = customcursor  # type: ignore
        if debug:
            print("Configuring database")
        async with self._lock:
            async with self._con.execute("pragma journal_mode=wal") as cur:
                row = await cur.fetchone()
                assert row is not None
                mode, = row
                assert mode == 'wal', "Could not enable WAL mode..."
            await self._con.commit()
        if debug:
            print("Configured database")

    @asynccontextmanager
    async def tx(self, debug=None,
                 lock=False, print_tb=False):
        debug = self.debug if debug == None else debug
        async with self._lock if lock else self._no_lock():
            try:
                if debug:
                    print("Tx started")
                await self._con.execute("begin")
                yield self._con
                await self._con.commit()
                if debug:
                    print("Tx committed")
            except Exception:
                await self._con.rollback()
                if print_tb:
                    (tb.format_exc())
                if debug:
                    print("Tx reverted")
                raise
