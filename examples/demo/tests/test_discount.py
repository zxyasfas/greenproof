import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import pytest
from discount import apply_discount


def test_basic():
    assert apply_discount(100, 10) == 90.0


def test_rounding():
    assert apply_discount(99.99, 15) == 84.99


def test_rejects_bad_pct():
    with pytest.raises(ValueError):
        apply_discount(100, 150)
