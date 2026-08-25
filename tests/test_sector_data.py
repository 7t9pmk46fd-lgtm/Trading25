"""Tests for shared/sector_data.py -- pure reference-data loading/aggregation,
no network, no MCP. Uses an in-memory tags dict rather than the real cache
file so these don't depend on data/sector_tags.json's current contents."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.sector_data import load_sector_tags, sector_concentration

TAGS = {
    "FANG": {"sector": "Energy", "industry": "Oil & Gas Exploration & Production"},
    "MPC": {"sector": "Energy", "industry": "Oil & Gas Refining & Marketing"},
    "AVGO": {"sector": "Technology", "industry": "Semiconductors"},
}


def test_load_sector_tags_missing_file_returns_empty_dict(tmp_path):
    assert load_sector_tags(path=tmp_path / "does_not_exist.json") == {}


def test_groups_by_sector_and_sums_pl():
    positions = [
        {"symbol": "FANG", "unrealized_pl": 500.0},
        {"symbol": "MPC", "unrealized_pl": 1000.0},
        {"symbol": "AVGO", "unrealized_pl": -100.0},
    ]
    result = sector_concentration(positions, tags=TAGS)

    assert result["total_positions"] == 3
    assert result["total_unrealized_pl"] == 1400.0
    assert result["by_sector"]["Energy"]["count"] == 2
    assert result["by_sector"]["Energy"]["unrealized_pl"] == 1500.0
    assert result["by_sector"]["Technology"]["count"] == 1


def test_unknown_symbol_lands_in_unknown_bucket_not_dropped():
    positions = [{"symbol": "ZZZZ", "unrealized_pl": 50.0}]
    result = sector_concentration(positions, tags=TAGS)

    assert result["by_sector"]["unknown"]["count"] == 1
    assert result["by_sector"]["unknown"]["symbols"] == ["ZZZZ"]


def test_percentages_sum_correctly():
    positions = [
        {"symbol": "FANG", "unrealized_pl": 300.0},
        {"symbol": "AVGO", "unrealized_pl": 100.0},
    ]
    result = sector_concentration(positions, tags=TAGS)

    assert result["by_sector"]["Energy"]["pct_of_positions"] == 0.5
    assert result["by_sector"]["Energy"]["pct_of_unrealized_pl"] == 0.75
