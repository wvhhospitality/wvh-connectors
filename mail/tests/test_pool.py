"""One mailbox, one conversation at a time.

A mail server answers in order on a single connection. Before 0.2.1 the pool handed
the same connection to several callers at once (the server runs four at a time, and
one tool asks the same mailbox several questions in parallel), and their answers got
crossed: "SSL: BAD_LENGTH", "WRONG_VERSION_NUMBER", failed searches. Every name here
is made up and no mail server is involved.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from wvh_mail_mcp import imap_client as mail

STUDIO = SimpleNamespace(email="robin@example.com")
HOME = SimpleNamespace(email="robin@example.org")


class Line:
    """Stands in for one connection. Notices if two callers are ever on it together."""

    def __init__(self, name: str = "line") -> None:
        self.name = name
        self.busy = 0
        self.crossed = False
        self.calls = 0
        self.hung_up = False
        self._guard = threading.Lock()

    def talk(self, seconds: float = 0.01) -> None:
        with self._guard:
            self.busy += 1
            self.calls += 1
            if self.busy > 1:
                self.crossed = True
        time.sleep(seconds)
        with self._guard:
            self.busy -= 1

    def logout(self) -> None:
        self.hung_up = True


class Pool(mail.IMAPPool):
    """The real pool, except that opening a connection hands out a Line."""

    def __init__(self) -> None:
        super().__init__("robin-mail")
        self.opened: list[Line] = []

    def _acquire(self, account):
        with self._lock:
            self._reap_idle()
            pooled = self._connections.get(account.email)
            if pooled is None:
                line = Line(f"{account.email}#{len(self.opened) + 1}")
                self.opened.append(line)
                pooled = mail._Pooled(client=line)
                self._connections[account.email] = pooled
            pooled.last_used = time.time()
            return pooled


def _together(count, work):
    start = threading.Barrier(count)
    errors: list[BaseException] = []

    def run(n):
        start.wait()
        try:
            work(n)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(n,)) for n in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return errors


def test_callers_on_one_mailbox_take_turns():
    pool = Pool()

    def work(_n):
        with pool.connection(STUDIO) as line:
            line.talk()
            line.talk()

    assert _together(8, work) == []
    (line,) = pool.opened
    assert line.calls == 16
    assert not line.crossed
    assert pool._connections[STUDIO.email].users == 0


def test_different_mailboxes_dont_wait_on_each_other():
    pool = Pool()
    both_inside = threading.Barrier(2, timeout=5)

    def work(n):
        with pool.connection(STUDIO if n == 0 else HOME):
            both_inside.wait()  # only passes if the two are inside at the same moment

    assert _together(2, work) == []
    assert len(pool.opened) == 2


def test_one_caller_may_open_the_same_mailbox_again():
    pool = Pool()
    with pool.connection(STUDIO) as outer:
        with pool.connection(STUDIO) as inner:
            assert inner is outer
            inner.talk(0)
    assert pool._connections[STUDIO.email].users == 0


def test_a_broken_connection_is_replaced_for_those_waiting():
    pool = Pool()
    inside = threading.Event()
    go = threading.Event()
    seen: list[str] = []

    def first():
        try:
            with pool.connection(STUDIO):
                inside.set()
                go.wait(5)
                raise OSError("line went dead")
        except OSError:
            pass

    def second():
        inside.wait(5)
        with pool.connection(STUDIO) as line:
            seen.append(line.name)

    a, b = threading.Thread(target=first), threading.Thread(target=second)
    a.start()
    b.start()
    inside.wait(5)
    time.sleep(0.05)  # let the second caller queue up behind the first
    go.set()
    a.join(5)
    b.join(5)
    assert pool.opened[0].hung_up
    assert seen == [pool.opened[1].name]  # the waiting caller got a new line, not the dead one
    assert not pool.opened[1].hung_up


def test_a_late_failure_cant_close_someone_elses_new_connection():
    pool = Pool()
    with pool.connection(STUDIO):
        pass
    old = pool._connections[STUDIO.email]
    pool._drop(STUDIO.email)  # closed, e.g. by the idle sweep
    with pool.connection(STUDIO):
        pass
    new = pool._connections[STUDIO.email]
    pool._drop(STUDIO.email, old)  # the old caller reports its failure late
    assert pool._connections[STUDIO.email] is new
    assert not new.client.hung_up


def test_the_idle_sweep_leaves_a_connection_in_use_alone(monkeypatch):
    pool = Pool()
    monkeypatch.setattr(mail, "IDLE_TIMEOUT_SECONDS", 0.01)
    with pool.connection(STUDIO) as line:
        time.sleep(0.05)  # idle for longer than the limit, but still held
        with pool.connection(HOME):  # opening another mailbox runs the sweep
            pass
        assert not line.hung_up
        assert STUDIO.email in pool._connections
    time.sleep(0.05)
    with pool.connection(HOME):  # now nobody holds it, so the sweep closes it
        pass
    assert line.hung_up
