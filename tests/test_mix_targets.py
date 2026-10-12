# tests/test_mix_targets.py
"""
Theoretical vs buildable mixes.

The sweep designs against interpolated densities on purpose — those densities
are the specification handed to the mix team.  The risk is quoting a design
without saying which of its mixes exist, so these tests pin the distinction:
a density that IS a real mix, one interpolated between two close lab points,
and one the regression is simply extrapolating.
"""
from __future__ import annotations

import pytest

from reporting.mix_targets import (
    DensityVerdict, MixTargetSpec, classify_densities, format_spec,
    zone_names_for,
)

LAB = [60.0, 65.0, 70.0, 71.9, 80.0]


def _props(d):
    return {"strength_c": 90.0 * (d - 55.0) + 120.0,
            "strength_t": 18.0 * (d - 55.0) + 15.0}


class TestClassification:
    def test_exact_real_mix_is_on_record(self):
        v = classify_densities([65.0], LAB, _props)[0]
        assert v.is_real and v.matched_mix_pcf == 65.0
        assert v.confidence == "on record"
        assert v.reach_pcf is None

    def test_near_miss_within_tolerance_counts_as_real(self):
        """Nobody batches to 0.1 pcf; 65.1 IS the 65 mix."""
        v = classify_densities([65.1], LAB, _props)[0]
        assert v.is_real and v.matched_mix_pcf == 65.0

    def test_interpolated_density_reports_its_brackets(self):
        v = classify_densities([66.5], LAB, _props)[0]
        assert not v.is_real
        assert (v.below_pcf, v.above_pcf) == (65.0, 70.0)
        assert v.bracketed
        assert v.reach_pcf == pytest.approx(1.5)

    def test_short_reach_reads_as_trustworthy(self):
        v = classify_densities([66.0], LAB, _props)[0]
        assert v.confidence == "interpolated (short reach)"

    def test_long_reach_is_flagged_as_a_guess(self):
        """Midway across the 71.9-80 gap: bracketed, but 4 pcf from any data."""
        v = classify_densities([76.0], LAB, _props)[0]
        assert v.bracketed and v.reach_pcf == pytest.approx(4.0)
        assert "stretched" in v.confidence

    def test_outside_the_data_is_extrapolation_not_interpolation(self):
        v = classify_densities([57.5], LAB, _props)[0]
        assert not v.bracketed and v.below_pcf is None
        assert v.above_pcf == 60.0
        assert v.confidence == "EXTRAPOLATED"

    def test_required_strengths_come_from_the_sweeps_own_properties(self):
        v = classify_densities([66.5], LAB, _props)[0]
        assert v.fc_psi == pytest.approx(_props(66.5)["strength_c"])
        assert v.ft_psi == pytest.approx(_props(66.5)["strength_t"])

    def test_zone_names_label_the_ibeam(self):
        assert zone_names_for(3) == ["bottom flange", "web", "top flange"]
        assert zone_names_for(1) == ["uniform hull"]
        vs = classify_densities([66.5, 61.5, 66.5], LAB, _props,
                                zone_names_for(3))
        assert [v.zone for v in vs] == ["bottom flange", "web", "top flange"]

    def test_no_mixes_on_record_is_not_a_crash(self):
        v = classify_densities([66.5], [], _props)[0]
        assert not v.is_real and v.reach_pcf is None
        assert v.confidence == "no data"


class TestSpec:
    def _spec(self, densities=(66.5, 61.5, 66.5)):
        return MixTargetSpec(
            verdicts=classify_densities(densities, LAB, _props,
                                        zone_names_for(len(densities))),
            theoretical_weight_lb=106.53,
            buildable_weight_lb=112.38,
            buildable_mixes_pcf=[70.0, 65.0, 70.0],
            existing_pcf=list(LAB), regression_n=2,
            regression_source="lab_results")

    def test_needs_development_deduplicates_the_ibeam_flanges(self):
        """[66.5, 61.5, 66.5] is TWO mixes to develop, not three."""
        todo = self._spec().needs_development
        assert [v.density_pcf for v in todo] == [66.5, 61.5]

    def test_all_real_when_every_zone_is_on_record(self):
        s = self._spec((70.0, 65.0, 70.0))
        assert s.all_real and not s.needs_development

    def test_penalty_is_the_cost_of_developing_nothing(self):
        assert self._spec().buildable_penalty_lb == pytest.approx(5.85)

    def test_report_states_both_weights_and_labels_them(self):
        txt = format_spec(self._spec())
        assert "106.53" in txt and "112.38" in txt
        assert "Theoretical optimum" in txt
        assert "mixes currently on record" in txt

    def test_report_names_the_targets_and_their_strengths(self):
        txt = format_spec(self._spec())
        assert "66.5" in txt and "61.5" in txt
        assert "f'c" in txt and "f_t" in txt

    def test_report_carries_the_small_sample_caveat(self):
        txt = format_spec(self._spec())
        assert "n = 2" in txt
        assert "EXTRAPOLATION" in txt or "extrapolation" in txt.lower()
        assert "necessary but NOT sufficient" in txt

    def test_report_distinguishes_real_from_interpolated_per_zone(self):
        s = MixTargetSpec(
            verdicts=classify_densities([70.0, 61.5, 70.0], LAB, _props,
                                        zone_names_for(3)),
            theoretical_weight_lb=110.0, existing_pcf=list(LAB))
        txt = format_spec(s)
        assert "already on record" in txt          # the flanges
        assert "nearest real mixes" in txt         # the web

    def test_payoff_rows_render(self):
        s = self._spec()
        s.payoff = [{"label": "mixes on record today", "weight_lb": 112.38,
                     "note": "= the achievable fallback"},
                    {"label": "+ develop 66.5 pcf", "weight_lb": 109.1,
                     "note": "cumulative"},
                    {"label": "+ develop 61.5 pcf", "weight_lb": 106.53,
                     "note": "cumulative"}]
        txt = format_spec(s)
        assert "PROJECTED PAYOFF" in txt and "109.1" in txt

    def test_no_target_paying_off_alone_is_called_out(self):
        """The measured case: neither mix helps by itself because an I-beam
        needs a PAIR at least 2 pcf apart.  Developing one at a time would
        look like wasted lab work without this note."""
        s = self._spec()
        s.payoff = [
            {"label": "mixes on record today", "weight_lb": 112.38,
             "note": "= the achievable fallback"},
            {"label": "with only 66.5 pcf added", "weight_lb": 112.38,
             "note": "developed alone"},
            {"label": "with only 61.5 pcf added", "weight_lb": 112.38,
             "note": "developed alone"},
            {"label": "+ develop 61.5 pcf", "weight_lb": 106.53,
             "note": "cumulative"}]
        txt = format_spec(s)
        assert "no target pays off ALONE" in txt
        assert "2 pcf between batches" in txt

    def test_a_target_that_pays_off_alone_is_named_first(self):
        s = self._spec()
        s.payoff = [
            {"label": "mixes on record today", "weight_lb": 112.38,
             "note": "= the achievable fallback"},
            {"label": "with only 66.5 pcf added", "weight_lb": 112.38,
             "note": "developed alone"},
            {"label": "with only 61.5 pcf added", "weight_lb": 108.90,
             "note": "developed alone"}]
        txt = format_spec(s)
        assert "DEVELOP FIRST" in txt and "61.5" in txt
        assert "no target pays off ALONE" not in txt

    def test_payoff_shows_the_delta_against_the_fallback(self):
        s = self._spec()
        s.payoff = [{"label": "mixes on record today", "weight_lb": 112.38,
                     "note": "= the achievable fallback"},
                    {"label": "+ develop 61.5 pcf", "weight_lb": 106.53,
                     "note": "cumulative"}]
        txt = format_spec(s)
        assert "-5.85" in txt

    def test_spec_survives_a_sweep_that_needs_nothing(self):
        s = self._spec((70.0, 65.0, 70.0))
        txt = format_spec(s)
        assert "none, the optimum is buildable today" in txt
