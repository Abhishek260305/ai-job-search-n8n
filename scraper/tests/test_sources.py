"""The source registry: every valid source name, the groups people can type, and which sources cost money."""
import pytest

import sources as S


def test_expand_groups_and_names():
    assert S.expand(["boards", "remotive"]) == ["greenhouse", "lever", "ashby", "workable", "smartrecruiters", "remotive"]


def test_expand_removes_duplicates_and_keeps_order():
    assert S.expand(["indeed", "jobspy"]) == ["indeed", "naukri", "linkedin"]


def test_expand_rejects_unknown():
    with pytest.raises(ValueError, match="nope") as err:
        S.expand(["indeed", "nope"])
    assert "feeds" in str(err.value)            # the message lists the group names too
    assert "greenhouse" in str(err.value)       # and the plain source names


def test_every_source_has_a_description_and_one_group_or_none():
    assert set(S.DESCRIPTIONS) == set(S.ALL)
    assert all(S.DESCRIPTIONS[name].strip() for name in S.ALL)
    for name in S.ALL:
        assert sum(name in members for members in S.GROUPS.values()) <= 1
    assert set(S.GROUPS) == {"jobspy", "apify", "boards", "feeds"}
    assert len(S.ALL) == len(set(S.ALL))


def test_only_apify_is_paid():
    assert S.PAID == frozenset({"linkedin_apify"})
    assert S.PAID <= set(S.ALL)
    assert "Apify credit" in S.DESCRIPTIONS["linkedin_apify"]


def test_all_matches_the_old_known_sources():
    old = {"indeed", "naukri", "linkedin", "glassdoor", "ziprecruiter", "google", "linkedin_apify"}
    assert old <= set(S.ALL)


def test_probe_urls_cover_every_board_system():
    assert set(S.PROBE_URLS) == set(S.BOARD_SYSTEMS)
    assert all(url.startswith("https://") and "{slug}" in url for url in S.PROBE_URLS.values())


@pytest.mark.parametrize("slug", ["acme", "a", "Acme-Corp_1.io", "9lives", "a" * 64])
def test_valid_slug_accepts(slug):
    assert S.valid_slug(slug) is True


@pytest.mark.parametrize("slug", ["", "-x", ".x", "../x", "a b", "a/b", "acme\n", "a" * 65, "café", "ａbc", None, 7, ["a"]])
def test_valid_slug_rejects(slug):
    assert S.valid_slug(slug) is False


def test_boards_valid_slug_is_the_same_rule():
    import boards
    assert boards.valid_slug is S.valid_slug
    assert S.SLUG_PATTERN == r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
