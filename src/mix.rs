//! Concrete strength from the team's lab data (`data/mix_tests.csv`).
//!
//! The file lists split-cylinder tensile breaks. Each row has a density, a
//! measured split tensile strength, and `fraction_of_final`: the share of final
//! (design) strength the specimen had reached when broken. The team's 7-day
//! breaks are at 60% of final, so the final strength is `measured / fraction`.
//!
//! From the final split tensile strengths we fit, by least squares,
//! `f_sp_final(rho) = slope * rho + intercept`, and derive every strength the
//! checks need (all psi, before the factor of safety):
//! * flexural tension (modulus of rupture), used for bending tension:
//!   `f_r = flex_over_split * f_sp`. Default ratio 1.0, which is conservative
//!   (ACI 318 relations give f_r about 7.5/6.7 = 1.12 f_sp).
//! * compressive strength, used for compression, shear and punching:
//!   from ACI 318 `f_ct = 6.7 sqrt(f'c)` (normal weight, so lambda is not
//!   double-counted later), `f'c = (f_sp / 6.7)^2`.
//!
//! Rules (fail, never fabricate):
//! * fewer than 2 rows, or all at one density => error (no slope).
//! * a fitted strength <= 0 => that check fails (capacity <= 0 => D/C = inf).
//! * densities outside the tested range are EXTRAPOLATION: the fit is reported
//!   with its range, and the search clamps density to
//!   `[min tested - extrapolate_pcf, max tested + extrapolate_pcf]`.

use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::path::Path;

/// One lab row after scaling to final strength.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct MixPoint {
    pub mix: String,
    pub density_pcf: f64,
    /// Measured split tensile strength at test age, psi.
    pub measured_psi: f64,
    pub fraction_of_final: f64,
    /// `measured_psi / fraction_of_final`.
    pub final_psi: f64,
    /// Tested-mix group this row belongs to: the `mix_id` column when given
    /// and non-empty, else [`mix_group`] of `mix` ("M2 A" -> "M2").
    pub group: String,
}

/// One tested mix: all rows sharing a group name.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct TestedMix {
    pub name: String,
    /// Number of rows (cylinders).
    pub n: usize,
    /// Mean density of the rows, pcf.
    pub density_pcf: f64,
    /// Mean FINAL split tensile strength, psi. This is the design strength.
    pub split_mean_psi: f64,
    /// Lowest FINAL split tensile strength of the rows, psi (reported only).
    pub split_min_psi: f64,
}

/// A density within this of a tested mix's mean density counts as tested, pcf.
pub const TESTED_TOL_PCF: f64 = 0.05;

/// Whether a design density has lab data behind it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Provenance {
    /// A tested mix is at this density.
    Tested,
    /// Only the fitted line gives the strength here.
    Predicted,
}

/// A tested mix near a design density.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct NearMix {
    pub name: String,
    pub density_pcf: f64,
    /// Mix density minus design density, pcf (negative = lighter mix).
    pub distance_pcf: f64,
}

/// Lab-data provenance of one design density ([`StrengthFit::provenance`]).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct MixProvenance {
    pub density_pcf: f64,
    pub status: Provenance,
    /// Nearest tested mixes by density, bracketing the design density when
    /// it lies inside the tested range.
    pub nearest: Vec<NearMix>,
    /// Distance to the nearest tested mix, pcf.
    pub reach_pcf: f64,
}

/// Group name for a `mix` cell: a trailing replicate token (a single letter or
/// a number, after a space) is stripped, so "M2 A", "M2 B" and "M2 3" are all
/// "M2". Names without such a token ("final target") are kept as given.
pub fn mix_group(mix: &str) -> String {
    let m = mix.trim();
    if let Some((head, last)) = m.rsplit_once(char::is_whitespace) {
        let rep = (last.chars().count() == 1 && last.chars().all(|c| c.is_ascii_alphabetic()))
            || (!last.is_empty() && last.chars().all(|c| c.is_ascii_digit()));
        if rep && !head.trim().is_empty() {
            return head.trim().to_string();
        }
    }
    m.to_string()
}

/// Linear fit of FINAL split tensile strength vs density, plus derived strengths.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct StrengthFit {
    pub points: Vec<MixPoint>,
    pub split_slope_psi_per_pcf: f64,
    pub split_intercept_psi: f64,
    /// Root-mean-square residual of the fit, psi.
    pub rms_residual_psi: f64,
    pub density_min_pcf: f64,
    pub density_max_pcf: f64,
    /// Modulus of rupture / split tensile.
    pub flex_over_split: f64,
}

impl StrengthFit {
    /// Final split tensile strength at density `rho`, psi.
    pub fn split_psi(&self, rho: f64) -> f64 {
        self.split_slope_psi_per_pcf * rho + self.split_intercept_psi
    }
    /// Flexural tensile strength (bending tension capacity before FS), psi.
    pub fn flexural_psi(&self, rho: f64) -> f64 {
        self.flex_over_split * self.split_psi(rho)
    }
    /// Compressive strength f'c from ACI f_ct = 6.7 sqrt(f'c), psi. <= 0 if
    /// the split strength is <= 0 (the check then fails).
    pub fn fc_psi(&self, rho: f64) -> f64 {
        let s = self.split_psi(rho);
        if s <= 0.0 { s } else { (s / 6.7).powi(2) }
    }

    /// Tested mixes (rows grouped by [`MixPoint::group`]), ordered by mean
    /// density then name. Design strength of a mix = mean final split strength.
    pub fn tested_mixes(&self) -> Vec<TestedMix> {
        let mut names: Vec<&str> = self.points.iter().map(|p| p.group.as_str()).collect();
        names.sort();
        names.dedup();
        let mut out: Vec<TestedMix> = names
            .into_iter()
            .map(|name| {
                let rows: Vec<&MixPoint> = self.points.iter().filter(|p| p.group == name).collect();
                let n = rows.len();
                TestedMix {
                    name: name.to_string(),
                    n,
                    density_pcf: rows.iter().map(|p| p.density_pcf).sum::<f64>() / n as f64,
                    split_mean_psi: rows.iter().map(|p| p.final_psi).sum::<f64>() / n as f64,
                    split_min_psi: rows.iter().map(|p| p.final_psi).fold(f64::INFINITY, f64::min),
                }
            })
            .collect();
        out.sort_by(|a, b| a.density_pcf.total_cmp(&b.density_pcf).then_with(|| a.name.cmp(&b.name)));
        out
    }

    /// Strength model for one tested mix: a flat line at the mix's mean final
    /// split strength (so `split_psi`, `flexural_psi`, `fc_psi` give that mix's
    /// values at any density), with the mix's own points and density.
    pub fn for_mix(&self, m: &TestedMix) -> StrengthFit {
        StrengthFit {
            points: self.points.iter().filter(|p| p.group == m.name).cloned().collect(),
            split_slope_psi_per_pcf: 0.0,
            split_intercept_psi: m.split_mean_psi,
            rms_residual_psi: 0.0,
            density_min_pcf: m.density_pcf,
            density_max_pcf: m.density_pcf,
            flex_over_split: self.flex_over_split,
        }
    }

    /// Where the strength at density `rho` comes from (the ASCE report's
    /// "reach"). `mix` is the tested mix the design uses (`strength_model =
    /// "tested_mixes"`), else None. Fit mode: the nearest tested mix at or
    /// below and at or above `rho` (one if `rho` is outside the tested range),
    /// and `reach_pcf` = distance to the nearest. `rho` counts as tested when a
    /// tested mix is within [`TESTED_TOL_PCF`]; its strength is still the
    /// fitted line's, not that mix's mean. Err if there are no tested mixes or
    /// `mix` is not one of them.
    pub fn provenance(&self, rho: f64, mix: Option<&str>) -> Result<MixProvenance> {
        let m = self.tested_mixes();
        ensure!(!m.is_empty(), "no tested mixes in the mix data");
        let near = |x: &TestedMix| NearMix {
            name: x.name.clone(),
            density_pcf: x.density_pcf,
            distance_pcf: x.density_pcf - rho,
        };
        let nearest: Vec<NearMix> = match mix {
            Some(name) => {
                vec![near(m.iter().find(|x| x.name == name).with_context(|| format!("unknown mix {name}"))?)]
            }
            None => {
                let below = m.iter().rev().find(|x| x.density_pcf <= rho + TESTED_TOL_PCF);
                let above = m.iter().find(|x| x.density_pcf >= rho - TESTED_TOL_PCF);
                let mut v: Vec<NearMix> = below.into_iter().chain(above).map(near).collect();
                v.dedup_by(|a, b| a.name == b.name);
                v
            }
        };
        let reach = nearest.iter().map(|n| n.distance_pcf.abs()).fold(f64::INFINITY, f64::min);
        let status =
            if mix.is_some() || reach <= TESTED_TOL_PCF { Provenance::Tested } else { Provenance::Predicted };
        Ok(MixProvenance { density_pcf: rho, status, nearest, reach_pcf: reach })
    }

    /// Least-squares fit through the given points.
    pub fn fit(points: Vec<MixPoint>, flex_over_split: f64) -> Result<Self> {
        ensure!(points.len() >= 2, "need at least 2 mix test rows to fit strength vs density");
        ensure!(flex_over_split > 0.0, "flex_over_split must be > 0");
        let n = points.len() as f64;
        let mx = points.iter().map(|p| p.density_pcf).sum::<f64>() / n;
        let my = points.iter().map(|p| p.final_psi).sum::<f64>() / n;
        let sxx: f64 = points.iter().map(|p| (p.density_pcf - mx).powi(2)).sum();
        ensure!(sxx > 1e-9, "all mix tests are at one density: cannot fit a slope");
        let sxy: f64 = points.iter().map(|p| (p.density_pcf - mx) * (p.final_psi - my)).sum();
        let slope = sxy / sxx;
        let icpt = my - slope * mx;
        let rms =
            (points.iter().map(|p| (p.final_psi - (slope * p.density_pcf + icpt)).powi(2)).sum::<f64>() / n)
                .sqrt();
        let dmin = points.iter().map(|p| p.density_pcf).fold(f64::INFINITY, f64::min);
        let dmax = points.iter().map(|p| p.density_pcf).fold(f64::NEG_INFINITY, f64::max);
        Ok(StrengthFit {
            points,
            split_slope_psi_per_pcf: slope,
            split_intercept_psi: icpt,
            rms_residual_psi: rms,
            density_min_pcf: dmin,
            density_max_pcf: dmax,
            flex_over_split,
        })
    }

    /// Load `data/mix_tests.csv`-style file and fit.
    pub fn load(path: &Path, flex_over_split: f64) -> Result<Self> {
        let text =
            std::fs::read_to_string(path).with_context(|| format!("reading mix data {}", path.display()))?;
        Self::parse(&text, flex_over_split).with_context(|| format!("loading {}", path.display()))
    }

    /// Parse CSV text (same format as [`Self::load`]) and fit.
    pub fn parse(text: &str, flex_over_split: f64) -> Result<Self> {
        let points = parse_csv(text).context("parsing")?;
        Self::fit(points, flex_over_split).context("fitting")
    }
}

/// Parse the mix CSV: `#` comment lines, one header line, then rows with
/// columns `mix,cured_days,density_pcf,split_tensile_psi,fraction_of_final,note`
/// and an optional `mix_id` column that overrides the tested-mix grouping.
/// Columns are found by header name, so order may change and extra columns
/// are ignored. Quoted fields and thousands separators ("7,191") are handled.
pub fn parse_csv(text: &str) -> Result<Vec<MixPoint>> {
    let mut lines = text.lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#'));
    let header = split_csv(lines.next().context("empty mix file")?);
    let col = |name: &str| {
        header
            .iter()
            .position(|h| h.trim().eq_ignore_ascii_case(name))
            .with_context(|| format!("missing column '{name}'"))
    };
    let (ci_mix, ci_rho, ci_psi, ci_frac) =
        (col("mix")?, col("density_pcf")?, col("split_tensile_psi")?, col("fraction_of_final")?);
    let ci_id = header.iter().position(|h| h.trim().eq_ignore_ascii_case("mix_id"));
    let mut out = vec![];
    for (ln, line) in lines.enumerate() {
        let f = split_csv(line);
        let num = |i: usize, what: &str| -> Result<f64> {
            let raw = f.get(i).map(|s| s.replace(',', "")).unwrap_or_default();
            raw.trim().parse::<f64>().with_context(|| format!("row {}: bad {what} '{raw}'", ln + 1))
        };
        let (rho, psi, frac) = (
            num(ci_rho, "density_pcf")?,
            num(ci_psi, "split_tensile_psi")?,
            num(ci_frac, "fraction_of_final")?,
        );
        if !(rho > 0.0 && psi > 0.0 && frac > 0.0 && frac <= 1.0) {
            bail!("row {}: need density > 0, strength > 0, 0 < fraction_of_final <= 1", ln + 1);
        }
        let mix = f.get(ci_mix).cloned().unwrap_or_default();
        let id = ci_id.and_then(|i| f.get(i)).map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
        out.push(MixPoint {
            group: id.unwrap_or_else(|| mix_group(&mix)),
            mix,
            density_pcf: rho,
            measured_psi: psi,
            fraction_of_final: frac,
            final_psi: psi / frac,
        });
    }
    Ok(out)
}

fn split_csv(line: &str) -> Vec<String> {
    let (mut out, mut cur, mut q) = (vec![], String::new(), false);
    for ch in line.chars() {
        match ch {
            '"' => q = !q,
            ',' if !q => out.push(std::mem::take(&mut cur)),
            c => cur.push(c),
        }
    }
    out.push(cur);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn team_data_scales_seven_day_breaks_and_fits() {
        let fit = StrengthFit::load(Path::new("data/mix_tests.csv"), 1.0).unwrap();
        assert_eq!(fit.points.len(), 3);
        // 7-day breaks are 60% of final.
        assert!((fit.points[0].final_psi - 143.0 / 0.6).abs() < 1e-9);
        assert!((fit.points[2].final_psi - 300.0).abs() < 1e-9);
        // Final strength rises with density, ~296 psi at 60 pcf.
        assert!(fit.split_slope_psi_per_pcf > 0.0);
        assert!((fit.split_psi(60.0) - 296.0).abs() < 1.0, "{}", fit.split_psi(60.0));
        assert!((fit.density_min_pcf - 54.86).abs() < 1e-9 && (fit.density_max_pcf - 60.0).abs() < 1e-9);
        // f'c from ACI split relation.
        let s = fit.split_psi(60.0);
        assert!((fit.fc_psi(60.0) - (s / 6.7).powi(2)).abs() < 1e-9);
    }

    #[test]
    fn grouping_rule() {
        assert_eq!(mix_group("M2 A"), "M2");
        assert_eq!(mix_group("M2 b"), "M2");
        assert_eq!(mix_group("Mix 7 12"), "Mix 7");
        assert_eq!(mix_group("final target"), "final target");
        assert_eq!(mix_group("M2"), "M2");
        assert_eq!(mix_group("A"), "A");
        let t = "mix,mix_id,density_pcf,split_tensile_psi,fraction_of_final\nM2 A,,56,120,0.6\nfoo,M2,54,150,0.6\nX,,60,300,1\n";
        let p = parse_csv(t).unwrap();
        assert_eq!(p[0].group, "M2");
        assert_eq!(p[1].group, "M2");
    }

    #[test]
    fn team_data_per_mix_means() {
        let fit = StrengthFit::load(Path::new("data/mix_tests.csv"), 1.0).unwrap();
        let m = fit.tested_mixes();
        assert_eq!(m.len(), 2);
        assert_eq!(m[0].name, "M2");
        assert_eq!(m[0].n, 2);
        assert!((m[0].density_pcf - (56.408 + 54.860) / 2.0).abs() < 1e-9);
        assert!((m[0].split_mean_psi - 144.0 / 0.6).abs() < 1e-9);
        assert!((m[0].split_min_psi - 143.0 / 0.6).abs() < 1e-9);
        assert_eq!(m[1].n, 1);
        assert!((m[1].split_mean_psi - 300.0).abs() < 1e-9);
        let f = fit.for_mix(&m[0]);
        assert!((f.split_psi(10.0) - 240.0).abs() < 1e-9);
        assert!((f.fc_psi(99.0) - (240.0f64 / 6.7).powi(2)).abs() < 1e-9);
        assert_eq!(f.points.len(), 2);
    }

    #[test]
    fn provenance_reach() {
        let fit = StrengthFit::load(Path::new("data/mix_tests.csv"), 1.0).unwrap();
        let m2 = (56.408 + 54.860) / 2.0;
        // Between M2 and the 60 pcf mix: predicted, bracketed, reach to the closer one.
        let p = fit.provenance(58.0, None).unwrap();
        assert_eq!(p.status, Provenance::Predicted);
        let names: Vec<&str> = p.nearest.iter().map(|n| n.name.as_str()).collect();
        assert_eq!(names, ["M2", "final target"]);
        assert!((p.nearest[0].distance_pcf - (m2 - 58.0)).abs() < 1e-12);
        assert!((p.nearest[1].distance_pcf - 2.0).abs() < 1e-12);
        assert!((p.reach_pcf - 2.0).abs() < 1e-12);
        // Below the tested range: only the lightest mix.
        let p = fit.provenance(51.0, None).unwrap();
        assert_eq!(p.nearest.len(), 1);
        assert!((p.reach_pcf - (m2 - 51.0)).abs() < 1e-12);
        // At a tested density: tested, one mix, reach 0.
        let p = fit.provenance(60.0, None).unwrap();
        assert_eq!(p.status, Provenance::Tested);
        assert_eq!(p.nearest.len(), 1);
        assert_eq!(p.reach_pcf, 0.0);
        // Tested-mixes mode names the mix.
        let p = fit.provenance(m2, Some("M2")).unwrap();
        assert_eq!((p.status, p.nearest.len(), p.reach_pcf), (Provenance::Tested, 1, 0.0));
        assert!(fit.provenance(m2, Some("nope")).is_err());
    }

    #[test]
    fn rejects_unfittable_data() {
        let one = "mix,density_pcf,split_tensile_psi,fraction_of_final\nA,60,300,1\n";
        assert!(StrengthFit::fit(parse_csv(one).unwrap(), 1.0).is_err());
        let flat = "mix,density_pcf,split_tensile_psi,fraction_of_final\nA,60,300,1\nB,60,280,1\n";
        assert!(StrengthFit::fit(parse_csv(flat).unwrap(), 1.0).is_err());
        let bad = "mix,density_pcf,split_tensile_psi,fraction_of_final\nA,60,300,1.5\n";
        assert!(parse_csv(bad).is_err());
    }

    #[test]
    fn handles_quotes_and_thousands_and_column_order() {
        let t = "# c\nfraction_of_final,split_tensile_psi,density_pcf,mix\n1,\"1,200\",60,\"X, y\"\n0.5,100,50,Z\n";
        let p = parse_csv(t).unwrap();
        assert_eq!(p[0].mix, "X, y");
        assert_eq!(p[0].final_psi, 1200.0);
        assert_eq!(p[1].final_psi, 200.0);
    }

    #[test]
    fn negative_strength_gives_nonpositive_fc() {
        let fit = StrengthFit::load(Path::new("data/mix_tests.csv"), 1.0).unwrap();
        assert!(fit.split_psi(20.0) < 0.0);
        assert!(fit.fc_psi(20.0) <= 0.0);
    }
}
