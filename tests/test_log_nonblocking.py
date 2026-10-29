import json
import sys


def _import_mintbot_isolated():
    """Import mintbot without leaking its .env load into later tests."""
    import os
    saved = dict(os.environ)
    try:
        import mintbot
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mintbot


mintbot = _import_mintbot_isolated()


def _setup(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mintbot, "LOG_FILE", tmp_path / "mintbot.log")
    mintbot.log_setup()


def test_deferred_lines_identical_and_flushed(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, capsys)
    times = iter([1.5, 2.5, 3.5, 4.5])
    monkeypatch.setattr(mintbot.time, "time", lambda: next(times))
    nested = {"a": [1, 2]}
    cases = [
        ("fivemin_eval", dict(slug="x", price=0.53, size=10.0, ok=True, none=None, n=3)),
        ("health", dict(feeds=["a", "b"], obj=object.__class__)),
        ("nested", dict(d=nested)),
        ("plain", {}),
    ]
    expected = []
    for ts, (event, kw) in zip([1.5, 2.5, 3.5, 4.5], cases):
        mintbot.log_event(event, **kw)
        expected.append(json.dumps({"ts": ts, "event": event, **kw}, default=str))
    nested["a"].append(99)  # mutation after logging must not leak into the line
    mintbot._stop_log_listener()
    mintbot._stop_log_listener()  # idempotent
    out = capsys.readouterr().out.splitlines()
    assert out == expected
    assert (tmp_path / "mintbot.log").read_text().splitlines() == expected


def test_plain_string_logging_unchanged(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, capsys)
    import logging
    logging.getLogger("mintbot").info("hello %s", "world")
    mintbot._stop_log_listener()
    assert capsys.readouterr().out.splitlines() == ["hello world"]
