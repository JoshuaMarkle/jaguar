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
            let hull = Hull::load(&p.hull.file)?;
            let result = optimize::run(&p, &hull)?;
            report::print_summary(&result);
            report::write_json(&result, &out)?;
            // Exit 2 = ran fine but no verified design: scripts can tell.
            if !result.winner.as_ref().is_some_and(|w| w.verified) {
                std::process::exit(2);
            }
        }
        Cmd::Check { params } => {
            let p = Params::load(&params)?;
            let hull = Hull::load(&p.hull.file)?;
            println!(
                "ok: hull '{}' L={:.1} in, {} stations x {} waterlines; {} load case(s)",
                hull.name,
                hull.length,
                hull.x.len(),
                hull.z.len(),
                p.load_cases.len()
            );
        }
        Cmd::ImportMichlet { off1, out_mlt, out } => {
            let hull = Hull::from_michlet(&off1, &out_mlt)?;
            hull.save(&out)?;
            println!("wrote {} ({} x {})", out.display(), hull.x.len(), hull.z.len());
        }
        Cmd::Serve { params, port } => serve::serve(&params, port)?,
    }
    Ok(())
}
