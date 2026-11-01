"""Artist biography lookup: validation helpers and resolution order."""

from __future__ import annotations

from typing import Any

import pytest

from mediacat.ingestion import artist_bio
from mediacat.ingestion.artist_bio import (
    BIO_MAX_CHARS,
    BIO_MIN_CHARS,
    SOURCE_DISCOGS,
    SOURCE_DISCOGS_OVER_WIKI,
    SOURCE_HOMEPAGE_AI,
    SOURCE_MB_WIKIPEDIA,
    SOURCE_NOT_FOUND,
    SOURCE_WIKIPEDIA_SEARCH,
    BioResult,
    about_links,
    choose_bio,
    clean_discogs_profile,
    clean_lead,
    discogs_artist_id,
    discogs_is_bio,
    html_to_text,
    is_meta_title,
    not_found_statement,
    reads_like_music_artist,
    resolve_artist_bio,
    split_paragraphs,
    title_matches_name,
    trim_bio,
)
from mediacat.llm.adapter import LlmResponse


def test_title_must_be_the_artist_name() -> None:
    assert title_matches_name("Dua Lipa", "Dua Lipa")
    assert title_matches_name("Genesis (band)", "Genesis")
    assert title_matches_name("Sigur Rós", "sigur rós")
    assert not title_matches_name("Nurse with Wound list", "Charlie Mike Sierra")
    assert not title_matches_name("NATO phonetic alphabet", "Charlie Mike Sierra")


def test_meta_pages_are_rejected() -> None:
    for title in (
        "Nurse with Wound list",
        "List of best-selling music artists",
        "Dua Lipa discography",
        "Mercury (disambiguation)",
        "Dua Lipa (album)",
        "Be the One (song)",
    ):
        assert is_meta_title(title), title
    assert not is_meta_title("Genesis (band)")
    assert not is_meta_title("Dua Lipa")


def test_music_artist_precheck() -> None:
    assert reads_like_music_artist("English singer and songwriter (born 1995)", "")
    assert reads_like_music_artist("", "Charlie Mike Sierra is a German techno producer.")
    assert not reads_like_music_artist("Spelling alphabet", "The NATO phonetic alphabet is…")


def test_split_paragraphs_drops_headings_and_fragments() -> None:
    text = (
        "Dua Lipa (born 22 August 1995) is an English singer and songwriter known for disco pop.\n"
        "Early life\n"
        "Lipa was born in London to Kosovo-Albanian parents and moved to Pristina aged eleven.\n"
        "short."
    )
    assert split_paragraphs(text) == [
        "Dua Lipa (born 22 August 1995) is an English singer and songwriter known for disco pop.",
        "Lipa was born in London to Kosovo-Albanian parents and moved to Pristina aged eleven.",
    ]


def test_trim_bio_between_quarter_and_half_page() -> None:
    para = "This sentence is about the artist and their records. " * 8  # ~424 chars
    paras = [para.strip()] * 10
    bio = trim_bio(paras)
    assert BIO_MIN_CHARS <= len(bio) <= BIO_MAX_CHARS
    assert all(p.endswith(".") for p in bio.split("\n\n"))

    # A single short lead stays as it is (nothing more to add)
    assert trim_bio(["Dua Lipa is an English singer and songwriter."]) == (
        "Dua Lipa is an English singer and songwriter."
    )

    # One huge paragraph is cut at a sentence boundary under the maximum
    huge = "Long sentence about the career of this artist goes here. " * 100
    cut = trim_bio([huge])
    assert len(cut) <= BIO_MAX_CHARS
    assert cut.endswith(".")


def test_clean_lead_drops_pronunciation() -> None:
    assert (
        clean_lead(
            "Dua Lipa ( DOO-ə LEE-pə; Albanian: [ˈdua ˈlipa] ; born 22 August 1995) is an English singer."
        )
        == "Dua Lipa (born 22 August 1995) is an English singer."
    )
    assert clean_lead("ABBA (AB-ə, Swedish: ) were a Swedish pop group.") == (
        "ABBA were a Swedish pop group."
    )
    assert clean_lead("Nick Mason (born 27 January 1944) is a drummer.") == (
        "Nick Mason (born 27 January 1944) is a drummer."
    )
    plain = "Pink Floyd are an English rock band formed in London in 1965 by Syd Barrett."
    assert clean_lead(plain) == plain


def test_not_found_statement_explains_why() -> None:
    text = not_found_statement("Charlie Mike Sierra", had_mb=True, had_homepage=False, llm=True)
    assert text.startswith("No biography found for Charlie Mike Sierra:")
    assert "MusicBrainz lists no Wikipedia article" in text
    assert "no official website is known" in text
    assert "no Discogs artist profile was found" in text
    thin = not_found_statement("X", had_mb=True, had_homepage=False, llm=True, discogs="no_bio")
    assert "the Discogs profile holds no real biography" in thin


# Real Discogs profiles (artists 145062, 4357593 and an excerpt of 45467)
DG_CHARLIE = "Often mistakenly believed to be an alias of [a=Jean-Pierre Massiera]"
DG_DUA = (
    "British singer, songwriter, and model.\r\nBorn: 22 August 1995 in London, England, UK."
    "\r\nLipa is of English-Kosovar Albanian descent. She won the 2019 Grammy for Best New Artist."
)
DG_FLOYD = (
    "Pink Floyd was an English rock band from London.\r\n\r\n"
    "[b]Main official members:[/b]\r\n[a=Roger Waters] – bass guitar\r\n"
    "[a=Seamus (11)] and [a12345] on [l=Harvest], see [url=https://x.example]the site[/url]."
)


def test_clean_discogs_profile_markup() -> None:
    assert clean_discogs_profile(DG_CHARLIE) == [
        "Often mistakenly believed to be an alias of Jean-Pierre Massiera"
    ]
    assert clean_discogs_profile(DG_FLOYD) == [
        "Pink Floyd was an English rock band from London.",
        "Main official members: Roger Waters – bass guitar Seamus and on Harvest, see the site.",
    ]


def test_discogs_profile_must_be_a_real_bio() -> None:
    assert not discogs_is_bio(clean_discogs_profile(DG_CHARLIE))  # one note, no bio
    assert discogs_is_bio(clean_discogs_profile(DG_DUA))
    assert not discogs_is_bio([])


def test_discogs_artist_id_from_musicbrainz_link() -> None:
    assert discogs_artist_id("https://www.discogs.com/artist/145062") == 145062
    assert discogs_artist_id("https://www.discogs.com/de/artist/4357593-Dua-Lipa") == 4357593
    assert discogs_artist_id("https://www.discogs.com/label/1") is None
    assert discogs_artist_id(None) is None


def _bio(text: str, source: str, image: str | None = None) -> BioResult:
    return BioResult(extract=text, source=source, found=True, thumbnail_url=image)


def test_choose_bio_is_weighted_for_wikipedia() -> None:
    wiki_long = _bio("w" * 1499, "MusicBrainz→Wikipedia", "wiki.jpg")
    assert choose_bio(wiki_long, _bio("d" * 175, SOURCE_DISCOGS)) is wiki_long
    # Discogs must be at least twice as long to replace Wikipedia
    wiki_short = _bio("w" * 400, "MusicBrainz→Wikipedia", "wiki.jpg")
    assert choose_bio(wiki_short, _bio("d" * 700, SOURCE_DISCOGS)) is wiki_short
    winner = choose_bio(wiki_short, _bio("d" * 800, SOURCE_DISCOGS))
    assert winner is not None
    assert winner.source == SOURCE_DISCOGS_OVER_WIKI
    assert winner.thumbnail_url == "wiki.jpg"  # Discogs had no image
    # Only one source: that one; neither: nothing
    only = _bio("d" * 300, SOURCE_DISCOGS)
    assert choose_bio(None, only) is only
    assert choose_bio(None, None) is None


def test_html_to_text_and_about_links() -> None:
    html = (
        "<html><head><script>var x=1;</script><style>p{}</style></head><body>"
        "<nav><a href='/shop'>Shop</a></nav>"
        "<p>Charlie Mike Sierra is a DJ &amp; producer.</p>"
        "<a href='/biography'>Read more</a> <a href='https://other.example/about'>x</a>"
        "<a href='/tour'>About the tour</a></body></html>"
    )
    text = html_to_text(html)
    assert "Charlie Mike Sierra is a DJ & producer." in text
    assert "var x" not in text
    assert "Shop" not in text
    assert about_links(html, "https://cms.example/") == [
        "https://cms.example/biography",
        "https://cms.example/tour",
    ]


# ---------------------------------------------------------------------------
# Resolution order with stubbed network and LLM
# ---------------------------------------------------------------------------


class _FakeWiki:
    def __init__(
        self,
        *,
        links: dict[str, Any],
        search: list[str],
        summaries: dict[str, dict[str, Any]],
    ) -> None:
        self._links = links
        self._search = search
        self._summaries = summaries
        self.summaries_fetched: list[str] = []

    async def musicbrainz_links(self, name: str) -> dict[str, Any]:
        return self._links

    async def search_artist(self, name: str) -> list[dict[str, Any]]:
        return [{"title": t} for t in self._search]

    async def fetch_artist_summary(self, title: str) -> dict[str, Any]:
        self.summaries_fetched.append(title)
        return self._summaries.get(title, {})

    async def fetch_plain_paragraphs(self, title: str) -> list[str]:
        return split_paragraphs(self._summaries.get(title, {}).get("extract", ""))


class _FakeLlm:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.tasks: list[str] = []

    async def complete(self, system: str, user: str, **kw: Any) -> LlmResponse:
        self.tasks.append(kw.get("task", ""))
        return LlmResponse(text=self.answers.pop(0), provider="fake", model="fake")


def _use(monkeypatch: pytest.MonkeyPatch, wiki: _FakeWiki) -> None:
    monkeypatch.setattr(artist_bio, "WikipediaConnector", lambda: wiki)


@pytest.fixture(autouse=True)
def discogs_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Discogs is stubbed in every test; tests may replace the stub."""
    calls: list[str] = []

    async def missing(name: str, **kw: Any) -> tuple[BioResult | None, str]:
        calls.append(name)
        return None, "missing"

    monkeypatch.setattr(artist_bio, "fetch_discogs_profile", missing)
    return calls


LEAD = (
    "Dua Lipa (born 22 August 1995) is an English singer and songwriter. Her "
    "music combines pop and disco with a strong dance influence across her albums."
)


LONG_LEAD = " ".join([LEAD] * 6)  # ~900 chars: a well-furnished Wikipedia bio


async def test_long_wikipedia_bio_skips_discogs(
    monkeypatch: pytest.MonkeyPatch, discogs_calls: list[str]
) -> None:
    wiki = _FakeWiki(
        links={
            "mbid": "x",
            "wp_title": "Dua Lipa",
            "discogs_url": "https://www.discogs.com/artist/4357593",
        },
        search=[],
        summaries={"Dua Lipa": {"extract": LONG_LEAD, "description": "English singer"}},
    )
    _use(monkeypatch, wiki)
    bio = await resolve_artist_bio("Dua Lipa")
    assert bio.source == SOURCE_MB_WIKIPEDIA
    assert discogs_calls == []  # Discogs was not even asked


async def test_discogs_number_is_dropped_except_for_discogs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: dict[str, str] = {}

    class Wiki(_FakeWiki):
        async def musicbrainz_links(self, name: str) -> dict[str, Any]:
            asked["mb"] = name
            return {}

    _use(monkeypatch, Wiki(links={}, search=[], summaries={}))

    async def profile(name: str, **kw: Any) -> tuple[BioResult | None, str]:
        asked["discogs"] = name
        return None, "missing"

    monkeypatch.setattr(artist_bio, "fetch_discogs_profile", profile)
    await resolve_artist_bio("Adele (3)")
    assert asked == {"mb": "Adele", "discogs": "Adele (3)"}


async def test_discogs_only_artist_uses_discogs(monkeypatch: pytest.MonkeyPatch) -> None:
    wiki = _FakeWiki(links={"mbid": "m", "discogs_url": "u"}, search=[], summaries={})
    _use(monkeypatch, wiki)
    seen: dict[str, Any] = {}

    async def profile(name: str, **kw: Any) -> tuple[BioResult | None, str]:
        seen.update(kw)
        return _bio("A Detroit techno producer. Active since 1992.", SOURCE_DISCOGS), "ok"

    monkeypatch.setattr(artist_bio, "fetch_discogs_profile", profile)
    bio = await resolve_artist_bio("Somebody", discogs_token="tok")
    assert bio.source == SOURCE_DISCOGS
    assert seen == {"artist_url": "u", "token": "tok"}


async def test_thin_discogs_profile_means_none_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Charlie Mike Sierra: no Wikipedia, Discogs has a one-line note only."""
    wiki = _FakeWiki(
        links={"mbid": "fb2b", "discogs_url": "https://www.discogs.com/artist/145062"},
        search=["Nurse with Wound list"],
        summaries={},
    )
    _use(monkeypatch, wiki)

    async def thin(name: str, **kw: Any) -> tuple[BioResult | None, str]:
        return None, "no_bio"

    monkeypatch.setattr(artist_bio, "fetch_discogs_profile", thin)
    bio = await resolve_artist_bio("Charlie Mike Sierra")
    assert not bio.found
    assert "the Discogs profile holds no real biography" in bio.extract


async def test_musicbrainz_link_is_trusted_without_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    wiki = _FakeWiki(
        links={"mbid": "x", "wp_title": "Dua Lipa", "homepages": []},
        search=[],
        summaries={
            "Dua Lipa": {
                "extract": LEAD,
                "description": "English singer",
                "url": "https://en.wikipedia.org/wiki/Dua_Lipa",
                "thumbnail_url": "https://thumb.wikimedia.org/x.jpg",
            }
        },
    )
    _use(monkeypatch, wiki)
    llm = _FakeLlm()
    bio = await resolve_artist_bio("Dua Lipa", llm=llm)  # type: ignore[arg-type]
    assert bio.found
    assert bio.source == SOURCE_MB_WIKIPEDIA
    assert bio.thumbnail_url == "https://thumb.wikimedia.org/x.jpg"
    assert llm.tasks == []


async def test_list_page_hit_becomes_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reported case: search returns 'Nurse with Wound list' for Charlie Mike Sierra."""
    wiki = _FakeWiki(
        links={"mbid": "fb2b", "wp_title": None, "homepages": []},
        search=["Nurse with Wound list", "NATO phonetic alphabet"],
        summaries={"Nurse with Wound list": {"extract": "The Nurse with Wound list is…"}},
    )
    _use(monkeypatch, wiki)
    bio = await resolve_artist_bio("Charlie Mike Sierra", llm=_FakeLlm())  # type: ignore[arg-type]
    assert not bio.found
    assert bio.source == SOURCE_NOT_FOUND
    assert bio.extract.startswith("No biography found for Charlie Mike Sierra")
    assert wiki.summaries_fetched == []  # rejected on the title alone


async def test_llm_can_veto_a_plausible_search_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    wiki = _FakeWiki(
        links={},
        search=["Mercury (band)"],
        summaries={
            "Mercury (band)": {"extract": "Mercury was a rock band from Ohio.", "description": ""}
        },
    )
    _use(monkeypatch, wiki)
    llm = _FakeLlm('{"music_artist": true, "same_artist": false, "reason": "other band"}')
    bio = await resolve_artist_bio("Mercury", llm=llm)  # type: ignore[arg-type]
    assert not bio.found
    assert llm.tasks == ["bio_verification"]


async def test_verified_search_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    wiki = _FakeWiki(
        links={},
        search=["Dua Lipa (album)", "Dua Lipa"],
        summaries={"Dua Lipa": {"extract": LEAD, "description": "English singer"}},
    )
    _use(monkeypatch, wiki)
    llm = _FakeLlm('```json\n{"music_artist": true, "same_artist": true, "reason": "ok"}\n```')
    bio = await resolve_artist_bio("Dua Lipa", llm=llm)  # type: ignore[arg-type]
    assert bio.found
    assert bio.source == SOURCE_WIKIPEDIA_SEARCH
    assert bio.description == "English singer"


async def test_homepage_summary_when_wikipedia_has_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki = _FakeWiki(
        links={"mbid": "m", "wp_title": None, "homepages": ["https://artist.example/"]},
        search=[],
        summaries={},
    )
    _use(monkeypatch, wiki)

    async def fake_text(url: str) -> str:
        return "Charlie Mike Sierra is a Berlin techno producer active since 1998. " * 5

    monkeypatch.setattr(artist_bio, "fetch_homepage_text", fake_text)
    summary = (
        "Charlie Mike Sierra is a Berlin-based techno producer who has released records "
        "since 1998 on several independent labels."
    )
    llm = _FakeLlm(summary)
    bio = await resolve_artist_bio("Charlie Mike Sierra", llm=llm)  # type: ignore[arg-type]
    assert bio.found
    assert bio.source == SOURCE_HOMEPAGE_AI
    assert bio.url == "https://artist.example/"
    assert bio.extract == summary
    assert llm.tasks == ["bio_summary"]


async def test_homepage_without_bio_gives_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    wiki = _FakeWiki(
        links={"mbid": "m", "homepages": ["https://artist.example/"]}, search=[], summaries={}
    )
    _use(monkeypatch, wiki)

    async def fake_text(url: str) -> str:
        return "Tickets and merchandise. " * 20

    monkeypatch.setattr(artist_bio, "fetch_homepage_text", fake_text)
    bio = await resolve_artist_bio("X", llm=_FakeLlm("NONE"))  # type: ignore[arg-type]
    assert not bio.found
    assert "official website held no usable biography" in bio.extract


async def test_internal_urls_are_never_fetched() -> None:
    assert not await artist_bio._is_public_url("http://127.0.0.1/")
    assert not await artist_bio._is_public_url("http://localhost:8000/")
    assert not await artist_bio._is_public_url("http://10.0.0.5/admin")
    assert not await artist_bio._is_public_url("file:///etc/passwd")
