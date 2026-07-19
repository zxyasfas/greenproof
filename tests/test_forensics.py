from greenproof.forensics import compare_file

BASE = '''
import pytest

def test_a():
    assert f() == 1
    assert g() == 2

def test_b():
    with pytest.raises(ValueError):
        h()

def test_c():
    assert k()
'''


def test_deleted_test_is_flagged():
    cur = BASE.replace("def test_c():\n    assert k()\n", "")
    fc = compare_file("t.py", BASE, cur)
    assert "test_c" in fc.deleted_tests


def test_disabled_test_is_flagged():
    cur = BASE.replace("def test_b():", "@pytest.mark.skip\ndef test_b():")
    fc = compare_file("t.py", BASE, cur)
    assert "test_b" in fc.disabled_tests


def test_weakened_asserts_is_flagged():
    cur = BASE.replace("    assert f() == 1\n    assert g() == 2\n", "    assert f() == 1\n")
    fc = compare_file("t.py", BASE, cur)
    assert any(n == "test_a" and b == 2 and a == 1 for (n, b, a) in fc.weakened_tests)


def test_gutted_test_is_flagged():
    cur = BASE.replace("def test_c():\n    assert k()\n", "def test_c():\n    pass\n")
    fc = compare_file("t.py", BASE, cur)
    assert "test_c" in fc.gutted_tests


def test_adding_a_test_is_not_flagged():
    cur = BASE + "\ndef test_d():\n    assert extra() == 3\n"
    fc = compare_file("t.py", BASE, cur)
    assert not fc.has_changes()


def test_renaming_a_local_is_not_flagged():
    cur = BASE.replace("assert f() == 1", "value = f()\n    assert value == 1")
    fc = compare_file("t.py", BASE, cur)
    assert not fc.has_changes()


def test_reordering_asserts_is_not_flagged():
    cur = BASE.replace(
        "    assert f() == 1\n    assert g() == 2\n",
        "    assert g() == 2\n    assert f() == 1\n",
    )
    fc = compare_file("t.py", BASE, cur)
    assert not fc.has_changes()
