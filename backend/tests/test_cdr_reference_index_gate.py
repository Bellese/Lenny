"""Unit tests for the CDR reference-param verification gate (#425).

`wait_for_cdr_reference_index` replaced a ~292s full CDR $reindex with a verification
that the index is already usable. These tests pin the two properties that swap made
load-bearing, without HAPI or Docker:

  1. It must NOT issue a $reindex -- that is the entire saving.
  2. A never-ready index must still raise a named fixture error against the shared
     budget, never pass silently. #425 exists because a gate warned instead.
"""

import pytest

from tests.integration import _index_gates
from tests.integration._index_gates import wait_for_cdr_reference_index
from tests.integration._setup_budget import GATE_TIMINGS, SetupBudget, reset_gate_timings


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _RecordingHttp:
    """Records every call the gate makes, so "no $reindex" is assertable."""

    def __init__(self, entries: list) -> None:
        self._entries = entries
        self.get_urls: list[str] = []
        self.post_urls: list[str] = []

    def get(self, url, timeout=None):  # noqa: ARG002 - signature parity with httpx
        self.get_urls.append(url)
        return _FakeResponse(200, {"entry": self._entries})

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: ARG002
        self.post_urls.append(url)
        return _FakeResponse(200, {})


@pytest.fixture(autouse=True)
def _clean_gate_timings():
    reset_gate_timings()
    yield
    reset_gate_timings()


@pytest.fixture
def ready_http(monkeypatch):
    http = _RecordingHttp(entries=[{"resource": {"id": "x"}}])
    monkeypatch.setattr(_index_gates, "httpx", http)
    return http


@pytest.fixture
def never_ready_http(monkeypatch):
    http = _RecordingHttp(entries=[])
    monkeypatch.setattr(_index_gates, "httpx", http)
    return http


def test_a_ready_index_passes_without_issuing_a_reindex(ready_http):
    """The saving is real only if no $reindex is POSTed."""
    wait_for_cdr_reference_index("http://cdr/fhir", "patient-1", SetupBudget(total_seconds=480))

    assert ready_http.post_urls == [], "gate must verify the index, never rebuild it"
    assert [u.split("/fhir/")[1].split("?")[0] for u in ready_http.get_urls] == [
        "Encounter",
        "Observation",
        "Condition",
    ]


def test_each_probe_is_patient_scoped(ready_http):
    """A bare row count would pass on an unusable index; the probe must be patient-scoped."""
    wait_for_cdr_reference_index("http://cdr/fhir", "patient-1", SetupBudget(total_seconds=480))

    assert all("patient=patient-1" in u for u in ready_http.get_urls)


def test_a_ready_index_records_one_timing_per_gate(ready_http):
    """The terminal summary is the only place a stalled gate becomes visible in CI."""
    wait_for_cdr_reference_index("http://cdr/fhir", "patient-1", SetupBudget(total_seconds=480))

    assert [name for name, _ in GATE_TIMINGS] == [
        "cdr-encounter-gate",
        "cdr-observation-gate",
        "cdr-condition-gate",
    ]


def test_a_ready_index_consumes_almost_none_of_the_budget(ready_http):
    """The point of the change: setup stops spending ~292s here."""
    budget = SetupBudget(total_seconds=480)
    wait_for_cdr_reference_index("http://cdr/fhir", "patient-1", budget)

    assert budget.remaining() > 470


def test_a_never_ready_index_raises_rather_than_warning(never_ready_http):
    """#425's core defect: a gate that warns hands the suite a known-bad server."""
    # A tiny non-zero allowance: allot() succeeds, the poll deadline is already past,
    # so the gate's own timeout path runs rather than the budget's refusal.
    with pytest.raises(RuntimeError):
        wait_for_cdr_reference_index("http://cdr/fhir", "patient-1", SetupBudget(total_seconds=0.01))


def test_the_gate_timeout_names_the_probe_and_the_escape_hatch(never_ready_http):
    """A job-timeout kill names nothing; this error must name what to look at."""
    with pytest.raises(RuntimeError) as exc:
        wait_for_cdr_reference_index("http://cdr/fhir", "patient-42", SetupBudget(total_seconds=0.01))

    message = str(exc.value)
    assert "cdr-encounter-gate" in message
    assert "Encounter?patient=patient-42" in message
    assert "INTEGRATION_FORCE_CDR_REINDEX=1" in message


def test_an_exhausted_budget_refuses_before_polling_and_names_the_gate(never_ready_http):
    """Distinct from the gate timeout: the budget is gone, so the gate never runs."""
    with pytest.raises(RuntimeError) as exc:
        wait_for_cdr_reference_index("http://cdr/fhir", "patient-1", SetupBudget(total_seconds=0))

    message = str(exc.value)
    assert "cdr-encounter-gate" in message
    assert "budget" in message.lower()
    assert never_ready_http.get_urls == [], "must not probe once the budget is gone"
