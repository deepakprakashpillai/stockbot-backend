"""New research runs must carry the limitations of their actual data and fills."""

from app.application.research_limitations import display_research_limitations
from app.domain.option_spread_models import OptionSpreadAssumptions


def test_research_snapshot_exposes_current_limitations() -> None:
    snapshot = OptionSpreadAssumptions().snapshot()
    limitations = display_research_limitations(snapshot)
    assert isinstance(limitations, list)
    assert any("unfiltered for market event" in item for item in limitations)
    assert any("does not verify contract rollover" in item for item in limitations)
    assert any("intrabar breaches are not detected" in item for item in limitations)
    assert any("No account affordability constraint" in item for item in limitations)
    assert snapshot["executable_quotes"] is False


def test_display_labels_do_not_change_immutable_assumption_snapshot() -> None:
    snapshot = OptionSpreadAssumptions().snapshot()
    original = list(snapshot["limitations"])
    display_research_limitations(snapshot)
    assert snapshot["limitations"] == original
