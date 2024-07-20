#!/usr/bin/env python3
import re
import sys
import textwrap as tw
from collections import defaultdict
from collections import deque
from pathlib import Path
from subprocess import run as run_shell

def build_usage(argspec):
    def usage():
        args = []
        descs = []
        for short, spec in argspec.items():
            pattern = ("%s%s" %
                       ("%s|%s" % (short, spec["long"]) if "long" in spec
                        else short,
                        " *%s_ARGS" % spec["action"].upper()
                        if "var" in spec and spec["var"] == "section"
                        else ""))
            args.append(pattern)
            default = ""
            if "init" in spec and spec["var"] != "section":
                default = "\ndefaults to %s" % spec["init"]
            msg: str = spec["help"]
            msg = "\n".join(tw.wrap("%s: %s" % (pattern, msg))) + default
            if "\n" in msg:
                firstline, nl, rest = msg.partition("\n")
                msg = firstline + nl + tw.indent(rest, "    ")
            descs.append(msg)
        print("Usage: run.py *ARGS %s" % " ".join("[%s]" % a for a in args))
        desc = "\n".join(descs)
        print(tw.indent(desc, "    "))
        exit()
    return usage

default_arg_spec = {
    "-v": {"long": "--verbose",
           "var": "verbosity", "init": 1,
           "action": lambda x: x + 1,
           "help": "Increase verbosity"},
    "-q": {"long": "--quiet",
           "var": "verbosity",
           "action": lambda x: x - 1,
           "help": "Decrease verbosity"},
    "-h": {"long": "--help",
           "help": "Show this help message"},
    "--": {"var": "section", "init": "args",
           "action": "extra",
           "help": "Stop processing arguments"},
}

missing = object()
def build_cli(argspec):
    argspec |= default_arg_spec
    argspec['-h']["action"] = build_usage(argspec)
    arg_router = {}
    state = {}
    for short, spec in argspec.items():
        data: dict[str, object] = spec.copy()
        try:
            data.pop("help")
        except KeyError:
            raise KeyError("%r has no definition for 'help'. %r" %
                           (short, {short: spec}))
        try:
            arg_router[data.pop("long")] = data
        except KeyError:
            ...
        arg_router[short] = data
        var, init, action = tuple(data.pop(arg, missing)
                                  for arg in ("var", "init", "action"))
        if var is not missing:
            data["var"] = var
            if init is not missing:
                state[var] = init
            if action is not missing:
                data["action"] = action
        else:
            if init is not missing:
                raise Exception("'init' present but no 'var' set")
            if action is not missing:
                data["action"] = action
    return state, arg_router

def parse_args(*args, arg_router, start_state):
    state = start_state.copy()
    for arg in args:
        if state["section"] == "extra":
            state["extra"].append(arg)
        elif arg in arg_router:
            update = arg_router[arg]
            action = update.get("action", missing)
            var = update.get("var", missing)
            if var is missing:
                if action is not missing:
                    action()
            else:
                if callable(action):
                    state[var] = action(state.get(var, None))
                else:
                    state[var] = action
                    if var == "section" and action not in state:
                        # action is the section
                        state[action] = []
        else:
            section = state["section"]
            if section not in state:
                state[section] = []
            state[section].append(arg)
    state.pop("section")
    return state

start_state, arg_router = build_cli({
    "-r": {"long": "--run",
           "var": "section",
           "action": "run",  # reuse args section for run
           "help": "Run python/shell scripts"},
    "-t": {"long": "--type-check",
           "var": "section",
           "action": "type_check",
           "help": "Run pyright on your python scripts"},
    "-T": {"long": "--test",
           "var": "section",
           "action": "test",
           "help": "Run pytest"},
    "-w": {"long": "--watch",
           "var": "watch", "init": False,
           "action": True,
           "help": "Re-run on filesystem changes"},
    "-k": {"long": "--keep-running",
           "var": "keep_running", "init": False,
           "action": True,
           "help": "Keep running further commands even after an error is encountered"},
    "-C": {"long": "--no-clear",
           "var": "clear", "init": True,
           "action": False,
           "help": "Don't clear the screen before running commands"},
    "-D": {"long": "--no-date",
           "var": "date", "init": True,
           "action": False,
           "help": "Don't show the date before running commands"},
})

def path2module(path: str):
    if path.endswith(".py"):
        return path.rpartition(".")[0].replace("/", ".")
    return path

def src2tests(path: str):
    testpath = re.sub(r"^(?:\./)?src/(.*).py$",
                      r"tests/\1_test.py", path)
    if Path(testpath).exists():
        return testpath
    print("\033[33mTest not found: %s \033[0m" % testpath)
    return path

def load_cmd_section(state, cmd_args, section):
    if section in state:
        cmd_args[section].extend(state[section])

def run_cmds(state):
    cmd_joiner = ";" if state["keep_running"] else " && "
    watch = state["watch"]
    verbosity = state["verbosity"]
    cmd_args = defaultdict(list)
    paths = state.get("args", [])
    extra_args = state.get("extra", [])
    load_cmd_section(state, cmd_args, 'run')
    load_cmd_section(state, cmd_args, 'type_check')
    load_cmd_section(state, cmd_args, 'test')
    commands = deque()
    py = ["python", "-m"]
    run_args = cmd_args.get("run", [])
    type_checks = []
    tests = []
    for path in paths:
        if path.endswith(".py"):
            if 'type_check' in cmd_args:
                type_checks.append(path)
            if 'test' in cmd_args:
                tests.append(src2tests(path))
            commands.append(
                [*py, path2module(path), *run_args, *extra_args])
        else:
            commands.append(
                ['./' + path.removeprefix("./"), *run_args, *extra_args])
    type_check_args = cmd_args.get("type_check", [])
    test_args = cmd_args.get("test", [])
    if 'type_check' in cmd_args:
        commands.insert(0, [*py, 'pyright', *type_check_args, *type_checks])
    if 'test' in cmd_args:
        commands.insert(0, [*py, 'pytest', *test_args, *tests])
    if verbosity > 0:
        # interleaves printf calls to show the command
        for idx, cmd in enumerate(commands.copy()):
            commands.insert(idx * 2, ["printf", fr"'> \033[34;1m%s\033[0m\n'",
                                      repr(" ".join(cmd))])
    if state["date"]:
        commands.insert(0, ["printf", r"'\033[0m'"])
        commands.insert(0, ["date"])
        commands.insert(0, ["printf", r"'\033[34;1m'"])
    if state["clear"]:
        commands.insert(0, ["clear"])
    try:
        if watch:
            cmdstr = cmd_joiner.join(
                " ".join(arg for arg in subcmd)
                for subcmd in commands)
            ignore = "__pycache__/*;.direnv/*;.git/;*_cache/"
            pattern = "*.py;*.json;*.txt;*.sh;*.yaml"
            watch_cmd = ["watchmedo", "auto-restart", "-R", "-p", pattern, "-i", ignore,
                         "--debounce-interval", ".3", "--no-restart-on-command-exit", "--", "bash", "-c", cmdstr]
            run_shell(watch_cmd)
        else:
            for cmd in commands:
                run_shell([arg.removesuffix("'").removeprefix("'")
                           for arg in cmd])
    except KeyboardInterrupt:
        print("Exiting")

state = parse_args(
    *sys.argv[1:], arg_router=arg_router, start_state=start_state)
run_cmds(state)
