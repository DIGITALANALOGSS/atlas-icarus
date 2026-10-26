from unittest.mock import AsyncMock, Mock

import pytest

from app import conversation_research as research
from app import conversation_vane as vane
from app.conversation_search import SearchError

OFFICIAL = "https://docs.example.org/command/"
OTHER = "https://community.example.org/post/"
HOSTS = ("docs.example.org",)
REQUIRED = (OFFICIAL,)


def evidence(message, urls):
    return {
        "message": message,
        "sources": [
            {"metadata": {"title": "Fixture source", "url": url}}
            for url in urls
        ],
    }


@pytest.mark.parametrize("message,urls,hosts,required,error", [
    ("Answer [1].", [OTHER], HOSTS, REQUIRED, "outside"),
    ("Answer [1].", [OTHER, OFFICIAL], HOSTS, REQUIRED, "outside"),
    ("Answer [2].", [OTHER, OFFICIAL], HOSTS, REQUIRED, None),
    ("Answer [1].", ["https://docs.example.org/other/"],
     HOSTS, REQUIRED, "every required"),
    ("Answer [1][2].", [OFFICIAL, OTHER], HOSTS, REQUIRED, "outside"),
    ("Answer [1].", ["https://docs.example.org.evil.example/page"],
     HOSTS, (), "outside"),
    ("Answer [1].", [OFFICIAL], (), REQUIRED, None),
    ("Answer [1].", [OTHER, OFFICIAL], (), REQUIRED, "every required"),
    ("Answer [1].", [OTHER], (), (), None),
])
def test_cited_source_constraints(message, urls, hosts, required, error):
    data = evidence(message, urls)
    kwargs = {
        "allowed_source_hosts": hosts,
        "required_source_urls": required,
    }
    if error:
        with pytest.raises(SearchError, match=error):
            vane._answer(data, **kwargs)
    else:
        result = vane._answer(data, **kwargs)
        assert message in result
        for number, url in enumerate(urls, 1):
            assert f"[{number}]" in result
            assert url in result


@pytest.mark.parametrize("hosts,required", [
    (["docs.example.org"], ()),
    (("*.example.org",), ()),
    (("Docs.Example.Org",), ()),
    (("docs.example.org/path",), ()),
    ((), [OFFICIAL]),
    ((), ("javascript:alert(1)",)),
    ((), (OFFICIAL + "#section",)),
    (HOSTS, (OTHER,)),
])
def test_invalid_policy_is_rejected(hosts, required):
    with pytest.raises(SearchError):
        vane._validate_source_policy(hosts, required)


@pytest.mark.asyncio
async def test_invalid_policy_rejected_before_configuration(monkeypatch):
    configuration = Mock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(vane, "_configuration", configuration)
    with pytest.raises(SearchError, match="bounded tuples"):
        await vane.vane_answer(
            "question", remaining=2, allowed_source_hosts=[]
        )
    configuration.assert_not_called()


@pytest.mark.asyncio
async def test_research_forwards_constraints(monkeypatch):
    monkeypatch.setenv("ATLAS_RESEARCH_ENABLED", "1")
    monkeypatch.setenv("ATLAS_RESEARCH_BACKEND", "vane")
    run = AsyncMock(return_value="Answer [1].")
    monkeypatch.setattr(vane, "vane_answer", run)
    await research.research_answer(
        object(), "question", remaining=2,
        allowed_source_hosts=HOSTS, required_source_urls=REQUIRED,
    )
    run.assert_awaited_once_with(
        "question", remaining=2,
        allowed_source_hosts=HOSTS, required_source_urls=REQUIRED,
    )


@pytest.mark.asyncio
async def test_searxng_does_not_silently_ignore_constraints(monkeypatch):
    monkeypatch.setenv("ATLAS_RESEARCH_ENABLED", "1")
    monkeypatch.setenv("ATLAS_RESEARCH_BACKEND", "searxng")
    search = AsyncMock()
    monkeypatch.setattr(research, "search_web", search)
    with pytest.raises(SearchError, match="require the Vane backend"):
        await research.research_answer(
            object(), "question", remaining=2,
            allowed_source_hosts=HOSTS,
        )
    search.assert_not_awaited()
