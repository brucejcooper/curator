"""Instagram: a few people's posts, all of them, read gently; trouble shows in the app."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import instaloader
import pytest

from curator import instagram, people, store
from curator.judge import apply_shared_rules
from curator.stories import ingest


def post(code, days, caption="A new ring, cast in recycled silver and set with a sapphire"):
    when = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)  # instaloader's date_utc is naive
    return SimpleNamespace(shortcode=code, caption=caption, date_utc=when, url=f"https://cdn.example/{code}.jpg")


@pytest.fixture
def insta(db, tmp_path, monkeypatch):
    """A fake Instagram: posts per handle, and a record of who was looked at."""
    monkeypatch.setenv("INSTAGRAM_USER", "throwaway")
    monkeypatch.setattr(instagram, "PICTURES", tmp_path / "media" / "instagram")
    monkeypatch.setattr(instagram, "SESSION", tmp_path / "session")
    monkeypatch.setattr(instagram, "loader", lambda conn: SimpleNamespace(context=None))
    monkeypatch.setattr(instagram, "save", lambda http, url, name, refresh=False: f"https://curator.example/media/instagram/{name}.jpg")
    fake = SimpleNamespace(posts={}, looked=[], fail=None)

    def from_username(context, handle):
        fake.looked.append(handle)
        if fake.fail:
            raise fake.fail
        return SimpleNamespace(full_name=handle.title(), profile_pic_url="https://cdn.example/me.jpg",
                               get_posts=lambda: iter(fake.posts.get(handle, [])))
    monkeypatch.setattr(instaloader.Profile, "from_username", staticmethod(from_username))
    return fake


def test_sources_list_people_whose_every_post_is_shown(alex):
    src = people.parse_sources("fixed:\n  instagram:\n    people: ['@jeweller', architects]\n", "alex")
    assert src["instagram_people"] == {"jeweller", "architects"}
    assert {"instagram:@jeweller", "instagram:@architects"} <= src["keys"] & src["always"]


def test_one_person_per_cycle_most_overdue_first_and_only_new_posts(db, insta):
    insta.posts = {"jeweller": [post("pinned", 400), post("b", 1), post("a", 3)], "architects": [post("c", 2)]}
    store.put(db, "instagram_fetched:@jeweller", "2001-01-01T00:00:00+00:00")
    store.put(db, "instagram_fetched:@architects", "2000-01-01T00:00:00+00:00")
    first = instagram.collect(db, {"jeweller", "architects"})
    assert insta.looked == ["architects"] and [it["url"] for it in first] == ["https://www.instagram.com/p/c/"]
    second = instagram.collect(db, {"jeweller", "architects"})
    assert insta.looked[-1] == "jeweller"
    assert {it["url"].split("/")[-2] for it in second} == {"a", "b"}  # not the year-old pinned post
    assert instagram.collect(db, {"jeweller", "architects"}) == []  # nobody due for a few hours
    it = second[0]
    assert it["source"] == "instagram:@jeweller" and it["author"] == "@jeweller" and it["always_show"] == 1
    assert it["image"].endswith("/media/instagram/b.jpg") and it["poster"]["name"] == "Jeweller"

    insta.posts["jeweller"].insert(0, post("d", 0))
    store.put(db, "instagram_fetched:@jeweller", "2000-01-01T00:00:00+00:00")
    assert [it["url"].split("/")[-2] for it in instagram.collect(db, {"jeweller"})] == ["d"]


def test_being_logged_out_is_said_in_the_app_and_clears_once_it_reads_again(db, insta):
    instagram.SESSION.write_text("old")
    insta.fail = instaloader.exceptions.LoginRequiredException("Login required")
    assert instagram.collect(db, {"jeweller"}) == []
    assert not instagram.SESSION.exists()  # logs in afresh next time
    assert "logged us out" in store.get(db, instagram.PROBLEM)
    insta.fail = None
    instagram.collect(db, {"jeweller"})  # still due: the failed look didn't count
    assert store.get(db, instagram.PROBLEM) is None


def test_a_refusal_pauses_instagram_for_an_hour(db, insta):
    insta.fail = instaloader.exceptions.ConnectionException("401 Unauthorized - Please wait a few minutes")
    assert instagram.collect(db, {"jeweller"}) == [] and instagram.collect(db, {"jeweller"}) == []
    assert insta.looked == ["jeweller"]
    assert "trying again in 1 h" in store.get(db, instagram.PROBLEM)
    store.put(db, "instagram_paused_until", "")
    instagram.collect(db, {"jeweller"})
    assert "trying again in 2 h" in store.get(db, instagram.PROBLEM)  # doubling while it keeps refusing


def test_a_failed_login_waits_hours_before_trying_again(db, tmp_path, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_USER", "throwaway")
    monkeypatch.setenv("INSTAGRAM_PASSWORD", "wrong")
    monkeypatch.setattr(instagram, "SESSION", tmp_path / "session")
    tries = []

    def login(self, user, password):
        tries.append(user)
        raise instaloader.exceptions.BadCredentialsException("Wrong password.")
    monkeypatch.setattr(instaloader.Instaloader, "login", login)
    assert instagram.loader(db) is None and instagram.loader(db) is None
    assert tries == ["throwaway"]
    assert "Couldn't log in to Instagram as throwaway" in store.get(db, instagram.PROBLEM)


def test_captions_full_of_hashtags_still_show(db, insta):
    soup = "New work " + " ".join(f"#tag{n}" for n in range(12))
    insta.posts = {"jeweller": [post("a", 1, soup)]}
    new = ingest(db, instagram.collect(db, {"jeweller"}))
    apply_shared_rules(db, new)
    assert db.execute("SELECT rule FROM items WHERE id = ?", (new[0],)).fetchone()[0] is None


def test_pictures_no_item_uses_are_tidied_away(db, insta):
    pics = instagram.PICTURES
    pics.mkdir(parents=True)
    for name in ("a.jpg", "gone.jpg", "@jeweller.jpg"):
        (pics / name).write_bytes(b"x")
    insta.posts = {"jeweller": [post("a", 1)]}
    ingest(db, instagram.collect(db, {"jeweller"}))
    assert instagram.tidy(db) == 1
    assert sorted(p.name for p in pics.iterdir()) == ["@jeweller.jpg", "a.jpg"]
