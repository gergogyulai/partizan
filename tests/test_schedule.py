import random

from partizan.schedule import Schedule
from partizan.youtube import LiveInfo, Video

T0 = 1_800_000_000.0


def make_schedule(n=5, duration=600, **kw):
    sched = Schedule(horizon=6 * 3600, history=3600, live_block=3600, rng=random.Random(1), **kw)
    videos = [Video(id=f"v{i}", title=f"Video {i}", duration=duration, index=i) for i in range(n)]
    sched.set_library(videos, now=T0)
    return sched


def assert_contiguous(sched):
    for a, b in zip(sched.items, sched.items[1:]):
        assert abs(a.end - b.start) < 1e-6, (a, b)


def test_plans_ahead_back_to_back():
    sched = make_schedule()
    assert sched.items[0].start == T0
    assert sched.items[-1].end >= T0 + 6 * 3600
    assert_contiguous(sched)
    assert all(p.kind == "vod" for p in sched.items)


def test_shuffle_plays_every_video_before_repeating():
    sched = make_schedule(n=5)
    first_round = [p.video_id for p in sched.items[:5]]
    assert sorted(first_round) == [f"v{i}" for i in range(5)]
    assert sched.items[5].video_id != sched.items[4].video_id


def test_newest_order():
    sched = make_schedule(n=3, order="newest")
    assert [p.video_id for p in sched.items[:4]] == ["v0", "v1", "v2", "v0"]


def test_current_and_next():
    sched = make_schedule()
    cur = sched.current(T0 + 700)
    assert cur.start <= T0 + 700 < cur.end
    nxt = sched.next_after(cur)
    assert nxt.start == cur.end


def test_offair_until_library_arrives_then_replaced():
    sched = Schedule(horizon=3600, history=0, rng=random.Random(1))
    sched.ensure(T0)
    assert all(p.kind == "offair" for p in sched.items)
    sched.set_library([Video(id="a", title="A", duration=300)], now=T0 + 10)
    cur = sched.current(T0 + 10)
    assert cur.kind == "vod" and cur.start == T0 + 10
    assert_contiguous(sched)


def test_live_interrupts_and_resumes():
    sched = make_schedule()
    playing = sched.current(T0 + 100)
    live = LiveInfo(id="L", title="Live show")
    sched.start_live(live, now=T0 + 100)
    assert playing.end == T0 + 100  # the video was cut short
    cur = sched.current(T0 + 100)
    assert cur.kind == "live" and cur.video_id == "L"
    assert cur.end == T0 + 100 + 3600
    assert_contiguous(sched)
    # The cut video and the rest of the plan come back after the live block.
    after = sched.next_after(cur)
    assert after.kind == "vod"

    # While live, the estimated end keeps moving forward.
    sched.start_live(live, now=T0 + 100 + 3000)
    cur = sched.current(T0 + 3100)
    assert cur.kind == "live" and cur.end == T0 + 3100 + 3600
    assert_contiguous(sched)

    sched.end_live(now=T0 + 4000)
    assert cur.end == T0 + 4000
    nxt = sched.current(T0 + 4000)
    assert nxt.kind == "vod" and nxt.start == T0 + 4000
    assert_contiguous(sched)
    assert sched.items[-1].end >= T0 + 4000 + 6 * 3600


def test_new_uploads_jump_the_queue():
    sched = make_schedule(n=3)
    new = Video(id="fresh", title="Fresh", duration=600, index=0)
    old = [Video(id=f"v{i}", title=f"Video {i}", duration=600, index=i + 1) for i in range(3)]
    current = sched.current(T0 + 100)
    sched.set_library([new] + old, now=T0 + 100)
    assert sched.current(T0 + 100) is current  # what's on now isn't interrupted
    assert sched.next_after(current).video_id == "fresh"
    assert_contiguous(sched)
    assert sched.items[-1].end >= T0 + 100 + 6 * 3600


def test_ban_replans(tmp_path):
    sched = make_schedule(n=3)
    cur = sched.current(T0 + 10)
    sched.ban(cur.video_id, now=T0 + 10)
    assert cur.video_id not in {p.video_id for p in sched.items if p.start >= T0 + 10}
    assert_contiguous(sched)


def test_persistence_roundtrip(tmp_path):
    sched = make_schedule(path=tmp_path / "s.json")
    sched.save()
    other = Schedule(path=tmp_path / "s.json")
    other.load()
    assert [p.to_dict() for p in other.items] == [p.to_dict() for p in sched.items]
    assert other.queue == sched.queue


def test_restart_after_downtime_starts_fresh():
    sched = make_schedule()
    later = T0 + 30 * 24 * 3600
    sched.ensure(later)
    assert sched.current(later).start == later
    assert_contiguous(sched)
