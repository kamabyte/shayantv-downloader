from pathlib import Path

from PIL import Image

from shayantv_downloader import hls, library
from shayantv_downloader.site import Episode, extract_app_data, parse_episodes, parse_show_list
from shayantv_downloader.state import State, assign_numbers

MASTER = """#EXTM3U
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MEDIA:TYPE=AUDIO,URI="stream_5.m3u8",GROUP-ID="default-audio-group",LANGUAGE="en",NAME="eng",DEFAULT=NO,AUTOSELECT=YES,CHANNELS="2"
#EXT-X-STREAM-INF:BANDWIDTH=3871519,CODECS="avc1.64001f,mp4a.40.2",RESOLUTION=1280x720,AUDIO="default-audio-group"
stream_1.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5674533,CODECS="avc1.640028,mp4a.40.2",RESOLUTION=1920x1080,AUDIO="default-audio-group"
stream_0.m3u8
"""

LIST_HTML = """
<div data-role="list-item"><div class="l-card">
  <a href="/cartoons/luntik" class="l-card-inner">
    <img src="/upload/iblock/c55/x.jpg" class="cover l-card-img" alt="Лунтик и его друзья">
    <h4 class="l-card-title">Лунтик и его друзья</h4>
  </a></div></div>
<a href="/cartoons/" class="l-card-inner">bogus self link</a>
"""

SHOW_HTML = r"""<script>
let JSAppData = {"VideoPlaylist":[{"season":"1","episodes":[
 {"season":"1","episode":1,"title":"1 серия","preview":"\/upload\/a.webp","noPublicAccess":false,"CDNReady":true,
  "hls":"https:\/\/cdn.example\/done\/hls\/8d08fcb71685099b38ec10dd4b2d0611ce75577d\/master.m3u8","duration":327},
 {"season":"1","episode":2,"title":"2 серия ","preview":null,"noPublicAccess":true,"CDNReady":true,
  "hls":"https:\/\/cdn.example\/done\/hls\/aaaaaaaaaaaaaaaaaaaa\/master.m3u8","duration":300}
]}],"noSeason":false,"elementID":1};
</script>"""


def ep(uid, site_episode, title, season=1):
    return Episode(uid=uid, season=season, site_episode=site_episode, title=title,
                   hls_url=f"https://x/hls/{uid}/master.m3u8", preview_url=None, duration=None)


def test_parse_master_picks_best_variant_and_audio():
    master = hls.parse_master(MASTER, "https://cdn.example/done/hls/abc/master.m3u8")
    best = master.best_variant()
    assert best.url == "https://cdn.example/done/hls/abc/stream_0.m3u8"
    audios = master.renditions_for(best, "AUDIO")
    assert [a.url for a in audios] == ["https://cdn.example/done/hls/abc/stream_5.m3u8"]


def test_ffmpeg_command_maps_audio_and_remaps_language(tmp_path):
    master = hls.parse_master(MASTER, "https://cdn.example/m/master.m3u8")
    cmd = hls.build_ffmpeg_command(master, tmp_path / "out.mkv.part", user_agent="UA",
                                   language_map={"en": "tat"}, default_language="tat", metadata={})
    assert cmd.count("-i") == 2
    assert ["-map", "0:v:0", "-map", "1:a:0"] == cmd[cmd.index("-map"):cmd.index("-map") + 4]
    assert "language=tat" in cmd


def test_parse_show_list_skips_self_link():
    shows = parse_show_list(LIST_HTML, "https://shayantv.ru/cartoons/", "/cartoons/")
    assert [(s.slug, s.title) for s in shows] == [("luntik", "Лунтик и его друзья")]
    assert shows[0].image_url == "https://shayantv.ru/upload/iblock/c55/x.jpg"


def test_parse_episodes_from_app_data():
    episodes = parse_episodes(extract_app_data(SHOW_HTML), "https://shayantv.ru/cartoons/luntik")
    assert len(episodes) == 1  # noPublicAccess one is skipped
    assert episodes[0].uid == "8d08fcb71685099b38ec10dd4b2d0611ce75577d"
    assert episodes[0].preview_url == "https://shayantv.ru/upload/a.webp"


def test_assign_numbers_prefers_title_number():
    # Смешарики: the second entry claims episode 1 but its title is "2 серия".
    numbers = assign_numbers([ep("a", 1, "1 серия"), ep("b", 1, "2 серия"), ep("c", 3, "3 серия")], set())
    assert numbers == {"a": 1, "b": 2, "c": 3}


def test_assign_numbers_appends_true_duplicates_without_shifting_others():
    # Цветняшки: two different "17 серия" entries must not push 18, 19 out of place.
    eps = [ep("e16", 16, "16 серия"), ep("e17", 17, "17 серия"), ep("dup", 17, "17 серия"),
           ep("e18", 18, "18 серия"), ep("e19", 19, "19 серия")]
    numbers = assign_numbers(eps, set())
    assert numbers == {"e16": 16, "e17": 17, "e18": 18, "e19": 19, "dup": 20}


def test_assign_numbers_uses_site_number_for_named_episodes():
    assert assign_numbers([ep("a", 1, "Буква А"), ep("b", 2, "Буква Ә")], set()) == {"a": 1, "b": 2}


def test_state_numbers_are_stable_across_runs(tmp_path):
    from shayantv_downloader.site import Show

    state = State(tmp_path / "s.db")
    state.upsert_show(Show("s", "Show", "u", None), "Show")
    assert state.add_episodes("s", [ep("a", 1, "1 серия"), ep("a", 1, "1 серия")]) == 1
    # A new episode later inserted *before* "a" on the page must not renumber "a".
    assert state.add_episodes("s", [ep("new", 1, "1 серия"), ep("a", 1, "1 серия")]) == 1
    numbers = {r.uid: r.episode for r in state.todo()}
    assert numbers == {"a": 1, "new": 2}


def test_sanitize():
    assert library.sanitize('Пчёлки: "шпионки"? / 2.') == "Пчёлки шпионки 2"
    assert library.episode_basename("Три кота", 1, 5, "Пикник") == "Три кота - S01E05 - Пикник"


def test_make_poster_is_portrait():
    poster = library.make_poster(Image.new("RGB", (1600, 696), "red"))
    assert poster.size == library.POSTER_SIZE


def test_nfo_written(tmp_path: Path):
    path = tmp_path / "tvshow.nfo"
    library.write_tvshow_nfo(path, title="Фиксики", slug="fiksiki", url="https://shayantv.ru/cartoons/fiksiki")
    text = path.read_text(encoding="utf-8")
    assert "<title>Фиксики</title>" in text and "<lockdata>true</lockdata>" in text


def test_health_states(tmp_path):
    from shayantv_downloader import health
    from shayantv_downloader.config import Config

    cfg = Config(library_dir=tmp_path / "lib", state_dir=tmp_path / "state")
    assert health.check(cfg)[0] is False  # nothing written yet
    health.beat(cfg)
    assert health.check(cfg) == (True, "ok")
    health.sync_finished(cfg, "RuntimeError: no shows found")
    assert health.check(cfg)[0] is False
    health.sync_finished(cfg, None)
    assert health.check(cfg)[0] is True
