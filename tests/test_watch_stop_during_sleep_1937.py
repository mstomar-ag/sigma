"""#1937: `state/watch.stop` is read during the sleep between ticks, not only after it."""
import importlib.util
import pathlib

LOOP = pathlib.Path(__file__).resolve().parents[1] / "skills" / "sigma-loop" / "scripts"


def _daemon():
    spec = importlib.util.spec_from_file_location("watch_daemon_1937", LOOP / "watch_daemon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Clock:
    """A fake clock: each sleep advances time, and may create the stop file at a chosen moment."""

    def __init__(self, stop, create_at=None):
        self.now, self.stop, self.create_at, self.slept = 0.0, stop, create_at, []

    def clock(self):
        return self.now

    def sleep(self, secs):
        self.slept.append(secs)
        self.now += secs
        if self.create_at is not None and self.now >= self.create_at:
            self.stop.write_text("")


def test_a_stop_raised_mid_sleep_ends_it_within_one_poll(tmp_path):
    d, stop = _daemon(), tmp_path / "watch.stop"
    c = Clock(stop, create_at=12)
    assert d.sleep_unless_stopped(stop, 900, poll=5, sleep=c.sleep, clock=c.clock) is True
    assert c.now <= 15 and c.now < 900      # honoured at the next poll after it appeared, not at 900


def test_without_a_stop_the_whole_interval_is_slept(tmp_path):
    d, stop = _daemon(), tmp_path / "watch.stop"
    c = Clock(stop)
    assert d.sleep_unless_stopped(stop, 12, poll=5, sleep=c.sleep, clock=c.clock) is False
    assert c.slept == [5, 5, 2]


def test_an_existing_stop_file_returns_before_sleeping(tmp_path):
    d, stop = _daemon(), tmp_path / "watch.stop"
    stop.write_text("")
    c = Clock(stop)
    assert d.sleep_unless_stopped(stop, 900, poll=5, sleep=c.sleep, clock=c.clock) is True
    assert c.slept == []


def test_the_default_poll_is_a_few_seconds_and_the_loop_uses_the_helper():
    d = _daemon()
    assert 0 < d.STOP_POLL_SECONDS <= 5
    src = (LOOP / "watch_daemon.py").read_text(encoding="utf-8")
    assert "sleep_unless_stopped(p.stop, sleep_for)" in src
    assert "time.sleep(sleep_for)" not in src
