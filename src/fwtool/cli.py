"""fwtool command line.

  fwtool inventory -i servers.csv [...]     collect + report (read-only)
  fwtool report --run-dir runs/<id>         re-analyse a run (e.g. after a catalog update)
  fwtool catalog refresh | import | info    manage the cached Dell catalog
  fwtool hpe-reference import <files>       build hpe_reference.yaml from HPE fwpp metadata
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .analysis import Analyzer
from .catalog.dell_catalog import DellCatalog, catalog_paths, import_catalog, refresh_catalog
from .collector import CollectOptions
from .config import Settings
from .credentials import build_provider
from .inputs import load_servers
from .logging_setup import setup_logging
from .reference.baselines import Baselines
from .reference.hpe_reference import HpeReference, import_fwpp, write_reference
from .report.build import build_reports
from .runner import RunState, iter_limit, run_collection

log = logging.getLogger("fwtool")


def _verify_arg(value: str | None) -> bool | str:
    if value is None:
        return False
    if value in ("", "true", "yes", "1"):
        return True
    return value  # CA bundle path


def _add_reference_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="settings YAML (scoring weights, target policy, defaults)")
    p.add_argument("--dell-catalog", action="append", default=[],
                   help="Dell Catalog.xml(.gz) to use; repeatable. Default: cached catalog + archive")
    p.add_argument("--offline", action="store_true", help="never download the Dell catalog; use cache/--dell-catalog only")
    p.add_argument("--cache-dir", help="catalog cache directory (default: cache)")
    p.add_argument("--hpe-reference", default="hpe_reference.yaml", help="HPE reference YAML (default: hpe_reference.yaml)")
    p.add_argument("--baselines", help="baselines.yaml with per-model target versions")
    p.add_argument("--dell-esxi-catalog", help="Dell ESXi_Catalog.xml.gz: baseline for servers with os=esxi")
    p.add_argument("--dell-vsan-catalog", help="Dell VSAN_Catalog.xml.gz: baseline for servers with platform=vsan")
    p.add_argument("--target", choices=["latest", "n-1"], help="compliance target when no baseline applies (default n-1)")
    p.add_argument("--as-of", help="date used to compute ages (YYYY-MM-DD, default today)")


def _build_analyzer(args: argparse.Namespace, settings: Settings) -> Analyzer:
    cache = Path(args.cache_dir or settings.cache_dir) / "dell"
    paths = list(args.dell_catalog)
    if not paths:
        if not args.offline:
            refresh_catalog(cache, url=settings.dell_catalog_url, max_age_days=settings.dell_catalog_max_age_days)
        paths = [str(p) for p in catalog_paths(cache)]
    if not paths:
        log.warning("No Dell catalog available: Dell components will be no-reference. "
                    "Run 'fwtool catalog refresh' or 'fwtool catalog import <file>'.")
    dell = DellCatalog.load(paths)
    baselines = Baselines.load(args.baselines)
    if args.dell_esxi_catalog:
        baselines.add_bundle_catalog(args.dell_esxi_catalog, "os=esxi")
    if args.dell_vsan_catalog:
        baselines.add_bundle_catalog(args.dell_vsan_catalog, "platform=vsan")
    as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else None
    return Analyzer(settings, dell, HpeReference.load(args.hpe_reference), baselines, as_of)


def _settings(args: argparse.Namespace) -> Settings:
    s = Settings.load(args.config)
    if getattr(args, "target", None):
        s.target_policy = args.target
    return s


# ------------------------------------------------------------------ commands
def cmd_inventory(args: argparse.Namespace) -> int:
    settings = _settings(args)
    for name in ("workers", "timeout", "retries"):
        v = getattr(args, name)
        if v is not None:
            setattr(settings, name, v)
    if args.verify_tls is not None:
        settings.verify_tls = _verify_arg(args.verify_tls)

    if args.resume:
        run_dir = Path(args.resume)
        if not (run_dir / "state").is_dir():
            print(f"error: {run_dir} is not a run directory", file=sys.stderr)
            return 2
    else:
        run_dir = Path(args.output) / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(run_dir / "fwtool.log", args.verbose)
    log.info("fwtool %s inventory run in %s", __version__, run_dir)

    records, rep = load_servers(args.input)
    if args.vendor:
        records = [r for r in records if r.vendor == args.vendor]
    records = iter_limit(records, args.limit)
    print(f"Input: {len(records)} server(s) from {args.input} "
          f"(blank rows {rep.blank}, duplicate IPs {len(rep.duplicates)}, missing IP {rep.missing_ip}, "
          f"invalid {len(rep.invalid_ip)})")
    for d in rep.duplicates:
        log.warning("duplicate IP skipped: %s", d)
    if not records:
        print("Nothing to do.")
        return 1

    # Load references before prompting, so a bad file fails fast.
    analyzer = _build_analyzer(args, settings)
    provider = build_provider(args.env_file, interactive=not args.no_prompt)

    vx = set()
    if args.vxrail_list:
        vx = {line.strip().lower() for line in Path(args.vxrail_list).read_text(encoding="utf-8-sig").splitlines()
              if line.strip() and not line.startswith("#")}
    opts = CollectOptions(
        verify=settings.verify_tls, timeout=settings.timeout, connect_timeout=settings.connect_timeout,
        retries=settings.retries, save_raw=args.save_raw, collect_hpe_drives=settings.collect_hpe_drives,
        vxrail_list=frozenset(vx),
    )
    (run_dir / "run.json").write_text(json.dumps({
        "input": str(args.input), "started": datetime.now().isoformat(timespec="seconds"),
        "workers": settings.workers, "timeout": settings.timeout, "retries": settings.retries,
        "verify_tls": bool(settings.verify_tls), "save_raw": args.save_raw, "version": __version__,
    }, indent=1), encoding="utf-8")

    state = RunState(run_dir)
    t0 = time.monotonic()
    counts = run_collection(records, provider, opts, state, workers=settings.workers,
                            retry_failed=not args.skip_failed, progress=not args.no_progress)
    elapsed = time.monotonic() - t0
    print(f"Collected in {elapsed / 60:.1f} min: ok={counts.get('ok', 0)} partial={counts.get('partial', 0)} "
          f"failed={counts.get('failed', 0)} skipped(resume)={counts.get('skipped', 0)}")

    wanted = {r.bmc_ip for r in records}
    results = [r for r in state.all_results() if r.bmc_ip in wanted]
    paths = build_reports(results, analyzer, run_dir, {"Input": str(args.input), "Run directory": str(run_dir)})
    for p in paths.values():
        print(f"  {p}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    setup_logging(run_dir / "fwtool.log", args.verbose)
    settings = _settings(args)
    analyzer = _build_analyzer(args, settings)
    results = RunState(run_dir).all_results()
    if not results:
        print(f"No collected servers in {run_dir}", file=sys.stderr)
        return 1
    for p in build_reports(results, analyzer, args.output or run_dir, {"Run directory": str(run_dir)}).values():
        print(f"  {p}")
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    setup_logging(None, args.verbose)
    settings = Settings.load(args.config)
    cache = Path(args.cache_dir or settings.cache_dir) / "dell"
    if args.action == "refresh":
        p = refresh_catalog(cache, url=args.url or settings.dell_catalog_url, force=True)
        if not p:
            print("Download failed and no cached catalog exists.", file=sys.stderr)
            return 1
        print(f"Catalog cached at {p}")
    elif args.action == "import":
        if not args.file:
            print("catalog import needs a file", file=sys.stderr)
            return 2
        print(f"Catalog cached at {import_catalog(args.file, cache)}")
    paths = catalog_paths(cache)
    cat = DellCatalog.load(paths)
    print(f"Cache: {cache}")
    for src, ver in zip(cat.sources, cat.catalog_versions):
        print(f"  {src}  (catalog version {ver})")
    print(f"  {len(cat)} component IDs")
    return 0


def cmd_hpe_import(args: argparse.Namespace) -> int:
    setup_logging(None, args.verbose)
    data = import_fwpp(args.files, existing=args.output if Path(args.output).exists() else None)
    write_reference(data, args.output)
    print(f"Wrote {args.output}: {len(data['components'])} entries. Review it, add 'source: manual' entries as needed.")
    return 0


# ---------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fwtool", description="Read-only Dell iDRAC / HPE iLO firmware inventory")
    ap.add_argument("--version", action="version", version=f"fwtool {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("inventory", help="collect firmware inventory from BMCs and build reports")
    inv.add_argument("-i", "--input", required=True, help="CSV with name,bmc_ip[,support,site,environment,os,platform,vendor]")
    inv.add_argument("-o", "--output", default="runs", help="parent directory for run folders (default: runs)")
    inv.add_argument("--resume", metavar="RUN_DIR", help="continue an interrupted run; servers already collected are skipped")
    inv.add_argument("--skip-failed", action="store_true", help="on resume, do not retry servers that failed")
    inv.add_argument("-w", "--workers", type=int, help="parallel BMC connections (default 25)")
    inv.add_argument("--timeout", type=float, help="per-request read timeout in seconds (default 30)")
    inv.add_argument("--retries", type=int, help="retries on timeout/429/5xx (default 3)")
    inv.add_argument("--verify-tls", nargs="?", const="true", metavar="CA_BUNDLE",
                     help="verify BMC certificates (optionally against a CA bundle). Off by default")
    inv.add_argument("--save-raw", action="store_true", help="save raw Redfish JSON per server for troubleshooting")
    inv.add_argument("--env-file", help="file with DELL_BMC_USER/PASS, HPE_BMC_USER/PASS or BMC_USER/PASS")
    inv.add_argument("--no-prompt", action="store_true", help="never prompt for credentials")
    inv.add_argument("--vxrail-list", help="file of IPs/names/service tags to treat as VxRail (one per line)")
    inv.add_argument("--limit", type=int, help="only the first N servers (pilot runs)")
    inv.add_argument("--vendor", choices=["dell", "hpe"], help="only rows whose CSV vendor column matches")
    inv.add_argument("--no-progress", action="store_true", help="no progress bar")
    inv.add_argument("-v", "--verbose", action="store_true")
    _add_reference_args(inv)
    inv.set_defaults(func=cmd_inventory)

    rep = sub.add_parser("report", help="re-analyse a run directory and rewrite reports")
    rep.add_argument("--run-dir", required=True)
    rep.add_argument("-o", "--output", help="write reports here instead of the run directory")
    rep.add_argument("-v", "--verbose", action="store_true")
    _add_reference_args(rep)
    rep.set_defaults(func=cmd_report)

    cat = sub.add_parser("catalog", help="manage the cached Dell catalog")
    cat.add_argument("action", choices=["refresh", "import", "info"])
    cat.add_argument("file", nargs="?", help="for import: a pre-downloaded Catalog.xml or Catalog.xml.gz")
    cat.add_argument("--url", help="catalog URL (default Dell LC catalog)")
    cat.add_argument("--cache-dir")
    cat.add_argument("--config")
    cat.add_argument("-v", "--verbose", action="store_true")
    cat.set_defaults(func=cmd_catalog)

    hpe = sub.add_parser("hpe-reference", help="HPE reference file tools")
    hsub = hpe.add_subparsers(dest="action", required=True)
    imp = hsub.add_parser("import", help="build hpe_reference.yaml from HPE SDR fwpp primary.xml(.gz) files")
    imp.add_argument("files", nargs="+")
    imp.add_argument("-o", "--output", default="hpe_reference.yaml")
    imp.add_argument("-v", "--verbose", action="store_true")
    imp.set_defaults(func=cmd_hpe_import)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run with --resume <run dir> to continue.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
