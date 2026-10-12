use anyhow::Result;
use clap::{Parser, Subcommand};
use jaguar::{hull::Hull, mix::StrengthFit, optimize, params::Params, report, serve};
use std::path::PathBuf;

/// JAGUAR: find the lightest concrete canoe wall that survives the crew.
#[derive(Parser)]
#[command(version)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the optimization described by a canoe.toml.
    Run {
        #[arg(default_value = "canoe.toml")]
        params: PathBuf,
        /// Where to write the result.
        #[arg(short, long, default_value = "result.json")]
        out: PathBuf,
    },
    /// Load and validate a canoe.toml and its hull without running.
    Check {
        #[arg(default_value = "canoe.toml")]
        params: PathBuf,
    },
    /// Convert a Godzilla/Michlet off1.mlt + out.mlt into a jaguar-hull/1 JSON.
    ImportMichlet {
        off1: PathBuf,
        out_mlt: PathBuf,
        #[arg(short, long, default_value = "hull.json")]
        out: PathBuf,
    },
    /// Generate a jaguar-hull/1 JSON from Michlet Series 7 shape factors (shape.toml).
    GenHull {
        shape: PathBuf,
        #[arg(short, long, default_value = "hull.json")]
        out: PathBuf,
        #[arg(long, default_value_t = 41)]
        stations: usize,
        #[arg(long, default_value_t = 21)]
        waterlines: usize,
        /// Hull name (default: file stem of shape.toml).
        #[arg(long)]
        name: Option<String>,
    },
    /// Print the lab mix data points and the strength fit (sanity-check lab data).
    Mix {
        #[arg(default_value = "data/mix_tests.csv")]
        data: PathBuf,
        /// Modulus of rupture / split tensile used for derived strengths.
        #[arg(long, default_value_t = 1.0)]
        flex_over_split: f64,
    },
    /// Web UI at http://127.0.0.1:PORT: view the canoe, edit params, run and watch (optional).
    Serve {
        #[arg(default_value = "canoe.toml")]
        params: PathBuf,
        #[arg(short, long, default_value_t = 8000)]
        port: u16,
    },
}

/// Warn when the strength fit rests on too little or too scattered data.
fn weak_fit_warning(fit: &StrengthFit) -> Option<String> {
    let n = fit.points.len();
    let mean = fit.points.iter().map(|p| p.final_psi).sum::<f64>() / n as f64;
    let mut why = vec![];
    if n < 5 {
        why.push(format!("only {n} lab points (want >= 5)"));
    }
    if fit.rms_residual_psi > 0.15 * mean {
        why.push(format!("rms {:.0} psi is > 15% of mean {:.0} psi", fit.rms_residual_psi, mean));
    }
    (!why.is_empty()).then(|| format!("WARNING strength fit is weak: {}; add lab tests", why.join(", ")))
}

fn main() -> Result<()> {
    match Cli::parse().cmd {
        Cmd::Run { params, out } => {
            let p = Params::load(&params)?;
            let result = jaguar::run(&p, &|_| {})?;
            report::print_summary(&result);
            report::write_json(&result, &out)?;
            // Exit 2 = ran fine but no verified design: scripts can tell.
            if !result.winner.as_ref().is_some_and(|w| w.verified) {
                std::process::exit(2);
            }
        }
        Cmd::Check { params } => {
            let p = Params::load(&params)?;
            let fit = optimize::strength(&p)?;
            let rhos: Vec<f64> = optimize::density_axis(&p, &fit)?.iter().map(|a| a.0).collect();
            match &p.hull.file {
                Some(f) => {
                    let hull = Hull::load(f)?;
                    println!(
                        "ok: hull '{}' L={:.1} in, {} stations x {} waterlines; {} load case(s)",
                        hull.name,
                        hull.length,
                        hull.x.len(),
                        hull.z.len(),
                        p.load_cases.len()
                    );
                }
                None => println!("ok: shape search; {} load case(s)", p.load_cases.len()),
            }
            println!(
                "mix fit: final split tensile = {:.2} * rho {:+.1} psi from {} tests ({:.1}-{:.1} pcf, rms {:.1} psi); searching {} densities {:.0}-{:.0} pcf",
                fit.split_slope_psi_per_pcf,
                fit.split_intercept_psi,
                fit.points.len(),
                fit.density_min_pcf,
                fit.density_max_pcf,
                fit.rms_residual_psi,
                rhos.len(),
                rhos[0],
                rhos[rhos.len() - 1]
            );
            if let Some(w) = weak_fit_warning(&fit) {
                println!("{w}");
            }
        }
        Cmd::ImportMichlet { off1, out_mlt, out } => {
            let hull = Hull::from_michlet(&off1, &out_mlt)?;
            hull.save(&out)?;
            println!("wrote {} ({} x {})", out.display(), hull.x.len(), hull.z.len());
        }
        Cmd::GenHull { shape, out, stations, waterlines, name } => {
            let text = std::fs::read_to_string(&shape)?;
            let sp: jaguar::hullgen::ShapeParams = toml::from_str(&text)?;
            let name = name.unwrap_or_else(|| shape.file_stem().unwrap_or_default().to_string_lossy().into());
            let hull = jaguar::hullgen::generate(&sp, stations, waterlines, &name)?;
            hull.save(&out)?;
            println!("wrote {} ({} x {})", out.display(), hull.x.len(), hull.z.len());
        }
        Cmd::Mix { data, flex_over_split } => {
            let fit = StrengthFit::load(&data, flex_over_split)?;
            println!(
                "{:<16} {:>8} {:>10} {:>9} {:>10} {:>10}",
                "mix", "rho pcf", "measured", "fraction", "final psi", "fit psi"
            );
            for pt in &fit.points {
                println!(
                    "{:<16} {:>8.1} {:>10.0} {:>9.2} {:>10.0} {:>10.0}",
                    pt.mix,
                    pt.density_pcf,
                    pt.measured_psi,
                    pt.fraction_of_final,
                    pt.final_psi,
                    fit.split_psi(pt.density_pcf)
                );
            }
            println!("{}", report::mix_fit_line(&fit));
            println!(
                "tested mixes (strength_model = \"tested_mixes\"; design = mean final split):\n{:<16} {:>3} {:>8} {:>10} {:>10} {:>10} {:>8}",
                "mix", "n", "rho pcf", "split mean", "split min", "flexural", "f'c"
            );
            for m in fit.tested_mixes() {
                let f = fit.for_mix(&m);
                println!(
                    "{:<16} {:>3} {:>8.1} {:>10.0} {:>10.0} {:>10.0} {:>8.0}",
                    m.name,
                    m.n,
                    m.density_pcf,
                    m.split_mean_psi,
                    m.split_min_psi,
                    f.flexural_psi(m.density_pcf),
                    f.fc_psi(m.density_pcf)
                );
            }
            for rho in [fit.density_min_pcf, fit.density_max_pcf] {
                println!(
                    "  at {rho:.1} pcf: split {:.0} psi, flexural {:.0} psi, f'c {:.0} psi",
                    fit.split_psi(rho),
                    fit.flexural_psi(rho),
                    fit.fc_psi(rho)
                );
            }
            if let Some(w) = weak_fit_warning(&fit) {
                println!("{w}");
            }
        }
        Cmd::Serve { params, port } => serve::serve(&params, port)?,
    }
    Ok(())
}
