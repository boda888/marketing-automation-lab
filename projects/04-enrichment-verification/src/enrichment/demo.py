"""Offline demo service: the batch API wired to fakes that answer ``examples/demo-batch.json``.

No paid provider, no network, no DNS: one free ``FakeProvider`` and a ``FakeResolver`` with
records for the fictional ``.example`` companies in the dataset. Run it with

    uvicorn enrichment.demo:app --port 8004

and post the dataset to ``/v1/enrich/batch`` (directly or through the n8n workflow in
``n8n/enrich-and-route.json``). Every one of the five actions comes out at least once.
"""

from __future__ import annotations

from enrichment.api import create_app
from enrichment.fakes import FakeProvider
from enrichment.pipeline import EnrichmentPipeline
from enrichment.precheck import FakeResolver
from enrichment.waterfall import Waterfall

DNS = {
    ("harborline.example", "MX"): ["10 mx.harborline.example."],
    ("quillstone.example", "MX"): ["10 mx.quillstone.example."],
    ("oldmill.example", "MX"): ["10 mx.oldmill.example."],
    ("parkedlot.example", "MX"): ["0 ."],  # null MX (RFC 7505): accepts no mail
}

FINDER_ANSWERS = {
    "https://www.linkedin.com/in/demo-ada": "ada@harborline.example",
    "https://www.linkedin.com/in/demo-hugo": "hugo@quillstone.example",
    # Still answers with the previous employer's address.
    "https://www.linkedin.com/in/demo-cara": "cara@oldmill.example",
}

FINDER_TIMEOUTS = frozenset({"https://www.linkedin.com/in/demo-jo"})


def demo_pipeline() -> EnrichmentPipeline:
    finder = FakeProvider(
        name="demo_finder",
        output_field="email",
        input_sets=(("linkedin_url",),),
        answers=FINDER_ANSWERS,
        timeouts=FINDER_TIMEOUTS,
    )
    return EnrichmentPipeline(waterfall=Waterfall("email", (finder,)), resolver=FakeResolver(DNS))


app = create_app(demo_pipeline())
