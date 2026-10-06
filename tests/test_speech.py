import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import speech, caption


def _words(text, step=0.34):
    t, out = 0.3, []
    for w in text.split():
        out.append({"w": w, "start": t, "end": t + 0.3})
        t += step
    return out


def test_cues_are_short_and_ordered():
    cues = speech.build_cues(_words("So this is what they call a totally normal day in America, right? Absolutely unbelievable stuff honestly " * 3))
    assert cues
    for a, b in zip(cues, cues[1:]):
        assert a["end"] <= b["start"] + 0.01
    for c in cues:
        assert len(c["text"].replace("\n", " ")) <= 45
        assert c["text"].count("\n") <= 1


def test_cues_break_on_pause():
    w = _words("hello there friend")
    w += [{"w": "new", "start": w[-1]["end"] + 2, "end": w[-1]["end"] + 2.3}]
    assert len(speech.build_cues(w)) == 2


def test_ass_escapes_and_uppercases():
    ass = speech.build_ass([{"start": 0, "end": 2, "text": "hi {there}\nfriend"}], 720, 1280, 288)
    assert "HI (THERE)\\NFRIEND" in ass
    assert "PlayResX: 720" in ass


def test_clip_words_rebased():
    ws = _words("a b c d e f")
    out = speech.clip_words(ws, ws[2]["start"], ws[4]["end"])
    assert out[0]["start"] == 0 or abs(out[0]["start"]) < 1e-6


def test_parse_focus():
    r, rest = speech.parse_focus("12:30-18:00 and 45:10 to 52:00 the argument")
    assert r == [(750.0, 1080.0), (2710.0, 3120.0)]
    assert "argument" in rest


def test_pick_clips_extends_short(monkeypatch):
    segs = [{"start": i * 8.0, "end": i * 8.0 + 7.5, "text": "s"} for i in range(40)]
    monkeypatch.setattr(speech, "_chat_json", lambda s, u: {"clips": [{"start": 16, "end": 30, "title": "a", "reason": "r", "score": 80}]})
    out = speech.pick_clips({"segments": segs, "words": []}, 320)
    assert out and out[0]["end"] - out[0]["start"] >= 30


def test_credit_handle():
    assert caption.credit_handle({"uploader_id": "123", "channel": "real_user"}) == "real_user"
    assert caption.credit_handle({"source_url": "https://www.tiktok.com/@tt/video/1"}) == "tt"
    assert caption.credit_handle({"uploader_id": "x", "credit_ok": False}) == ""


def test_mask_cues():
    out = speech.mask_cues([{"start": 0, "end": 1, "text": "this is shit ok", "words": []}], ["ok"])
    assert "shit" not in out[0]["text"] and "*" in out[0]["text"]


def test_build_ass_highlight():
    cues = [{"start": 0, "end": 2, "text": "hello big world",
             "words": [{"w": "hello", "start": 0, "end": .6}, {"w": "big", "start": .6, "end": 1.2}, {"w": "world", "start": 1.2, "end": 2}]}]
    ass = speech.build_ass(cues, 1080, 1920, 300, style="highlight")
    assert ass.count("Dialogue:") >= 3
    assert "&H0000E4FF" in ass
