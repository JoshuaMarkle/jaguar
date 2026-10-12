use anyhow::Result;
use clap::{Parser, Subcommand};
use jaguar::{hull::Hull, optimize, params::Params, report, serve};
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
    /// Web UI at http://127.0.0.1:PORT: view the canoe, edit params, run and watch (optional).
    Serve {
        #[arg(default_value = "canoe.toml")]
        params: PathBuf,
        #[arg(short, long, default_value_t = 8000)]
        port: u16,
    },
}

fn main() -> Result<()> {
    match Cli::parse().cmd {
        Cmd::Run { params, out } => {
            let p = Params::load(&params)?;
            let result = optimize::run_params(&p, &|_| {})?;
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
            let rhos = optimize::densities(&p, &fit)?;
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
        Cmd::Serve { params, port } => serve::serve(&params, port)?,
    }
    Ok(())
}
