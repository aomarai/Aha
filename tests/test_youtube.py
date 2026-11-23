"""Tests for the YouTube link allowlist behind /play."""

from __future__ import annotations

import pytest

from aha.youtube import HOSTS, LinkError, video_id, watch_url

ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "link",
    [
        f"https://www.youtube.com/watch?v={ID}",
        f"http://www.youtube.com/watch?v={ID}",
        f"https://youtube.com/watch?v={ID}",
        f"https://m.youtube.com/watch?v={ID}",
        f"https://music.youtube.com/watch?v={ID}",
        f"https://youtu.be/{ID}",
        f"https://youtu.be/{ID}?t=42",
        f"https://www.youtube.com/shorts/{ID}",
        f"https://www.youtube.com/embed/{ID}",
        f"https://www.youtube.com/watch?v={ID}&t=42s",
        f"https://www.youtube.com/watch?v={ID}&list=PLabc&index=3",  # the video of a playlist link
        f"  https://www.youtube.com/watch?v={ID}  ",
        f"<https://www.youtube.com/watch?v={ID}>",  # Discord wraps links to hide the embed
        f"HTTPS://WWW.YOUTUBE.COM/watch?v={ID}",
        f"https://www.youtube.com:443/watch?v={ID}",
        f"http://www.youtube.com:80/watch?v={ID}",
    ],
)
def test_a_youtube_video_link_gives_its_id(link: str) -> None:
    assert video_id(link) == ID


@pytest.mark.parametrize(
    "link",
    [
        "",
        "   ",
        "never gonna give you up",
        ID,  # a bare id: the link is required, so the intent is clear
        f"youtube.com/watch?v={ID}",  # no scheme
        f"ftp://www.youtube.com/watch?v={ID}",
        f"file:///etc/passwd?v={ID}",
        f"javascript:alert(1)//youtube.com/watch?v={ID}",
        f"https://evil.example/watch?v={ID}",
        f"https://www.youtube.com.evil.example/watch?v={ID}",
        f"https://evilyoutube.com/watch?v={ID}",
        f"https://youtube.com@evil.example/watch?v={ID}",  # the real host is evil.example
        f"https://user:pw@www.youtube.com/watch?v={ID}",
        f"https://user@www.youtube.com/watch?v={ID}",
        f"https://www.youtube.com:8443/watch?v={ID}",
        "https://www.youtube.com:notaport/watch?v=" + ID,
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch?v=",
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/watch?v=" + ID + "x",  # 12 characters
        "https://www.youtube.com/watch?v=" + ID[:-1] + "!",
        f"https://www.youtube.com/user/someone?v={ID}",
        "https://www.youtube.com/@channel",
        "https://youtu.be/",
        "https://www.youtube.com/shorts/",
        "https://www.youtube.com/shorts",
        "https://www.youtube.com/",
        f"https://www.youtube.com/live/{ID}",  # live links are not accepted at all
    ],
)
def test_anything_else_is_a_bad_link(link: str) -> None:
    with pytest.raises(LinkError) as error:
        video_id(link)

    assert error.value.key == "voice.bad_link"


def test_a_link_that_is_only_a_playlist_gets_its_own_message() -> None:
    with pytest.raises(LinkError) as error:
        video_id("https://www.youtube.com/playlist?list=PLabcdefghij")

    assert error.value.key == "voice.playlist_unsupported"
    assert str(error.value) == "voice.playlist_unsupported"


def test_a_playlist_path_without_a_list_is_just_a_bad_link() -> None:
    with pytest.raises(LinkError) as error:
        video_id("https://www.youtube.com/playlist")

    assert error.value.key == "voice.bad_link"


def test_the_allowlist_is_exactly_the_youtube_hosts() -> None:
    assert {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    } == HOSTS


def test_the_canonical_link_is_built_from_the_id_alone() -> None:
    assert watch_url(ID) == f"https://www.youtube.com/watch?v={ID}"


def test_what_reaches_the_node_never_carries_anything_the_user_typed_but_the_id() -> None:
    sneaky = (
        f"https://www.youtube.com/watch?v={ID}&list=PL1&start_radio=1&foo=http://169.254.169.254/"
    )

    assert watch_url(video_id(sneaky)) == f"https://www.youtube.com/watch?v={ID}"


def test_extra_path_segments_after_a_short_link_id_are_ignored() -> None:
    assert video_id(f"https://youtu.be/{ID}/anything/more") == ID
    assert video_id(f"https://youtu.be/{ID}?si=tracking") == ID


def test_a_shorts_or_embed_link_with_trailing_parts_gives_the_id() -> None:
    assert video_id(f"https://www.youtube.com/shorts/{ID}/") == ID
    assert video_id(f"https://www.youtube.com/embed/{ID}?rel=0") == ID


def test_a_watch_link_with_v_in_the_wrong_place_is_not_used() -> None:
    with pytest.raises(LinkError):
        video_id(f"https://www.youtube.com/other?v={ID}")
    with pytest.raises(LinkError):
        video_id(f"https://www.youtube.com/?v={ID}")
