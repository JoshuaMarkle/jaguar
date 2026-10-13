//! Concrete strength from the team's lab data (`data/mix_tests.csv`).
//!
//! The file lists cylinder breaks. Each row has a density, a measured split
//! tensile strength, optionally a measured flexural strength (`flexural_psi`)
//! and/or compressive strength (`compressive_psi`), and `fraction_of_final`:
//! the share of final (design) strength the specimen had reached when broken.
//! The team's 7-day breaks are at 60% of final, so every final strength is
//! `measured / fraction`.
//!
//! From the final split tensile strengths we fit, by least squares,
//! `f_sp_final(rho) = slope * rho + intercept`. Each other property the checks
//! need (all psi, before the factor of safety) comes from its own measured
//! least-squares line when the CSV has that property at >= 2 distinct
//! densities, else it is derived from the split line:
//! * flexural tension (modulus of rupture), used for bending tension:
//!   derived `f_r = flex_over_split * f_sp`. Default ratio 1.0, which is
//!   conservative (ACI 318 relations give f_r about 7.5/6.7 = 1.12 f_sp).
//! * compressive strength, used for compression, shear and punching:
//!   derived from ACI 318 `f_ct = 6.7 sqrt(f'c)` (normal weight, so lambda is
//!   not double-counted later), `f'c = (f_sp / 6.7)^2`.
//!
//! Rules (fail, never fabricate):
//! * fewer than 2 split rows, or all at one density => error (no slope).
//! * a fitted strength <= 0 => that check fails (capacity <= 0 => D/C = inf).
//! * densities outside the tested range are EXTRAPOLATION: the fit is reported
//!   with its range, and the search clamps density to
//!   `[min tested - extrapolate_pcf, max tested + extrapolate_pcf]` (split range).

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
    /// Measured flexural strength at test age, psi (blank cell => None).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub flexural_psi: Option<f64>,
    /// Measured compressive strength at test age, psi (blank cell => None).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub compressive_psi: Option<f64>,
}

impl MixPoint {
    /// Final flexural strength, psi, when measured.
    pub fn flexural_final_psi(&self) -> Option<f64> {
        self.flexural_psi.map(|v| v / self.fraction_of_final)
    }
    /// Final compressive strength, psi, when measured.
    pub fn compressive_final_psi(&self) -> Option<f64> {
        self.compressive_psi.map(|v| v / self.fraction_of_final)
    }
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
    /// Mean FINAL flexural strength of the rows that measured it, psi.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub flexural_mean_psi: Option<f64>,
    /// Mean FINAL compressive strength of the rows that measured it, psi.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub compressive_mean_psi: Option<f64>,
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

/// Least-squares line of one measured FINAL strength vs density.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct PropertyFit {
    /// Rows that measured this property.
    pub n: usize,
    pub slope_psi_per_pcf: f64,
    pub intercept_psi: f64,
    /// Root-mean-square residual of the fit, psi.
    pub rms_residual_psi: f64,
    pub density_min_pcf: f64,
    pub density_max_pcf: f64,
}

impl PropertyFit {
    /// Fitted final strength at density `rho`, psi.
    pub fn at(&self, rho: f64) -> f64 {
        self.slope_psi_per_pcf * rho + self.intercept_psi
    }

    /// Least squares through `(density, final psi)`. Err with fewer than 2 rows
    /// or all at one density.
    fn fit(xy: &[(f64, f64)]) -> Result<Self> {
        ensure!(xy.len() >= 2, "need at least 2 rows to fit strength vs density");
        let n = xy.len() as f64;
        let mx = xy.iter().map(|p| p.0).sum::<f64>() / n;
        let my = xy.iter().map(|p| p.1).sum::<f64>() / n;
        let sxx: f64 = xy.iter().map(|p| (p.0 - mx).powi(2)).sum();
        ensure!(sxx > 1e-9, "all rows are at one density: cannot fit a slope");
        let sxy: f64 = xy.iter().map(|p| (p.0 - mx) * (p.1 - my)).sum();
        let slope = sxy / sxx;
        let icpt = my - slope * mx;
        let rms = (xy.iter().map(|p| (p.1 - (slope * p.0 + icpt)).powi(2)).sum::<f64>() / n).sqrt();
        Ok(PropertyFit {
            n: xy.len(),
            slope_psi_per_pcf: slope,
            intercept_psi: icpt,
            rms_residual_psi: rms,
            density_min_pcf: xy.iter().map(|p| p.0).fold(f64::INFINITY, f64::min),
            density_max_pcf: xy.iter().map(|p| p.0).fold(f64::NEG_INFINITY, f64::max),
        })
    }
}

/// A strength property with an optional measured column.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Property {
    Flexural,
    Compressive,
}

impl Property {
    pub const ALL: [Property; 2] = [Property::Flexural, Property::Compressive];
    /// CSV column name.
    pub fn column(self) -> &'static str {
        match self {
            Property::Flexural => "flexural_psi",
            Property::Compressive => "compressive_psi",
        }
    }
    /// Human label.
    pub fn label(self) -> &'static str {
        match self {
            Property::Flexural => "flexural",
            Property::Compressive => "f'c",
        }
    }
    fn final_psi(self, p: &MixPoint) -> Option<f64> {
        match self {
            Property::Flexural => p.flexural_final_psi(),
            Property::Compressive => p.compressive_final_psi(),
        }
    }
}

/// Linear fit of FINAL split tensile strength vs density, optional measured
/// flexural / compressive fits, and the strengths the checks use.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct StrengthFit {
    pub points: Vec<MixPoint>,
    pub split_slope_psi_per_pcf: f64,
    pub split_intercept_psi: f64,
    /// Root-mean-square residual of the fit, psi.
    pub rms_residual_psi: f64,
    pub density_min_pcf: f64,
    pub density_max_pcf: f64,
    /// Modulus of rupture / split tensile (used only when flexural is derived).
    pub flex_over_split: f64,
    /// Measured flexural line; None => flexural is derived from split.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub flexural_fit: Option<PropertyFit>,
    /// Measured compressive line; None => f'c is derived from split.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub compressive_fit: Option<PropertyFit>,
}

impl StrengthFit {
    /// Final split tensile strength at density `rho`, psi.
    pub fn split_psi(&self, rho: f64) -> f64 {
        self.split_slope_psi_per_pcf * rho + self.split_intercept_psi
    }
    /// Flexural tensile strength (bending tension capacity before FS), psi:
    /// the measured line when there is one, else `flex_over_split * split`.
    pub fn flexural_psi(&self, rho: f64) -> f64 {
        match &self.flexural_fit {
            Some(f) => f.at(rho),
            None => self.flex_over_split * self.split_psi(rho),
        }
    }
    /// Compressive strength f'c, psi: the measured line when there is one,
    /// else from ACI f_ct = 6.7 sqrt(f'c). <= 0 if the governing strength is
    /// <= 0 (the check then fails).
    pub fn fc_psi(&self, rho: f64) -> f64 {
        if let Some(f) = &self.compressive_fit {
            return f.at(rho);
        }
        let s = self.split_psi(rho);
        if s <= 0.0 { s } else { (s / 6.7).powi(2) }
    }

    /// Measured fit of `prop`, if any.
    pub fn measured(&self, prop: Property) -> Option<&PropertyFit> {
        match prop {
            Property::Flexural => self.flexural_fit.as_ref(),
            Property::Compressive => self.compressive_fit.as_ref(),
        }
    }

    /// "measured" or "derived": where `prop` comes from.
    pub fn source(&self, prop: Property) -> &'static str {
        if self.measured(prop).is_some() { "measured" } else { "derived" }
    }

    /// One line per property saying where it comes from.
    pub fn source_lines(&self) -> Vec<String> {
        Property::ALL
            .iter()
            .map(|&prop| {
                let rows = self.points.iter().filter(|p| prop.final_psi(p).is_some()).count();
                match self.measured(prop) {
                    Some(f) => format!(
                        "{}: measured, final = {:.2}*rho {:+.1} psi from {} tests ({:.1}-{:.1} pcf), rms {:.1} psi",
                        prop.label(),
                        f.slope_psi_per_pcf,
                        f.intercept_psi,
                        f.n,
                        f.density_min_pcf,
                        f.density_max_pcf,
                        f.rms_residual_psi
                    ),
                    None => {
                        let how = match prop {
                            Property::Flexural => format!("{:.2} x split", self.flex_over_split),
                            Property::Compressive => "(split / 6.7)^2".to_string(),
                        };
                        let why = if rows == 0 {
                            format!("no {} data", prop.column())
                        } else {
                            format!("{rows} {} row(s) at one density: cannot fit", prop.column())
                        };
                        format!("{}: derived = {how} ({why})", prop.label())
                    }
                }
            })
            .collect()
    }

    /// Weak-fit warnings: one per fitted property (split, and each measured
    /// one) with fewer than 5 points or rms scatter above 15% of the mean.
    pub fn weak_fit_warnings(&self) -> Vec<String> {
        let split: Vec<f64> = self.points.iter().map(|p| p.final_psi).collect();
        let mut fits = vec![("strength", split, self.rms_residual_psi)];
        for prop in Property::ALL {
            if let Some(f) = self.measured(prop) {
                let ys = self.points.iter().filter_map(|p| prop.final_psi(p)).collect();
                fits.push((prop.label(), ys, f.rms_residual_psi));
            }
        }
        let mut out = vec![];
        for (name, ys, rms) in fits {
            let n = ys.len();
            let mean = ys.iter().sum::<f64>() / n as f64;
            let mut why = vec![];
            if n < 5 {
                why.push(format!("only {n} lab points (want >= 5)"));
            }
            if rms > 0.15 * mean {
                why.push(format!("rms {rms:.0} psi is > 15% of mean {mean:.0} psi"));
            }
            if !why.is_empty() {
                out.push(format!("WARNING {name} fit is weak: {}; add lab tests", why.join(", ")));
            }
        }
        out
    }

    /// Tested mixes (rows grouped by [`MixPoint::group`]), ordered by mean
    /// density then name. Design strength of a mix = mean final split strength;
    /// flexural / compressive means are over the rows that measured them.
    pub fn tested_mixes(&self) -> Vec<TestedMix> {
        let mut names: Vec<&str> = self.points.iter().map(|p| p.group.as_str()).collect();
        names.sort();
        names.dedup();
        let mean = |v: Vec<f64>| (!v.is_empty()).then(|| v.iter().sum::<f64>() / v.len() as f64);
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
                    flexural_mean_psi: mean(rows.iter().filter_map(|p| p.flexural_final_psi()).collect()),
                    compressive_mean_psi: mean(
                        rows.iter().filter_map(|p| p.compressive_final_psi()).collect(),
                    ),
                }
            })
            .collect();
        out.sort_by(|a, b| a.density_pcf.total_cmp(&b.density_pcf).then_with(|| a.name.cmp(&b.name)));
        out
    }

    /// Strength model for one tested mix: flat lines at the mix's mean final
    /// split strength and, where the mix measured them, its mean flexural and
    /// compressive strengths (others derived from its split mean as in fit
    /// mode), with the mix's own points and density.
    pub fn for_mix(&self, m: &TestedMix) -> StrengthFit {
        let points: Vec<MixPoint> = self.points.iter().filter(|p| p.group == m.name).cloned().collect();
        let flat = |prop: Property, v: Option<f64>| {
            v.map(|v| PropertyFit {
                n: points.iter().filter(|p| prop.final_psi(p).is_some()).count(),
                slope_psi_per_pcf: 0.0,
                intercept_psi: v,
                rms_residual_psi: 0.0,
                density_min_pcf: m.density_pcf,
                density_max_pcf: m.density_pcf,
            })
        };
        StrengthFit {
            flexural_fit: flat(Property::Flexural, m.flexural_mean_psi),
            compressive_fit: flat(Property::Compressive, m.compressive_mean_psi),
            points,
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

    /// Least-squares fit through the given points: split always (error if it
    /// cannot be fitted), flexural / compressive where >= 2 distinct densities
    /// measured them.
    pub fn fit(points: Vec<MixPoint>, flex_over_split: f64) -> Result<Self> {
        ensure!(points.len() >= 2, "need at least 2 mix test rows to fit strength vs density");
        ensure!(flex_over_split > 0.0, "flex_over_split must be > 0");
        let split: Vec<(f64, f64)> = points.iter().map(|p| (p.density_pcf, p.final_psi)).collect();
        let s = PropertyFit::fit(&split).context("split tensile")?;
        let measured = |prop: Property| {
            let xy: Vec<(f64, f64)> =
                points.iter().filter_map(|p| prop.final_psi(p).map(|y| (p.density_pcf, y))).collect();
            PropertyFit::fit(&xy).ok()
        };
        Ok(StrengthFit {
            flexural_fit: measured(Property::Flexural),
            compressive_fit: measured(Property::Compressive),
            points,
            split_slope_psi_per_pcf: s.slope_psi_per_pcf,
            split_intercept_psi: s.intercept_psi,
            rms_residual_psi: s.rms_residual_psi,
            density_min_pcf: s.density_min_pcf,
            density_max_pcf: s.density_max_pcf,
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
/// columns `mix,cured_days,density_pcf,split_tensile_psi,fraction_of_final,note`,
/// optional `flexural_psi` / `compressive_psi` columns (cells may be blank),
/// and an optional `mix_id` column that overrides the tested-mix grouping.
/// Columns are found by header name, so order may change and extra columns
/// are ignored. Quoted fields and thousands separators ("7,191") are handled.
pub fn parse_csv(text: &str) -> Result<Vec<MixPoint>> {
    let mut lines = text.lines().map(str::trim).filter(|l| !l.is_empty() && !l.starts_with('#'));
    let header = split_csv(lines.next().context("empty mix file")?);
    let find = |name: &str| header.iter().position(|h| h.trim().eq_ignore_ascii_case(name));
    let col = |name: &str| find(name).with_context(|| format!("missing column '{name}'"));
    let (ci_mix, ci_rho, ci_psi, ci_frac) =
        (col("mix")?, col("density_pcf")?, col("split_tensile_psi")?, col("fraction_of_final")?);
    let ci_id = find("mix_id");
    let (ci_flex, ci_fc) = (find(Property::Flexural.column()), find(Property::Compressive.column()));
    let mut out = vec![];
    for (ln, line) in lines.enumerate() {
        let f = split_csv(line);
        let cell = |i: usize| f.get(i).map(|s| s.replace(',', "")).unwrap_or_default();
        let num = |i: usize, what: &str| -> Result<f64> {
            let raw = cell(i);
            raw.trim().parse::<f64>().with_context(|| format!("row {}: bad {what} '{raw}'", ln + 1))
        };
        // Optional strength: blank or missing cell => None, else must be > 0.
        let opt = |ci: Option<usize>, what: &str| -> Result<Option<f64>> {
            let Some(i) = ci.filter(|&i| !cell(i).trim().is_empty()) else { return Ok(None) };
            let v = num(i, what)?;
            ensure!(v > 0.0, "row {}: {what} must be > 0 (or blank), got {v}", ln + 1);
            Ok(Some(v))
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
            flexural_psi: opt(ci_flex, "flexural_psi")?,
            compressive_psi: opt(ci_fc, "compressive_psi")?,
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
        // No measured flexural / compressive columns: both derived.
        assert_eq!(fit.source(Property::Flexural), "derived");
        assert_eq!(fit.source(Property::Compressive), "derived");
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

    const HDR: &str = "mix,density_pcf,split_tensile_psi,fraction_of_final,flexural_psi,compressive_psi\n";

    #[test]
    fn optional_columns_parse_blank_and_present() {
        let t = format!("{HDR}A 1,50,200,0.5,150,\nA 2,52,210,1,,\"2,400\"\nB,60,300,1\n");
        let p = parse_csv(&t).unwrap();
        assert_eq!(p[0].flexural_psi, Some(150.0));
        assert_eq!(p[0].flexural_final_psi(), Some(300.0));
        assert_eq!(p[0].compressive_psi, None);
        assert_eq!(p[1].flexural_psi, None);
        assert_eq!(p[1].compressive_final_psi(), Some(2400.0));
        // Short row: missing trailing cells are blank.
        assert_eq!((p[2].flexural_psi, p[2].compressive_psi), (None, None));
        // Present but bad or non-positive values are errors, not blanks.
        assert!(parse_csv(&format!("{HDR}A,50,200,1,abc,\n")).is_err());
        assert!(parse_csv(&format!("{HDR}A,50,200,1,0,\n")).is_err());
        assert!(parse_csv(&format!("{HDR}A,50,200,1,,-5\n")).is_err());
        // Without the columns nothing is serialized for them.
        let q = parse_csv("mix,density_pcf,split_tensile_psi,fraction_of_final\nA,50,200,1\n").unwrap();
        assert!(!serde_json::to_string(&q[0]).unwrap().contains("flexural"));
    }

    #[test]
    fn measured_fit_used_when_present() {
        // Flexural at two densities (one row scaled by fraction), f'c at three.
        let t =
            format!("{HDR}A,50,200,1,120,1500\nB,55,250,0.5,140,900\nC,60,300,1,,2500\nD,60,310,1,400,\n");
        let fit = StrengthFit::parse(&t, 1.0).unwrap();
        let fl = fit.flexural_fit.as_ref().unwrap();
        // Final flexural: (50,120), (55,280), (60,400) -> least squares.
        assert_eq!(fl.n, 3);
        assert!((fl.slope_psi_per_pcf - 28.0).abs() < 1e-9, "{}", fl.slope_psi_per_pcf);
        assert!((fit.flexural_psi(55.0) - 800.0 / 3.0).abs() < 1e-9);
        let fc = fit.compressive_fit.as_ref().unwrap();
        assert_eq!(fc.n, 3); // (50,1500), (55,1800), (60,2500)
        assert!((fc.slope_psi_per_pcf - 100.0).abs() < 1e-9);
        assert!((fit.fc_psi(55.0) - 1933.0 - 1.0 / 3.0).abs() < 1e-9);
        assert_eq!(fit.source(Property::Flexural), "measured");
        assert_eq!(fit.source(Property::Compressive), "measured");
        // Split fit is unaffected by the extra columns.
        let base = StrengthFit::parse(
            "mix,density_pcf,split_tensile_psi,fraction_of_final\nA,50,200,1\nB,55,250,0.5\nC,60,300,1\nD,60,310,1\n",
            1.0,
        )
        .unwrap();
        assert_eq!(base.split_slope_psi_per_pcf, fit.split_slope_psi_per_pcf);
        assert_eq!(base.split_intercept_psi, fit.split_intercept_psi);
        assert!(fit.source_lines()[0].starts_with("flexural: measured"));
        // Each measured property is judged on its own (3 points < 5).
        let w = fit.weak_fit_warnings();
        assert_eq!(w.len(), 3, "{w:?}");
        assert!(
            w[1].starts_with("WARNING flexural fit is weak") && w[2].starts_with("WARNING f'c fit is weak")
        );
    }

    #[test]
    fn falls_back_to_derived_without_two_densities() {
        // Flexural only at one density, compressive at none: both derived.
        let t = format!("{HDR}A,50,200,1,300,\nB,50,210,1,320,\nC,60,300,1,,\n");
        let fit = StrengthFit::parse(&t, 1.1).unwrap();
        assert!(fit.flexural_fit.is_none() && fit.compressive_fit.is_none());
        let s = fit.split_psi(55.0);
        assert!((fit.flexural_psi(55.0) - 1.1 * s).abs() < 1e-9);
        assert!((fit.fc_psi(55.0) - (s / 6.7).powi(2)).abs() < 1e-9);
        let lines = fit.source_lines();
        assert!(lines[0].contains("derived") && lines[0].contains("2 flexural_psi row(s) at one density"));
        assert!(lines[1].contains("no compressive_psi data"));
        assert!(!serde_json::to_string(&fit).unwrap().contains("flexural_fit"));
    }

    #[test]
    fn tested_mixes_use_per_mix_measured_means() {
        // M1 measured flexural on one of its rows and f'c on both; M2 nothing.
        let t = format!("{HDR}M1 A,50,200,0.5,100,1000\nM1 B,52,220,1,,1600\nM2 A,60,300,1,,\n");
        let fit = StrengthFit::parse(&t, 1.0).unwrap();
        let m = fit.tested_mixes();
        assert_eq!(m[0].flexural_mean_psi, Some(200.0));
        assert_eq!(m[0].compressive_mean_psi, Some(1800.0));
        assert_eq!((m[1].flexural_mean_psi, m[1].compressive_mean_psi), (None, None));
        let f1 = fit.for_mix(&m[0]);
        assert_eq!(f1.flexural_psi(10.0), 200.0);
        assert_eq!(f1.fc_psi(99.0), 1800.0);
        assert_eq!(f1.flexural_fit.as_ref().unwrap().n, 1);
        // M2 has no measurements: derived from its own split mean even though
        // the whole-file f'c fit exists.
        assert!(fit.compressive_fit.is_some());
        let f2 = fit.for_mix(&m[1]);
        assert_eq!(f2.source(Property::Compressive), "derived");
        assert!((f2.fc_psi(60.0) - (300.0f64 / 6.7).powi(2)).abs() < 1e-9);
        assert_eq!(f2.flexural_psi(60.0), 300.0);
    }

    #[test]
    fn deterministic() {
        let t = format!("{HDR}M1 A,50,200,0.5,100,1000\nM1 B,52,220,1,,1600\nM2 A,60,300,1,350,2900\n");
        let a = serde_json::to_string(&StrengthFit::parse(&t, 1.0).unwrap()).unwrap();
        let b = serde_json::to_string(&StrengthFit::parse(&t, 1.0).unwrap()).unwrap();
        assert_eq!(a, b);
        // Parses back (serve / result.json), measured fits included.
        let back: StrengthFit = serde_json::from_str(&a).unwrap();
        assert!(back.flexural_fit.is_some() && back.compressive_fit.is_some());
    }
}
