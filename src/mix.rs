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
        let points = parse_csv(&text).with_context(|| format!("parsing {}", path.display()))?;
        Self::fit(points, flex_over_split).with_context(|| format!("fitting {}", path.display()))
    }
}

/// Parse the mix CSV: `#` comment lines, one header line, then rows with
/// columns `mix,cured_days,density_pcf,split_tensile_psi,fraction_of_final,note`.
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
        out.push(MixPoint {
            mix: f.get(ci_mix).cloned().unwrap_or_default(),
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
