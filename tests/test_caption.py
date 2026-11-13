import sys, types, os
for n in ("psycopg2", "psycopg2.extras", "psycopg2.pool"):
    sys.modules.setdefault(n, types.ModuleType(n))
os.environ.pop("OPENAI_API_KEY", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import caption as c


def test_multiple_emoji_kept_and_quotes_allowed():
    assert c._enforce_single_trailing_emoji('"He did it" \U0001F633\U0001F633') == '"He did it" \U0001F633\U0001F633'
    assert c._enforce_single_trailing_emoji("Wait \U0001F633 \U0001F62E").endswith("\U0001F633\U0001F62E")
    assert c._core_line('"Do you agree?" \U0001F447\U0001F914').startswith('"Do you agree?"')


def test_fallback_uses_clip_content_and_varies():
    out = {c.fallback_on_screen("He said taxes are theft and walked out of the court today.", {"title": "Courtroom walkout"}) for _ in range(20)}
    assert len(out) > 3


def test_hashtags_vary():
    sets = {c.enforce_tag_mix("Do you agree?\n\n#a", "judge courtroom", 5) for _ in range(15)}
    assert len(sets) > 3


def test_core_caption_syncs_to_platforms():
    posts = c.default_platform_posts("Do you agree? \U0001F447\n\n#funny #viral")
    new = c.sync_posts_with_master(posts, "Do you agree? \U0001F447", '"Not him" \U0001F633\U0001F633')
    assert new["instagram"]["caption"].startswith('"Not him"') and "#funny" in new["instagram"]["caption"]
    assert new["youtube"]["caption"].startswith('"Not him"')
