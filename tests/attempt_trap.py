"""AttemptTrap: prove "nothing happened" instead of asserting it.

Records (and for process launches and network lookups also VETOES) what the code run inside the `with`
attempts, from the interpreter's audit events (PEP 578), the mechanism `tests/path_recorder.py` documents: raised
by the C implementation, so independent of the Python-level name a caller used. A veto means a control never runs
a real command or leaves the machine. File mutations are recorded, not vetoed, so a control can show a file
really appears.

Listens ONLY on the thread that opened it: a pytest-xdist worker has other threads whose I/O is none of this
trap's business. An audit hook cannot be removed; it is installed once and returns at once while no trap is open
(same cost as path_recorder). Python 3.11, 3.13 and 3.14 were measured; CI covers 3.10 to 3.13, and the enabled
controls assert every channel is non-empty, so an event missing on some version turns those controls red instead
of silently passing the closed runs.

A shell launch is classified by the command it runs, not by the shell: `Popen(..., shell=True)` is audited as
`/bin/sh -c <text>` and `os.system(<text>)` as the text, so the first word of the text is the program. That is
the first word only (a compound line such as `cd x && claude` is classified `cd`); the launch itself is always
recorded in `processes`, so a closed-gate check cannot be fooled by the classification."""
import os
import pathlib
import shlex
import sys
import threading

PROCESS_EVENTS = frozenset({"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty"})
NETWORK_EVENTS = frozenset({"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
                            "socket.connect", "socket.sendto", "socket.sendmsg", "http.client.connect", "urllib.Request",
                            "smtplib.connect", "ftplib.connect"})
MUTATION_EVENTS = frozenset({"os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.link", "os.symlink", "shutil.copyfile",
                             "shutil.copytree", "shutil.move", "shutil.rmtree"})
MODEL_PROGRAMS = frozenset({"claude", "codex", "cursor-agent"})
SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

_ACTIVE = []
_LOCK = threading.Lock()
_INSTALLED = False


class Vetoed(Exception):
    """Raised from inside the audited call: a process launch or network attempt the trap refused."""


def _first_word(text):
    """The basename of the program a shell command line starts with."""
    text = os.fsdecode(text)
    try:
        words = shlex.split(text)
    except ValueError:
        words = text.split()
    return os.path.basename(words[0]) if words else None


def _program(event, args):
    """The basename of the program an event would start, or None. Never raises."""
    try:
        if event == "os.system":
            return _first_word(args[0])
        if event == "subprocess.Popen":
            argv = [os.fsdecode(a) for a in args[1]]
        elif event == "os.spawn":
            argv = [os.fsdecode(a) for a in args[2]]
        elif event in ("os.exec", "os.posix_spawn"):
            argv = [os.fsdecode(a) for a in args[1]]
        else:
            return None
        head = os.path.basename(argv[0])
        for position, word in enumerate(argv[1:], 1):          # sh -c TEXT, bash -lc TEXT
            if word.startswith("-") and not word.startswith("--") and word.endswith("c") and word[1:].isalpha():
                if head in SHELLS and position + 1 < len(argv):
                    return _first_word(argv[position + 1])
        return head or None
    except Exception:                      # classification must never break the trap
        return None


def _writes(mode, flags):
    if isinstance(mode, str) and any(c in mode for c in "wax+"):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _hook(event, args):
    if not _ACTIVE:
        return
    me = threading.get_ident()
    for trap in list(_ACTIVE):
        if trap._owner == me:
            trap._see(event, args)


def _install():
    global _INSTALLED
    with _LOCK:
        if not _INSTALLED:
            sys.addaudithook(_hook)
            _INSTALLED = True


class AttemptTrap:
    def __init__(self, model_programs=()):
        self.processes, self.models, self.network, self.writes = [], [], [], []
        self._models = MODEL_PROGRAMS | frozenset(model_programs)
        self._owner = None

    def __enter__(self):
        _install()
        self._owner = threading.get_ident()
        _ACTIVE.append(self)
        return self

    def __exit__(self, *exc):
        _ACTIVE.remove(self)
        return False

    @property
    def quiet(self):
        return not (self.processes or self.models or self.network or self.writes)

    def _see(self, event, args):
        if event in PROCESS_EVENTS:
            program = _program(event, args)
            self.processes.append((event, program))
            if program in self._models:
                self.models.append(program)
            raise Vetoed("process launch vetoed: %s %s" % (event, program))
        if event in NETWORK_EVENTS:
            self.network.append((event, str(args[0])[:60] if args else ""))
            raise Vetoed("network attempt vetoed: " + event)
        if event == "open":
            try:
                path, mode, flags = args[0], args[1], args[2]
            except (IndexError, TypeError):
                return
            if _writes(mode, flags) and isinstance(path, (str, bytes, os.PathLike)):
                self.writes.append(os.fsdecode(path))
        elif event in MUTATION_EVENTS and args and isinstance(args[0], (str, bytes, os.PathLike)):
            self.writes.append(os.fsdecode(args[0]))


def snapshot(*roots):
    """Every path under each root -> (is_dir, mtime_ns, size). The same lstat walk as tests/test_script_help.py `_snapshot`."""
    seen = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                p = pathlib.Path(dirpath) / name
                try:
                    st = p.lstat()
                except OSError:
                    continue
                seen[p.as_posix()] = (p.is_dir(), st.st_mtime_ns, st.st_size)
    return seen


def tree_diff(before, after):
    """(added, removed, changed), each sorted."""
    return (sorted(set(after) - set(before)), sorted(set(before) - set(after)),
            sorted(k for k in set(before) & set(after) if before[k] != after[k]))
