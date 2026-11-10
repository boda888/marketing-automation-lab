"""One ``write(rows, crm=...)`` for all three CRMs, feeding one shared ``IdMap``.

Each writer (HubSpot batch upsert, Attio assert, Pipedrive search → update / create) already
returns one ``RowOutcome`` per input row. This module picks the writer, runs it, and stores
every landed ``key -> crm id`` in the ``IdMap`` **under the same namespace the merge-webhook
parsers use**, so a later merge event re-points exactly the keys this sync wrote:

=========  ==============================  =========================================
CRM        IdMap namespace                 written by
=========  ==============================  =========================================
hubspot    ``hubspot:contact``             ``parse_hubspot_merges`` (``contact.merge``)
attio      ``attio:<object UUID>``         ``parse_attio_merges`` (``id.object_id``)
pipedrive  ``pipedrive:person``            ``parse_pipedrive_merge`` / person cache
=========  ==============================  =========================================

Attio merge webhooks name the object by UUID, while the assert path usually uses the slug
(``people``). With an ``IdMap`` wired, an Attio write therefore needs ``attio_object_id``
(or a UUID as ``object``); otherwise ids would be stored where no merge ever finds them,
and the call is refused instead of silently drifting.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Collection
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

from .attio import EMAIL_ATTRIBUTE, execute_asserts, plan_asserts
from .executor import RateBudget, Transport, UpsertReport, execute_upserts
from .hubspot import plan_upserts
from .idmap import IdMap, IdMapPersonCache
from .models import CrmRow, Outcome, RowOutcome
from .pipedrive import execute_persons, plan_persons

CRMS = ("hubspot", "attio", "pipedrive")
LANDED = frozenset({Outcome.CREATED, Outcome.UPDATED, Outcome.UPSERTED})
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


@dataclass(frozen=True)
class WriteOptions:
    """Per-CRM knobs; each writer reads only its own."""

    id_property: str = "email"  # hubspot: email or the portal's own unique property
    object: str = "people"  # attio: object slug or UUID
    attio_object_id: str | None = None  # attio: object UUID, as merge webhooks name it
    matching_attribute: str = EMAIL_ATTRIBUTE  # attio
    multiselect: Collection[str] = (EMAIL_ATTRIBUTE, "phone_numbers")  # attio
    overwrite: Collection[str] = ()  # attio: multiselects this sync owns


@dataclass
class WriteResult:
    crm: str
    outcomes: list[RowOutcome]
    summary: dict[str, object]
    idmap_namespace: str | None = None
    idmap_recorded: int = 0  # landed rows whose key -> id is now in the IdMap
    report: UpsertReport | None = field(default=None, repr=False)


def idmap_namespace(crm: str, options: WriteOptions) -> str:
    """The ``IdMap`` namespace a merge webhook for the written object will use."""
    if crm == "hubspot":
        return "hubspot:contact"
    if crm == "pipedrive":
        return "pipedrive:person"
    if crm == "attio":
        object_id = options.attio_object_id or options.object
        if not _UUID.match(object_id or ""):
            raise ValueError(
                "attio merge webhooks name the object by UUID: pass attio_object_id "
                f"(object {options.object!r} is a slug) or write without an IdMap"
            )
        return f"attio:{object_id.lower()}"
    raise ValueError(f"unknown crm {crm!r}; expected one of {', '.join(CRMS)}")


def write(
    rows: list[CrmRow],
    *,
    crm: str,
    transport: Transport,
    token: str,
    budget: RateBudget,
    idmap: IdMap | None = None,
    options: WriteOptions | None = None,
    allow_fixtures: bool = False,
    lock: AbstractContextManager | None = None,
    max_attempts: int = 3,
    max_wait_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> WriteResult:
    """Plan and send ``rows`` to ``crm``; one outcome per input row, in input order.

    Landed rows (created / updated / upserted with an id) are put into ``idmap``. Pipedrive
    also *reads* it before searching, so an id re-pointed by a merge webhook is used directly.
    Failed and skipped rows never touch the map. ``base_url`` is each writer's default.
    """
    opts = options or WriteOptions()
    if crm not in CRMS:
        raise ValueError(f"unknown crm {crm!r}; expected one of {', '.join(CRMS)}")
    namespace = idmap_namespace(crm, opts) if idmap is not None else None
    common = {
        "transport": transport,
        "token": token,
        "budget": budget,
        "max_attempts": max_attempts,
        "max_wait_seconds": max_wait_seconds,
        "sleep": sleep,
    }
    report: UpsertReport
    if crm == "hubspot":
        plan = plan_upserts(rows, id_property=opts.id_property, allow_fixtures=allow_fixtures)
        report = execute_upserts(plan, **common)
    elif crm == "attio":
        plan = plan_asserts(
            rows,
            object=opts.object,
            matching_attribute=opts.matching_attribute,
            multiselect=opts.multiselect,
            overwrite=opts.overwrite,
            allow_fixtures=allow_fixtures,
        )
        report = execute_asserts(plan, **common)
    else:
        cache = IdMapPersonCache(idmap, namespace) if idmap is not None else None
        plan = plan_persons(rows, allow_fixtures=allow_fixtures)
        report = execute_persons(plan, cache=cache, lock=lock, **common)

    recorded = 0
    if idmap is not None and namespace is not None:
        for outcome in report.outcomes:
            if outcome.outcome in LANDED and outcome.key and outcome.crm_id:
                idmap.put(namespace, outcome.key, outcome.crm_id)
                recorded += 1
    return WriteResult(
        crm=crm,
        outcomes=report.outcomes,
        summary=report.summary(),
        idmap_namespace=namespace,
        idmap_recorded=recorded,
        report=report,
    )
