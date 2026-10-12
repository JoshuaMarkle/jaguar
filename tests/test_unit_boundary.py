"""
Unit-boundary audit (task 6.3).

Tests for the ``domain.units.physical_range`` decorator and its application
to key physics boundary functions.

Also verifies that public functions in ``physics/`` carry unit documentation
(a ``[`` character in the docstring or a ``_physical_ranges`` attribute from
the decorator), catching functions that were added without unit annotation.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


# ══════════════════════════════════════════════════════════════════════════════
#  physical_range decorator unit tests
# ══════════════════════════════════════════════════════════════════════════════

class TestPhysicalRangeDecorator:
    """Verify decorator mechanics: acceptance, rejection, introspection."""

    @pytest.fixture(scope="class")
    def decorated_fn(self):
        from domain.units import physical_range

        @physical_range(L=(1.0, 15.0), T=(0.01, 1.0), V=(0.1, 10.0))
        def sample_fn(L, T, V, label="test"):
            return L * T * V

        return sample_fn

    def test_valid_call_passes_through(self, decorated_fn):
        result = decorated_fn(6.0, 0.20, 3.0)
        assert result == pytest.approx(6.0 * 0.20 * 3.0)

    def test_out_of_range_L_raises_value_error(self, decorated_fn):
        with pytest.raises(ValueError, match="'L'"):
            decorated_fn(L=0.5, T=0.20, V=3.0)   # L below 1.0

    def test_out_of_range_T_raises_value_error(self, decorated_fn):
        with pytest.raises(ValueError, match="'T'"):
            decorated_fn(6.0, T=1.5, V=3.0)       # T above 1.0

    def test_out_of_range_V_raises_value_error(self, decorated_fn):
        with pytest.raises(ValueError, match="'V'"):
            decorated_fn(6.0, 0.20, V=0.05)       # V below 0.1

    def test_at_exact_lower_bound_passes(self, decorated_fn):
        # Inclusive bound: L=1.0 must be accepted
        result = decorated_fn(1.0, 0.01, 0.1)
        assert result == pytest.approx(1.0 * 0.01 * 0.1)

    def test_at_exact_upper_bound_passes(self, decorated_fn):
        result = decorated_fn(15.0, 1.0, 10.0)
        assert result == pytest.approx(15.0 * 1.0 * 10.0)

    def test_non_numeric_argument_skipped(self, decorated_fn):
        # label is a string and has no declared range — should pass through
        result = decorated_fn(6.0, 0.20, 3.0, label="skip_me")
        assert result == pytest.approx(6.0 * 0.20 * 3.0)

    def test_introspection_attribute_present(self, decorated_fn):
        assert hasattr(decorated_fn, "_physical_ranges"), (
            "Decorated function must expose _physical_ranges attribute"
        )
        ranges = decorated_fn._physical_ranges
        assert "L" in ranges
        assert ranges["L"] == (1.0, 15.0)

    def test_docstring_preserved(self):
        from domain.units import physical_range

        @physical_range(x=(0.0, 100.0))
        def my_fn(x):
            """Original docstring."""
            return x

        assert my_fn.__doc__ == "Original docstring."

    def test_function_name_preserved(self):
        from domain.units import physical_range

        @physical_range(x=(0.0, 100.0))
        def well_named_function(x):
            return x

        assert well_named_function.__name__ == "well_named_function"


# ══════════════════════════════════════════════════════════════════════════════
#  total_resistance has physical_range applied
# ══════════════════════════════════════════════════════════════════════════════

class TestTotalResistanceBounds:
    """total_resistance must reject physically nonsensical inputs."""

    @pytest.fixture(scope="class")
    def dummy_offsets(self):
        import numpy as np
        from hull_generator import generate_hull
        return generate_hull([1.4, 1.4, 1.2, 1.0, 1.5, 0.15, 0.05],
                             6.0, 0.61, 0.20, 31, 15)

    def test_normal_call_succeeds(self, dummy_offsets):
        from wave_resistance import total_resistance
        res = total_resistance(dummy_offsets, L=6.0, B=0.61, T=0.20,
                               V=3.0, disp=0.45)
        assert res["Rt"] > 0.0

    def test_negative_length_raises(self, dummy_offsets):
        from wave_resistance import total_resistance
        with pytest.raises(ValueError, match="'L'"):
            total_resistance(dummy_offsets, L=0.5, B=0.61, T=0.20,
                             V=3.0, disp=0.45)

    def test_zero_speed_raises(self, dummy_offsets):
        from wave_resistance import total_resistance
        with pytest.raises(ValueError, match="'V'"):
            total_resistance(dummy_offsets, L=6.0, B=0.61, T=0.20,
                             V=0.0, disp=0.45)

    def test_negative_draft_raises(self, dummy_offsets):
        from wave_resistance import total_resistance
        with pytest.raises(ValueError, match="'T'"):
            total_resistance(dummy_offsets, L=6.0, B=0.61, T=-0.1,
                             V=3.0, disp=0.45)


# ══════════════════════════════════════════════════════════════════════════════
#  physics/ package unit documentation audit
# ══════════════════════════════════════════════════════════════════════════════

def _public_functions_in_package(pkg_name: str):
    """
    Yield (qualified_name, fn) for every non-private function defined
    directly (not imported) in modules under *pkg_name*.
    """
    pkg = importlib.import_module(pkg_name)
    pkg_path = Path(pkg.__file__).parent
    results = []

    for mod_info in pkgutil.walk_packages([str(pkg_path)], prefix=pkg_name + "."):
        try:
            mod = importlib.import_module(mod_info.name)
        except Exception:
            continue
        for attr_name, obj in inspect.getmembers(mod, inspect.isfunction):
            if attr_name.startswith("_"):
                continue
            # Only functions defined in this module (not re-exports)
            if getattr(obj, "__module__", "") != mod_info.name:
                continue
            results.append((f"{mod_info.name}.{attr_name}", obj))
    return results


class TestPhysicsFunctionUnitAnnotations:
    """
    Every public function in physics/ must have unit documentation —
    either a docstring mentioning '[' (unit bracket) or a
    _physical_ranges attribute from the @physical_range decorator.

    This enforces task 6.3: unit-boundary audit.
    """

    @pytest.fixture(scope="class")
    def physics_fns(self):
        return _public_functions_in_package("physics")

    def test_physics_package_has_public_functions(self, physics_fns):
        assert len(physics_fns) >= 2, (
            "Expected at least 2 public functions in physics/; found none — "
            "check the package structure."
        )

    def test_all_physics_functions_have_unit_docs(self, physics_fns):
        missing = []
        for qname, fn in physics_fns:
            has_bracket_doc = bool(
                fn.__doc__ and "[" in fn.__doc__
            )
            has_range_attr = hasattr(fn, "_physical_ranges")
            if not (has_bracket_doc or has_range_attr):
                missing.append(qname)

        assert not missing, (
            "The following physics/ functions lack unit documentation "
            "(add '[unit]' to the docstring or apply @physical_range):\n"
            + "\n".join(f"  {n}" for n in missing)
        )
