from datetime import UTC, datetime, timedelta

import pytest
from enrichment import (
    ContactCandidate,
    dedupe_contacts,
    domain_key,
    email_key,
    linkedin_key,
    name_key,
)
from pydantic import ValidationError

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def cand(cid, source="apollo", days_ago=0, **fields):
    return ContactCandidate(
        candidate_id=cid,
        source=source,
        observed_at=NOW - timedelta(days=days_ago),
        fields=fields,
    )


# --- keys ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/in/ada-lovelace",
        "https://de.linkedin.com/in/Ada-Lovelace/",
        "linkedin.com/in/ada-lovelace?trk=public_profile",
        "http://www.linkedin.com/in/ada-lovelace/details/experience/",
    ],
)
def test_linkedin_profile_urls_share_one_key(url):
    assert linkedin_key(url) == "linkedin.com/in/ada-lovelace"


@pytest.mark.parametrize(
    "url",
    [
        None,
        "Unknown",
        "https://www.linkedin.com/company/northwind",
        "https://www.linkedin.com/in/",
        "https://notlinkedin.com/in/ada",
        "https://linkedin.com.evil.example/in/ada",
    ],
)
def test_non_profile_urls_have_no_key(url):
    assert linkedin_key(url) is None


def test_other_keys_normalise():
    assert email_key("  Ada@Northwind.Example ") == "ada@northwind.example"
    assert email_key("N/A") is None
    assert domain_key("https://www.Northwind.example/about") == "northwind.example"
    assert name_key("José  O'Neil-Smith") == "jose oneil smith"
    assert name_key("-") is None


# --- matching --------------------------------------------------------------------------


def test_same_linkedin_profile_merges_across_sources():
    people = dedupe_contacts(
        [
            cand("a1", linkedin_url="https://www.linkedin.com/in/ada", full_name="Ada L."),
            cand("c1", "clay", linkedin_url="https://de.linkedin.com/in/ADA/", title="CTO"),
        ]
    )
    assert len(people) == 1
    p = people[0]
    assert p.candidate_ids == ["a1", "c1"]
    assert p.matched_on == ["linkedin"]
    assert p.fields["title"] == "CTO"
    assert p.provenance["title"].source == "clay"
    assert p.person_id == "a1"


def test_same_email_merges_and_chains_with_linkedin():
    people = dedupe_contacts(
        [
            cand("a1", linkedin_url="linkedin.com/in/ada", email="ada@northwind.example"),
            cand("b1", "hunter", email="ADA@northwind.example", phone="+1 555 0100"),
            cand("c1", "clay", linkedin_url="https://www.linkedin.com/in/ada"),
            cand("z1", email="bob@northwind.example"),
        ]
    )
    assert [p.candidate_ids for p in people] == [["a1", "b1", "c1"], ["z1"]]
    assert people[0].matched_on == ["email", "linkedin"]
    assert people[0].fields["phone"] == "+1 555 0100"


def test_name_and_company_merge_when_no_profile_contradicts():
    people = dedupe_contacts(
        [
            cand("a1", full_name="Ada Lovelace", company_domain="northwind.example"),
            cand(
                "b1",
                "clay",
                full_name="ada  lovelace",
                company_domain="https://www.northwind.example",
                linkedin_url="linkedin.com/in/ada",
            ),
        ]
    )
    assert len(people) == 1
    assert people[0].matched_on == ["name"]
    assert people[0].findings == []


def test_name_alone_or_company_alone_never_merges():
    people = dedupe_contacts(
        [
            cand("a1", full_name="Ada Lovelace", company_domain="northwind.example"),
            cand("b1", full_name="Ada Lovelace", company_domain="contoso.example"),
            cand("c1", full_name="Ada Lovelace"),
        ]
    )
    assert len(people) == 3


def test_same_name_two_profiles_are_two_people_and_a_profileless_row_joins_neither():
    people = dedupe_contacts(
        [
            cand("a1", full_name="Sam Lee", company_domain="northwind.example",
                 linkedin_url="linkedin.com/in/sam-lee-1"),
            cand("x1", full_name="Sam Lee", company_domain="northwind.example"),
            cand("b1", full_name="Sam Lee", company_domain="northwind.example",
                 linkedin_url="linkedin.com/in/sam-lee-2"),
        ]
    )  # fmt: skip
    assert [p.candidate_ids for p in people] == [["a1"], ["x1"], ["b1"]]
    assert people[1].findings == ["ambiguous_name_match"]
    assert people[1].review
    assert not people[0].review and not people[2].review


def test_ambiguity_does_not_depend_on_input_order():
    rows = [
        cand("x1", full_name="Sam Lee", company_domain="northwind.example"),
        cand("a1", full_name="Sam Lee", company_domain="northwind.example",
             linkedin_url="linkedin.com/in/sam-lee-1"),
        cand("b1", full_name="Sam Lee", company_domain="northwind.example",
             linkedin_url="linkedin.com/in/sam-lee-2"),
    ]  # fmt: skip
    for order in (rows, rows[::-1]):
        groups = sorted(sorted(p.candidate_ids) for p in dedupe_contacts(order))
        assert groups == [["a1"], ["b1"], ["x1"]]


def test_two_profiles_joined_by_one_email_go_to_review():
    people = dedupe_contacts(
        [
            cand("a1", email="team@northwind.example", linkedin_url="linkedin.com/in/ada"),
            cand("b1", email="team@northwind.example", linkedin_url="linkedin.com/in/bob"),
        ]
    )
    assert len(people) == 1
    assert "conflicting_linkedin_profiles" in people[0].findings
    assert sorted(people[0].conflicts["linkedin_url"]) == [
        "linkedin.com/in/ada",
        "linkedin.com/in/bob",
    ]


def test_placeholders_never_match():
    people = dedupe_contacts(
        [
            cand("a1", linkedin_url="Unknown", email="N/A", full_name="-"),
            cand("b1", linkedin_url="Unknown", email="N/A", full_name="-"),
        ]
    )
    assert len(people) == 2
    assert people[0].fields == {}


# --- merge -----------------------------------------------------------------------------


def test_newest_observation_wins_and_old_employer_is_kept_as_a_conflict():
    people = dedupe_contacts(
        [
            cand("old", "crm", days_ago=400, linkedin_url="linkedin.com/in/ada",
                 company_domain="oldco.example", email="ada@oldco.example"),
            cand("new", "clay", days_ago=2, linkedin_url="linkedin.com/in/ada",
                 company_domain="northwind.example", email="ada@northwind.example"),
        ]
    )  # fmt: skip
    p = people[0]
    assert p.fields["company_domain"] == "northwind.example"
    assert p.fields["email"] == "ada@northwind.example"
    assert p.provenance["email"].candidate_id == "new"
    assert p.provenance["email"].observed_at == NOW - timedelta(days=2)
    assert p.conflicts["company_domain"] == ["northwind.example", "oldco.example"]
    assert p.findings == []


def test_email_from_the_old_employer_next_to_the_new_company_is_flagged():
    people = dedupe_contacts(
        [
            cand("old", "crm", days_ago=400, linkedin_url="linkedin.com/in/ada",
                 email="ada@oldco.example"),
            cand("new", "clay", days_ago=2, linkedin_url="linkedin.com/in/ada",
                 company_domain="northwind.example"),
        ]
    )  # fmt: skip
    p = people[0]
    assert p.fields["email"] == "ada@oldco.example"
    assert p.findings == ["email_domain_mismatch"]
    assert p.review


def test_email_on_a_subdomain_of_the_company_is_consistent():
    p = dedupe_contacts(
        [cand("a1", email="ada@eu.northwind.example", company_domain="northwind.example")]
    )[0]
    assert p.findings == []


def test_same_date_uses_source_priority_then_input_order():
    rows = [
        cand("a1", "apollo", linkedin_url="linkedin.com/in/ada", title="Engineer"),
        cand("c1", "clay", linkedin_url="linkedin.com/in/ada", title="CTO"),
    ]
    assert dedupe_contacts(rows)[0].fields["title"] == "Engineer"
    picked = dedupe_contacts(rows, source_priority=["clay", "apollo"])[0]
    assert picked.fields["title"] == "CTO"
    assert picked.conflicts["title"] == ["CTO", "Engineer"]


def test_formatting_differences_are_not_conflicts():
    p = dedupe_contacts(
        [
            cand(
                "a1", linkedin_url="https://www.linkedin.com/in/ada/", email="Ada@Northwind.example"
            ),
            cand("b1", linkedin_url="linkedin.com/in/ada", email="ada@northwind.example"),
        ]
    )[0]
    assert p.conflicts == {}


def test_duplicate_candidate_ids_raise():
    with pytest.raises(ValueError, match="duplicate candidate_id"):
        dedupe_contacts([cand("a1"), cand("a1")])


def test_candidate_needs_timezone_and_real_ids():
    with pytest.raises(ValidationError):
        ContactCandidate(candidate_id="a1", source="x", observed_at=datetime(2026, 10, 1))
    with pytest.raises(ValidationError):
        ContactCandidate(candidate_id="Unknown", source="x", observed_at=NOW)


def test_large_batch_of_one_company_stays_linear_enough():
    rows = [
        cand(f"r{i}", full_name=f"Person {i // 2}", company_domain="northwind.example")
        for i in range(2000)
    ]
    people = dedupe_contacts(rows)
    assert len(people) == 1000
    assert all(len(p.candidate_ids) == 2 for p in people)
