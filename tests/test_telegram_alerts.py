import json
import telegram_alerts as ta


def row(sym="AAA", tf="D", d=1, **kw):
    r = dict(sym=sym, tf=tf, dir=d, ps="3-2u", pat="3-2u", st="IN-FORCE", fresh=True,
             partial=False, chop=False, pz=False, rev=True, lc=100.0, trig=98.0, sd="2026-10-29",
             tg=[[102.0, "x", 2, 2.0, "o"], [103.0, "x", 3, 3.0, "o"], [105.0, "x", 5, 5.0, "o"]])
    r.update(kw)
    return r


def panels(sym="AAA", d=1):
    c = "u" if d == 1 else "d"
    return {sym: {"fd": {"D": c, "W": c, "M": c}}}


def sel(rows, p=None, **kw):
    return ta.select(rows, p if p is not None else panels(), **kw)


def test_accepts_clean_setup():
    assert len(sel([row()])) == 1


def test_rejections():
    bad = [row(fresh=False), row(partial=True), row(st="STILL GOOD"), row(chop=True),
           row(tf="M"), row(lc=5.0), row(tg=[[102, "x", 2, 2.0, "o"]] * 2),
           row(tg=[[102, "x", 2, 2.0, "x"]] * 3), row(tg=[[101, "x", 1, 0.5, "o"]] * 3)]
    for b in bad:
        assert sel([b]) == [], b


def test_ftfc_must_match_direction():
    assert sel([row()], {"AAA": {"fd": {"D": "u", "W": "d", "M": "u"}}}) == []
    assert sel([row(d=-1)], panels(d=1)) == []
    assert len(sel([row(d=-1)], panels(d=-1))) == 1
    assert sel([row()], {}) == []


def test_ranking_and_cap():
    rows = [row("A", tg=[[1, "x", 1, 1.5, "o"]] * 3), row("B", tg=[[1, "x", 1, 4.0, "o"]] * 3)]
    p = {**panels("A"), **panels("B")}
    assert [r["sym"] for r in sel(rows, p)] == ["B", "A"]
    assert len(sel(rows, p, top=1)) == 1


def test_message_has_tv_link_and_escapes():
    msgs = ta.format_message(sel([row()]), "2026-10-29")
    assert "tradingview.com/chart/?symbol=AAA" in msgs[0] and "LONG" in msgs[0]
    assert all(len(m) < 4096 for m in msgs)


def test_message_splits_when_long():
    rows = sel([row(f"S{i}") for i in range(60)], {f"S{i}": panels()["AAA"] for i in range(60)}, top=60)
    assert len(ta.format_message(rows, "d")) > 1


def _setup(tmp_path):
    (tmp_path / "latest_targets.json").write_text(json.dumps({"scan_date": "2026-10-29", "rows": [row()]}))
    (tmp_path / "latest.json").write_text(json.dumps({"panels": panels()}))


def test_run_sends_once_and_dedupes(tmp_path):
    _setup(tmp_path)
    calls = []
    s = ta.run(tmp_path, token="t", chat_id="c", sender=lambda *a: calls.append(a))
    assert s["sent"] == 1 and len(calls) == 1 and (tmp_path / "telegram_sent.json").exists()
    s = ta.run(tmp_path, token="t", chat_id="c", sender=lambda *a: calls.append(a))
    assert s["sent"] == 0 and len(calls) == 1


def test_run_without_secrets_sends_nothing(tmp_path):
    _setup(tmp_path)
    s = ta.run(tmp_path)
    assert s["sent"] == 0 and not (tmp_path / "telegram_sent.json").exists()


def test_dry_run_does_not_record(tmp_path):
    _setup(tmp_path)
    ta.run(tmp_path, dry_run=True)
    assert not (tmp_path / "telegram_sent.json").exists()


def test_failed_send_not_recorded(tmp_path):
    _setup(tmp_path)
    def boom(*a): raise RuntimeError("x")
    try:
        ta.run(tmp_path, token="t", chat_id="c", sender=boom)
    except RuntimeError:
        pass
    assert not (tmp_path / "telegram_sent.json").exists()


def _scan(**over):
    a = dict(ticker="ZZZ", tf="D", level=100.0, swing_date="2026-10-01", target=130.0,
             target_date="2026-10-29", magnitude=40.0, expected_move=29.0, risk=1.0)
    a.update(over)
    return {"panels": {"ZZZ": {"ft": "u"}}, "bf_active": {"long": [a], "short": []},
            "bf_signals": [{"date": "2026-10-29"}], "signals": []}


def test_magnitude_accepts_preset_match():
    r = ta.select_magnitude(_scan())
    assert len(r) == 1 and r[0]["rr"] > 2 and r[0]["room"] >= 10 and r[0]["fresh"]


def test_magnitude_rejections():
    assert ta.select_magnitude(_scan(target_date="2026-10-28")) == []        # not reclaimed today
    assert ta.select_magnitude(_scan(expected_move=5.0, risk=4.0)) == []     # R:R < 2 and room < 10%
    assert ta.select_magnitude(_scan(risk=20.0)) == []                      # move already > 1/3
    s = _scan(); s["panels"]["ZZZ"]["ft"] = "d"
    assert ta.select_magnitude(s) == []                                      # FTFC against
    s = _scan(); s["panels"] = {}
    assert ta.select_magnitude(s) == []                                      # FTFC unknown


def test_magnitude_latest_per_ticker():
    s = _scan()
    s["bf_active"]["long"].append(dict(s["bf_active"]["long"][0], level=90.0, target_date="2026-10-20"))
    assert len(ta.select_magnitude(s)) == 1


def test_weekly_pz_preset():
    ok = row(tf="W", pz=True, rev=True)
    assert len(ta.select_weekly_pz([ok], panels())) == 1
    assert len(ta.select_weekly_pz([row(tf="W", pz=True, rev=True, st="POTENTIAL 3", partial=True)], panels())) == 1
    for bad in (row(tf="D", pz=True), row(tf="W", pz=False), row(tf="W", pz=True, rev=False),
                row(tf="W", pz=True, st="STILL GOOD"),
                row(tf="W", pz=True, tg=[[102, "x", 2, 2.0, "x"]] * 3)):
        assert ta.select_weekly_pz([bad], panels()) == [], bad
    assert ta.select_weekly_pz([ok], {"AAA": {"fd": {"D": "u", "W": "d", "M": "u"}}}) == []


def test_discover_chat_id_oldest_private(monkeypatch):
    import io, json as j
    body = {"result": [{"message": {"chat": {"type": "group", "id": -5}}},
                       {"message": {"chat": {"type": "private", "id": 111}}},
                       {"message": {"chat": {"type": "private", "id": 222}}}]}
    monkeypatch.setattr(ta.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(j.dumps(body).encode()))
    assert ta.discover_chat_id("t") == "111"


def test_run_discovers_chat_id(tmp_path, monkeypatch):
    _setup(tmp_path)
    monkeypatch.setattr(ta, "discover_chat_id", lambda t: "42")
    got = []
    s = ta.run(tmp_path, token="t", sender=lambda tok, chat, m: got.append(chat))
    assert s["sent"] == 1 and got == ["42"]
