import ast
from pathlib import Path

import pytest

from chaos import codefaults as cf
from chaos.scenarios import SPECS

ROOT = Path(__file__).resolve().parent.parent

CASES = [
    (cf.ORDERS, cf.patch_n_plus_one),
    (cf.CHECKOUT, cf.patch_cpu_hog),
    (cf.CHECKOUT, cf.patch_event_loop),
    (cf.RECEIPTS, cf.patch_fd_leak),
]


@pytest.mark.parametrize(("rel", "patch"), CASES)
def test_patch_applies_and_compiles(rel, patch):
    src = (ROOT / rel).read_text()
    out = patch(src)
    assert out != src
    ast.parse(out)


def test_patches_fail_loudly_on_drift():
    with pytest.raises(cf.ChaosError):
        cf.patch_fd_leak("nothing here")


def test_n_plus_one_keeps_items_attribute_usable():
    out = cf.patch_n_plus_one((ROOT / cf.ORDERS).read_text())
    assert "set_committed_value(order, \"items\", items)" in out
    assert "selectinload" not in out


def test_specs_registered_as_held_out_with_loads():
    from chaos import load

    for sid in (3, 9, 10, 14):
        sp = SPECS[sid]
        assert sp.held_out
        assert all(name in load.PROFILES for name in sp.loads)
        assert sp.expect
