# tests/test_godzilla_isolation.py
"""
Godzilla must not be able to corrupt the tracked reference hull.

mlt933w.exe overwrites off1.mlt / out.mlt in place and litters its working
directory.  Run in the project root, any crash, timeout or user cancel leaves
the reference geometry half-written -- observed in practice as off1.mlt and
out.mlt ending up byte-identical, which silently breaks every downstream sweep
(41 station parse -> garbage -> 31 test failures).

launch_godzilla therefore runs in a scratch subdirectory and promotes
off1.mlt / out.mlt back only on a clean exit.
"""
from __future__ import annotations

import hashlib
import os

import pytest

from services.hull_service import HullService


def _md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


@pytest.fixture
def fake_root(tmp_path):
    """A project root with a stand-in binary and a tracked reference hull."""
    (tmp_path / "mlt933w.exe").write_bytes(b"MZ fake")
    (tmp_path / "in.mlt").write_text("seed input\r\n", newline="")
    (tmp_path / "off1.mlt").write_text("REFERENCE OFFSETS\r\n", newline="")
    (tmp_path / "out.mlt").write_text("REFERENCE REPORT\r\n", newline="")
    return tmp_path


def _reference_md5s(root):
    return {f: _md5(root / f) for f in ("off1.mlt", "out.mlt")}


def test_failed_run_leaves_reference_hull_untouched(fake_root, monkeypatch):
    before = _reference_md5s(fake_root)

    class _Proc:
        returncode = 1
        def poll(self): return 1
        def communicate(self): return (b"", b"boom")

    def _popen(cmd, cwd=None, **kw):
        # Simulate Godzilla half-writing its outputs then dying.
        (open(os.path.join(cwd, "off1.mlt"), "w")).write("HALF WRITTEN")
        (open(os.path.join(cwd, "out.mlt"), "w")).write("HALF WRITTEN")
        return _Proc()

    monkeypatch.setattr("services.hull_service.subprocess.Popen", _popen)
    ok = HullService().launch_godzilla(str(fake_root), log_fn=lambda *_: None)

    assert ok is False
    assert _reference_md5s(fake_root) == before, (
        "a FAILED Godzilla run corrupted the tracked reference hull"
    )


def test_cancelled_run_leaves_reference_hull_untouched(fake_root, monkeypatch):
    import threading
    before = _reference_md5s(fake_root)
    stop = threading.Event(); stop.set()

    class _Proc:
        returncode = None
        def poll(self): return None
        def terminate(self): pass
        def communicate(self): return (b"", b"")

    def _popen(cmd, cwd=None, **kw):
        (open(os.path.join(cwd, "off1.mlt"), "w")).write("PARTIAL")
        return _Proc()

    monkeypatch.setattr("services.hull_service.subprocess.Popen", _popen)
    ok = HullService().launch_godzilla(str(fake_root), stop_event=stop,
                                       log_fn=lambda *_: None)
    assert ok is False
    assert _reference_md5s(fake_root) == before


def test_successful_run_promotes_outputs(fake_root, monkeypatch):
    class _Proc:
        returncode = 0
        def poll(self): return 0
        def communicate(self): return (b"ok", b"")

    def _popen(cmd, cwd=None, **kw):
        open(os.path.join(cwd, "off1.mlt"), "w").write("NEW OFFSETS")
        open(os.path.join(cwd, "out.mlt"), "w").write("NEW REPORT")
        return _Proc()

    monkeypatch.setattr("services.hull_service.subprocess.Popen", _popen)
    ok = HullService().launch_godzilla(str(fake_root), log_fn=lambda *_: None)

    assert ok is True
    assert (fake_root / "off1.mlt").read_text() == "NEW OFFSETS"
    assert (fake_root / "out.mlt").read_text() == "NEW REPORT"


def test_clean_exit_without_outputs_is_a_failure(fake_root, monkeypatch):
    # Exit 0 but nothing produced: must NOT be reported as success, and must
    # not touch the reference.
    before = _reference_md5s(fake_root)

    class _Proc:
        returncode = 0
        def poll(self): return 0
        def communicate(self): return (b"", b"")

    monkeypatch.setattr("services.hull_service.subprocess.Popen",
                        lambda cmd, cwd=None, **kw: _Proc())
    ok = HullService().launch_godzilla(str(fake_root), log_fn=lambda *_: None)
    assert ok is False
    assert _reference_md5s(fake_root) == before


def test_run_happens_in_scratch_not_the_root(fake_root, monkeypatch):
    seen = {}

    class _Proc:
        returncode = 0
        def poll(self): return 0
        def communicate(self): return (b"", b"")

    def _popen(cmd, cwd=None, **kw):
        seen["cwd"] = cwd
        open(os.path.join(cwd, "off1.mlt"), "w").write("x")
        open(os.path.join(cwd, "out.mlt"), "w").write("y")
        return _Proc()

    monkeypatch.setattr("services.hull_service.subprocess.Popen", _popen)
    HullService().launch_godzilla(str(fake_root), log_fn=lambda *_: None)

    assert seen["cwd"] != str(fake_root), "Godzilla ran directly in the root"
    assert seen["cwd"].endswith(HullService.SCRATCH_DIRNAME)
    # the binary must have been staged alongside it
    assert os.path.exists(os.path.join(seen["cwd"], "mlt933w.exe"))
