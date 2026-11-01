"""Resolve a campaign by its explicit name: no hard-coded ids, no silent fallback."""

from __future__ import annotations

from collections.abc import Iterable

from .models import Campaign


class CampaignError(LookupError):
    """Base class: the push must stop, loudly."""


class CampaignNotFound(CampaignError):
    pass


class AmbiguousCampaign(CampaignError):
    pass


def _norm(name: str) -> str:
    return " ".join(name.split()).casefold()


def resolve_campaign(name: str, campaigns: Iterable[Campaign]) -> Campaign:
    """Return the one campaign whose name matches ``name``.

    Matching ignores case and repeated whitespace only. Zero matches raises
    ``CampaignNotFound`` (listing near names), two or more raise ``AmbiguousCampaign``
    with every matching id. There is never a fallback to "the first" or "the latest".
    """
    wanted = _norm(name)
    if not wanted:
        raise CampaignNotFound("campaign name is blank")
    listed = list(campaigns)
    matches = [c for c in listed if _norm(c.name) == wanted]
    if len(matches) == 1:
        return matches[0]
    if matches:
        ids = ", ".join(c.campaign_id for c in matches)
        raise AmbiguousCampaign(f"{len(matches)} campaigns are named {name!r}: {ids}")
    near = sorted(c.name for c in listed if wanted in _norm(c.name) or _norm(c.name) in wanted)
    hint = f"; similar: {near}" if near else ""
    raise CampaignNotFound(f"no campaign named {name!r} among {len(listed)}{hint}")
