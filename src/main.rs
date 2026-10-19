use anyhow::Result;
use clap::{Parser, Subcommand};
use jaguar::mix::{Property, StrengthFit};
use jaguar::{hull::Hull, optimize, params::Params, report, serve};
use std::path::PathBuf;

/// JAGUAR: find the lightest concrete canoe wall that survives the crew.
/// With no command (a double-click): open the web UI on the canoe folder Documents/JAGUAR.
#[derive(Parser)]
#[command(version)]
struct Cli {
    #[command(subcommand)]
    cmd: Option<Cmd>,
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
    /// Read a canoe made in CAD (STEP, STL or OBJ; solid wall or surface) into a jaguar-hull/1 JSON,
    /// measuring its wall and depth; with --canoe, also write a canoe.toml that analyses it as drawn.
    ImportMesh {
        mesh: PathBuf,
        #[arg(short, long)]
        out: Option<PathBuf>,
        /// Units of the model: in | mm | cm | m | ft (default: a STEP file's own unit, else detected from the length)
        #[arg(long)]
        units: Option<String>,
        /// Length / up axis: x | y | z (default: longest / shortest side)
        #[arg(long)]
        length_axis: Option<String>,
        #[arg(long)]
        up_axis: Option<String>,
        /// The keel is at the high end of the up axis
        #[arg(long)]
        flip_up: bool,
        /// The bow is at the high end of the length axis
        #[arg(long)]
        bow_at_max: bool,
        #[arg(long, default_value_t = 81)]
        stations: usize,
        #[arg(long, default_value_t = 41)]
        waterlines: usize,
        /// canoe.toml to take everything else from (crew, mixes, gates, [race]); writes --analysis
        #[arg(long)]
        canoe: Option<PathBuf>,
        /// Where to write the analysis canoe.toml (default: <mesh stem>_analysis.toml)
        #[arg(long)]
        analysis: Option<PathBuf>,
        /// Wall thickness, in (default: measured; needed for a surface mesh to analyse one wall)
        #[arg(long)]
        thickness: Option<f64>,
        /// Mix density of the drawn design, pcf (default: the canoe.toml's range, lightest passing)
        #[arg(long)]
        density: Option<f64>,
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
    /// Export the canoe as STEP or STL. Input: result.json (winner hull, wall, depth) or a
    /// jaguar-hull/1 JSON (then give --depth, and --thickness for a solid wall).
    Export {
        input: PathBuf,
        #[arg(short, long, default_value = "canoe.step")]
        out: PathBuf,
        /// step | stl (default: from the -o extension)
        #[arg(long)]
        format: Option<String>,
        /// Wall thickness, in (overrides the result; omit on a hull for the mold surface only)
        #[arg(long)]
        thickness: Option<f64>,
        /// Mold depth, in (overrides the result; default for a hull: its draft_design)
        #[arg(long)]
        depth: Option<f64>,
        /// Topside flare, deg (overrides the result)
        #[arg(long)]
        flare: Option<f64>,
        /// Line-arc-line gunwale radius, in (overrides the result; see hull.gunwale_radius_in)
        #[arg(long)]
        gunwale_radius: Option<f64>,
        #[arg(long, default_value_t = 161)]
        stations: usize,
        /// Points around the girth, sheer to sheer (odd)
        #[arg(long, default_value_t = 41)]
        girth: usize,
        /// Leave out the air pocket bulkheads (result.json with [flotation])
        #[arg(long)]
        no_pockets: bool,
        /// Unshelled: the outer hull filled in to the mold depth, one solid (no wall, no pockets)
        #[arg(long)]
        filled: bool,
    },
    /// Hardboard for the winner's male foam mold, packed onto sheets, one DXF per sheet for the
    /// water jet: tracing templates for the foam layers, or boards between them (src/manufacture.rs).
    Mold {
        #[arg(default_value = "result.json")]
        result: PathBuf,
        /// Folder for the DXF files and mold.json (created if missing)
        #[arg(short, long, default_value = "mold")]
        out: PathBuf,
        /// Hardboard between the foam layers, part of the mold (default: tracing templates)
        #[arg(long)]
        in_mold: bool,
        /// Layers standing upright across the canoe (ribs) instead of laid flat
        #[arg(long)]
        vertical: bool,
        /// blocks (joints line up in every layer: the mold comes apart in blocks) | continuous
        /// (fewest pieces per layer) | tight (fewest sheets) | fewest-shapes (half templates,
        /// flipped and shared)
        #[arg(long, default_value = "blocks")]
        packing: String,
        /// Foam sheet thicknesses on hand, in, comma separated (e.g. 1.5,2): layers mix them
        #[arg(long, value_delimiter = ',', default_value = "2")]
        foam: Vec<f64>,
        /// Foam sheet sizes on hand, LxW in, comma separated (e.g. 96x48,72x36)
        #[arg(long, value_delimiter = ',', default_value = "96x48")]
        foam_sheet: Vec<String>,
        /// Hardboard thickness, in
        #[arg(long, default_value_t = 0.125)]
        board: f64,
        /// Hardboard sheet sizes on hand, LxW in, comma separated (e.g. 96x48,48x48)
        #[arg(long, value_delimiter = ',', default_value = "96x48")]
        sheet: Vec<String>,
        /// Least space between pieces and from the sheet edge, in
        #[arg(long, default_value_t = 0.25)]
        gap: f64,
        /// Cut smaller pieces out of the middle of bigger ones (templates become rings)
        #[arg(long)]
        nest_inside: bool,
        /// Least ring width left around pieces cut out of a template, in (with --nest-inside)
        #[arg(long, default_value_t = 1.0)]
        ring: f64,
        /// Leave out the alignment holes
        #[arg(long)]
        no_holes: bool,
        /// Alignment hole size, in
        #[arg(long, default_value_t = 0.25)]
        hole: f64,
        /// Water-jet cut width to allow for, in (0: the water-jet software compensates)
        #[arg(long, default_value_t = 0.0)]
        kerf: f64,
        /// Also write reference lines (names, centreline, ticks, sanding line, sheet edge) in the
        /// DXF; off: cut lines only (OMAX and the like cut every line they import)
        #[arg(long)]
        dxf_marks: bool,
        /// Double-symmetric canoe: cut templates for the bow half only, each also traces the
        /// matching stern piece turned end for end (flat tracing templates)
        #[arg(long)]
        half_canoe: bool,
    },
    /// Race the winner of a result.json over the `[race]` courses: time, straight vs turning,
    /// and recommended drag-objective weightings (src/race.rs).
    Race {
        #[arg(default_value = "result.json")]
        result: PathBuf,
        /// Take `[race]` from this canoe.toml instead of the result's params.
        #[arg(long)]
        params: Option<PathBuf>,
        #[arg(short, long, default_value = "race.json")]
        out: PathBuf,
        /// "EVENT=SECONDS": fit race.paddler_power_w so EVENT takes SECONDS, then race with it.
        #[arg(long)]
        measured: Option<String>,
    },
    /// Web UI at http://127.0.0.1:PORT: view the canoe, edit params, run and watch (optional).
    Serve {
        #[arg(default_value = "canoe.toml")]
        params: PathBuf,
        #[arg(short, long, default_value_t = 8000)]
        port: u16,
        /// Autosaved UI settings file (default $XDG_CONFIG_HOME/jaguar/prefs.toml,
        /// else ~/.config/jaguar/prefs.toml).
        #[arg(long)]
        prefs: Option<PathBuf>,
    },
}

fn main() -> Result<()> {
    let Some(cmd) = Cli::parse().cmd else {
        if let Err(e) = jaguar::launch::launch() {
            jaguar::launch::alert(&format!("{e:#}"));
            std::process::exit(1);
        }
        return Ok(());
    };
    match cmd {
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
            for l in fit.source_lines().iter().chain(&fit.weak_fit_warnings()) {
                println!("{l}");
            }
        }
        Cmd::ImportMesh {
            mesh,
            out,
            units,
            length_axis,
            up_axis,
            flip_up,
            bow_at_max,
            stations,
            waterlines,
            canoe,
            analysis,
            thickness,
            density,
        } => {
            use jaguar::meshimport::{self as mi, MeshOptions};
            let bytes = std::fs::read(&mesh)?;
            let ext = mesh.extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
            let (tris, declared, what) = mi::read_cad(&ext, &bytes)?;
            println!("read {what}");
            let given = units.as_deref().map(mi::unit_scale).transpose()?;
            let opts = MeshOptions {
                units: given.or(declared.as_ref().map(|d| d.0)),
                units_from: declared
                    .filter(|_| given.is_none())
                    .map(|d| format!("{} (declared in the file)", d.1)),
                length_axis: length_axis.as_deref().map(mi::axis_index).transpose()?,
                up_axis: up_axis.as_deref().map(mi::axis_index).transpose()?,
                flip_up,
                bow_at_max,
                stations,
                waterlines,
            };
            let stem = mesh.file_stem().unwrap_or_default().to_string_lossy().to_string();
            let (hull, m) = mi::hull_from_mesh(&tris, &stem, &opts)?;
            let out = out.unwrap_or_else(|| mesh.with_file_name(format!("{stem}_hull.json")));
            hull.save(&out)?;
            let f = |v: Option<f64>| v.map_or("-".into(), |x| format!("{x:.3}"));
            println!(
                "read {} triangles, units {}, length axis {}, up axis {}",
                m.triangles, m.units, m.length_axis, m.up_axis
            );
            println!(
                "canoe {:.1} x {:.1} in, gunwale depth {:.2} in (sheer max {:.2}); wall {} in (min {}, max {}; 2V/A {})",
                m.length_in,
                m.beam_in,
                m.depth_in,
                m.sheer_max_in,
                f(m.thickness_in),
                f(m.thickness_min_in),
                f(m.thickness_max_in),
                f(m.thickness_volume_in)
            );
            for n in &m.notes {
                println!("note: {n}");
            }
            println!("wrote {} ({} x {})", out.display(), hull.x.len(), hull.z.len());
            if let Some(c) = canoe {
                let base = std::fs::read_to_string(&c)?;
                let dir = c.parent().unwrap_or(std::path::Path::new(""));
                let (text, notes) = mi::analysis_toml(&base, dir, &out, &m, thickness, density)?;
                let a = analysis.unwrap_or_else(|| mesh.with_file_name(format!("{stem}_analysis.toml")));
                Params::parse(&text, a.parent().unwrap_or(std::path::Path::new("")))?;
                std::fs::write(&a, text)?;
                for n in notes {
                    println!("note: {n}");
                }
                println!(
                    "wrote {} (as drawn). Next: jaguar run {} -o result.json ; jaguar race result.json",
                    a.display(),
                    a.display()
                );
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
            // Final measured flexural / f'c columns only when the CSV has any.
            let extra = fit.points.iter().any(|p| p.flexural_psi.is_some() || p.compressive_psi.is_some());
            let cell = |v: Option<f64>| v.map_or_else(|| "-".to_string(), |x| format!("{x:.0}"));
            println!(
                "{:<16} {:>8} {:>10} {:>9} {:>10} {:>10}{}",
                "mix",
                "rho pcf",
                "measured",
                "fraction",
                "final psi",
                "fit psi",
                if extra { format!(" {:>10} {:>10}", "flex final", "f'c final") } else { String::new() }
            );
            for pt in &fit.points {
                println!(
                    "{:<16} {:>8.1} {:>10.0} {:>9.2} {:>10.0} {:>10.0}{}",
                    pt.mix,
                    pt.density_pcf,
                    pt.measured_psi,
                    pt.fraction_of_final,
                    pt.final_psi,
                    fit.split_psi(pt.density_pcf),
                    if extra {
                        format!(
                            " {:>10} {:>10}",
                            cell(pt.flexural_final_psi()),
                            cell(pt.compressive_final_psi())
                        )
                    } else {
                        String::new()
                    }
                );
            }
            println!("{}", report::mix_fit_line(&fit));
            println!(
                "tested mixes (strength_model = \"tested_mixes\"; design = mean final split; m = mix mean of measured values, else derived):\n{:<16} {:>3} {:>8} {:>10} {:>10} {:>10} {:>8}",
                "mix", "n", "rho pcf", "split mean", "split min", "flexural", "f'c"
            );
            for m in fit.tested_mixes() {
                let f = fit.for_mix(&m);
                // Trailing "m" = mean of this mix's measured values, else derived.
                let tag =
                    |p: Property, v: f64| format!("{v:.0}{}", if f.measured(p).is_some() { "m" } else { "" });
                println!(
                    "{:<16} {:>3} {:>8.1} {:>10.0} {:>10.0} {:>10} {:>8}",
                    m.name,
                    m.n,
                    m.density_pcf,
                    m.split_mean_psi,
                    m.split_min_psi,
                    tag(Property::Flexural, f.flexural_psi(m.density_pcf)),
                    tag(Property::Compressive, f.fc_psi(m.density_pcf))
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
            for l in fit.source_lines().iter().chain(&fit.weak_fit_warnings()) {
                println!("{l}");
            }
        }
        Cmd::Export {
            input,
            out,
            format,
            thickness,
            depth,
            flare,
            gunwale_radius,
            stations,
            girth,
            no_pockets,
            filled,
        } => {
            use jaguar::export;
            let text = std::fs::read_to_string(&input)?;
            let is_result = serde_json::from_str::<serde_json::Value>(&text)
                .map(|v| v["format"] == optimize::RESULT_FORMAT)
                .unwrap_or(false);
            let (hull, mut spec) = if is_result {
                export::from_result(&text, stations, girth)?
            } else {
                let hull = Hull::load(&input)?;
                let d = hull.draft_design;
                (
                    hull,
                    export::ExportSpec {
                        depth_in: d,
                        thickness_in: None,
                        flare_deg: 0.0,
                        gunwale_radius_in: None,
                        stations,
                        girth,
                        pocket_in: None,
                        layers: None,
                        filled: false,
                    },
                )
            };
            if let Some(t) = thickness {
                spec.thickness_in = Some(t);
            }
            if let Some(d) = depth {
                spec.depth_in = d;
            }
            if let Some(f) = flare {
                spec.flare_deg = f;
            }
            if gunwale_radius.is_some() {
                spec.gunwale_radius_in = gunwale_radius;
            }
            if no_pockets || filled {
                spec.pocket_in = None;
            }
            if filled {
                spec.thickness_in = None;
                spec.filled = true;
            }
            let pockets = spec.pocket_in.filter(|&l| l > 0.0 && spec.thickness_in.is_some());
            let fmt = format
                .or_else(|| out.extension().map(|e| e.to_string_lossy().to_lowercase()))
                .unwrap_or_else(|| "step".into());
            let mesh = export::build_mesh(&hull, &spec)?;
            match fmt.as_str() {
                "step" | "stp" => std::fs::write(&out, export::to_step(&mesh, &hull.name))?,
                "stl" => std::fs::write(&out, export::to_stl(&mesh, &hull.name))?,
                f => anyhow::bail!("unknown export format '{f}' (step | stl)"),
            }
            let (lo, hi) = mesh.bbox();
            println!(
                "wrote {} ({}, {} facets, {:.1} x {:.1} x {:.1} in{})",
                out.display(),
                if spec.filled {
                    "filled hull"
                } else if mesh.closed {
                    "solid"
                } else {
                    "mold surface"
                },
                mesh.tris.len(),
                hi[0] - lo[0],
                hi[1] - lo[1],
                hi[2] - lo[2],
                match (spec.filled, mesh.closed) {
                    (true, _) => format!(", volume to the mold depth {:.0} in^3", mesh.volume()),
                    (false, true) => format!(", wall volume {:.0} in^3", mesh.volume()),
                    _ => String::new(),
                }
            );
            if let (Some(l), Some(b)) = (pockets, spec.bulkhead_in()) {
                let one = match spec.layers {
                    Some(n) => format!("one of {n} casting layers"),
                    None => "the whole wall: punching gives no layer count".into(),
                };
                println!(
                    "air pockets {l:.1} in from each tip, sealed by a bulkhead and a top cover {b:.3} in thick ({one}; --no-pockets to leave out)"
                );
            }
        }
        Cmd::Mold {
            result,
            out,
            in_mold,
            vertical,
            packing,
            foam,
            foam_sheet,
            board,
            sheet,
            gap,
            nest_inside,
            ring,
            no_holes,
            hole,
            kerf,
            dxf_marks,
            half_canoe,
        } => {
            use jaguar::manufacture::{self, Boards, MoldSpec, Orientation, Packing};
            let spec = MoldSpec {
                boards: if in_mold { Boards::Mold } else { Boards::Templates },
                orientation: if vertical { Orientation::Vertical } else { Orientation::Flat },
                packing: match packing.as_str() {
                    "blocks" => Packing::Blocks,
                    "continuous" => Packing::Continuous,
                    "tight" => Packing::Tight,
                    "fewest-shapes" | "fewest_shapes" => Packing::FewestShapes,
                    p => {
                        anyhow::bail!("unknown --packing '{p}' (blocks | continuous | tight | fewest-shapes)")
                    }
                },
                foam_in: foam,
                foam_sheets: foam_sheet.iter().map(|s| size_arg(s)).collect::<Result<_>>()?,
                board_in: board,
                sheets: sheet.iter().map(|s| size_arg(s)).collect::<Result<_>>()?,
                gap_in: gap,
                nest_inside,
                ring_in: ring,
                holes: !no_holes,
                hole_in: hole,
                kerf_in: kerf,
                dxf_marks,
                half_canoe,
            };
            let l = manufacture::from_result(&std::fs::read_to_string(&result)?, &spec)?;
            std::fs::create_dir_all(&out)?;
            for k in 0..l.sheets.len() {
                std::fs::write(out.join(manufacture::dxf_name(&l, k)), manufacture::to_dxf(&l, k)?)?;
            }
            std::fs::write(out.join("mold.json"), serde_json::to_string_pretty(&l)?)?;
            let s = &l.stack;
            println!(
                "{}: {} foam layers {} ({:.3} in {}), {} foam pieces{}",
                l.hull,
                s.foam_layers,
                if vertical { "standing upright" } else { "laid flat" },
                s.height_in,
                if vertical { "long" } else { "high" },
                s.foam_pieces,
                if in_mold { format!(", {} hardboard layers between them", s.layers) } else { String::new() }
            );
            println!(
                "foam layers {} in ({:.2} in over the mold)",
                s.foam_order_in.iter().map(|t| t.to_string()).collect::<Vec<_>>().join(" + "),
                s.over_in
            );
            println!(
                "{} hardboard {} on {} sheets ({}) ({:.0}% used); wrote {}",
                l.pieces.len(),
                if in_mold { "boards" } else { "templates" },
                l.sheets.len(),
                {
                    // "11 x (96 x 48), 1 x (48 x 48)", sizes in first-used order.
                    let mut seen: Vec<([f64; 2], usize)> = vec![];
                    for sh in &l.sheets {
                        match seen.iter_mut().find(|(z, _)| *z == [sh.length_in, sh.width_in]) {
                            Some((_, n)) => *n += 1,
                            None => seen.push(([sh.length_in, sh.width_in], 1)),
                        }
                    }
                    seen.iter().map(|([a, b], n)| format!("{n} x ({a} x {b})")).collect::<Vec<_>>().join(", ")
                },
                100.0 * l.utilization,
                out.display()
            );
        }
        Cmd::Race { result, params, out, measured } => {
            use jaguar::race;
            let input = report::race_input(&std::fs::read_to_string(&result)?)?;
            let mut rp = match params {
                Some(p) => Params::load(&p)?.race,
                None => input.race.clone(),
            }
            .ok_or_else(|| anyhow::anyhow!("no [race] section (add one to canoe.toml, or pass --params)"))?;
            if let Some(m) = measured {
                let (event, secs) = m
                    .rsplit_once('=')
                    .ok_or_else(|| anyhow::anyhow!("--measured wants \"EVENT=SECONDS\""))?;
                let secs: f64 = secs.trim().parse()?;
                let top = race::topsides(&input.hull, input.depth_in, input.topsides);
                let p = race::calibrate_power(&top, input.hull_lb, &rp, event.trim(), secs)?;
                println!("calibrated: paddler_power_w = {p:.1} W ({} in {secs} s)", event.trim());
                rp.paddler_power_w = p;
            }
            if let Some(why) = &input.fails {
                println!("note: racing a design that does not pass every check ({why})");
            }
            let r = race::run(&input.hull, input.hull_lb, input.depth_in, input.topsides, &rp)?;
            for l in report::race_lines(&r) {
                println!("{l}");
            }
            std::fs::write(&out, serde_json::to_string_pretty(&r)?)?;
            println!("wrote {}", out.display());
        }
        Cmd::Serve { params, port, prefs } => serve::serve(&params, port, prefs)?,
    }
    Ok(())
}

/// "96x48" -> [96, 48].
fn size_arg(s: &str) -> Result<[f64; 2]> {
    let (l, w) = s
        .trim()
        .split_once(['x', 'X'])
        .ok_or_else(|| anyhow::anyhow!("sheet size '{s}' should be LxW, e.g. 96x48"))?;
    Ok([l.trim().parse()?, w.trim().parse()?])
}
