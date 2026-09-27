# fwtool: server firmware inventory and age reporting (Dell iDRAC + HPE iLO)

Phase 1 of the firmware lifecycle project. `fwtool` reads a CSV of BMC IP addresses, pulls the firmware
inventory from every Dell iDRAC8/iDRAC9 and HPE iLO 4/5/6 over Redfish, and reports how old each
component is, how far behind the latest release it is, and which servers to deal with first.

**Phase 1 is strictly read-only.** The Redfish client refuses every HTTP method except GET (a unit test
enforces this), never creates Redfish sessions and never changes anything on a BMC. Phase 2 (updates)
is design-only and lives in `docs/` once approved.

## Setup

Python 3.10+ on a Windows or Linux jump host that reaches the BMC VLAN on HTTPS/443.

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e .                # or: pip install -e ".[test]" to run the tests
fwtool --version
```

BMC traffic always goes direct: proxy and CA-bundle environment variables (`HTTPS_PROXY`,
`REQUESTS_CA_BUNDLE`, ...) are ignored for BMCs. The Dell catalog download is internet traffic and does
use the corporate proxy from the environment.

## Credentials

One read-only account per vendor (or one shared account). Lookup order:

1. `DELL_BMC_USER` / `DELL_BMC_PASS` and `HPE_BMC_USER` / `HPE_BMC_PASS`
2. `BMC_USER` / `BMC_PASS` (shared account)
3. The same keys in a file passed with `--env-file` (see `examples/env.example`; `chmod 600` it)
4. An interactive prompt for each vendor still missing (blank username skips that vendor)

Credentials are never logged or written: they are not in the log file, state files, raw JSON or reports,
and a log filter masks them if anything tries. The provider interface (`fwtool.credentials.CredentialProvider`)
is looked up per server, so a HashiCorp Vault / CyberArk provider can be added later without other changes.

## Input CSV

Required columns: `name`, `bmc_ip`. Optional: `support` (`hpe` / `tpm`), `site`, `environment`,
`os` (`esxi` / `linux` / `windows`), `platform` (`vxrail` / `vxflex` / `vsan`), `vendor` (`dell` / `hpe`),
and the CMDB model column (`model`, `model id`, `Modle id` or `manufacturer`, e.g. `Dell Inc. PowerEdge R640`).
When there is no `vendor` column, the vendor is taken from the model text; rows for other vendors
(Cisco UCS, IBM, Lenovo, ...) are skipped up front and counted as "other vendors skipped".
A UTF-8 BOM, `;` or tab delimiters, extra columns (kept in state files), blank rows and duplicate IPs
(first row wins, others listed in the log) are all handled. See `examples/servers.csv`.

## Usage

```bash
# 1. Dell catalog: download and cache (uses the proxy), or import a file you downloaded
fwtool catalog refresh
fwtool catalog import Catalog.xml.gz

# 2. Pilot: 5 Dell + 5 HPE servers first, with raw JSON for troubleshooting
fwtool inventory -i pilot.csv --save-raw --dell-esxi-catalog ESXi_Catalog.xml.gz

# 3. Full run
fwtool inventory -i cmdb_export.csv --baselines baselines.yaml

# Interrupted? Continue the same run; servers already collected are skipped, failed ones retried
fwtool inventory -i cmdb_export.csv --resume runs/20260927-101500

# Re-analyse a finished run after updating the catalog / reference / baselines (no BMC traffic)
fwtool report --run-dir runs/20260927-101500 --baselines baselines.yaml
```

Useful options: `-w/--workers` (default 25), `--timeout` (30 s), `--retries` (3),
`--verify-tls [CA_BUNDLE]` (off by default: BMCs use self-signed certificates), `--offline` (never
download the catalog), `--target latest|n-1`, `--limit N`, `--vxrail-list FILE`, `--config config.yaml`
(see `examples/config.yaml`), `-v` for debug logging.

Each run writes to `runs/<timestamp>/`:

| File | Contents |
|---|---|
| `servers.csv` | One row per server: identity, BMC/BIOS versions vs latest, status, age summary, priority score and rank, VxRail flag, support type, error and fix hint |
| `firmware.csv` | One row per component: category, installed/latest version, release dates, age in days, versions behind, status, target and compliance, update track |
| `report.xlsx` | Summary, Worst 100, Model x Component version spread, Failures & Partials, plus full Servers and Firmware sheets |
| `state/<ip>.json` | Collected data per server (used for resume and `fwtool report`) |
| `raw/<ip>.json` | Raw Redfish responses (`--save-raw` only) |
| `fwtool.log` | Structured JSON-lines log |

Run folders contain serial numbers and IPs; they are git-ignored.

## What is collected

Identity (vendor, model, serial / service tag, generation, BMC type and version, power state, health
rollup, Dell SystemID), and the full `UpdateService/FirmwareInventory` (`$expand` first, then one GET per
member). Dell `Previous-*` / `Available-*` entries are skipped and the component ID is taken from the
inventory Id. HPE `Oem.Hpe.DeviceContext` is kept as the location; HPE drives come from `Storage`
(or `SmartStorage` on iLO 4) and PSUs from `Chassis/Power`. Every component is mapped to one of
BIOS, BMC, CPLD, Storage, NIC, Drive, PSU, Other.

**VxRail** nodes are flagged when the model or SKU contains "VxRail", the CSV `platform` is `vxrail`, or
the IP / name / service tag is in `--vxrail-list`. **VxFlex / PowerFlex** nodes (e.g. "VxFlex integrated
rack R640 C") are flagged the same way in the `appliance` column. Both are reported with
`update_eligible = no` and must only be updated through VxRail Manager / PowerFlex Manager.

Supported and tested generations: Dell 13G (iDRAC8) to 16G (iDRAC9), including R640, R650 and R760XD2;
HPE Gen8 and Gen9 (iLO 4), Gen10 and Gen10 Plus (iLO 5), and Gen11 (iLO 6). Gen8 System ROMs are
versioned by date (`P70 07/01/2015`) and are compared as `2015.07.01`.

**Failures** are classified as `unreachable`, `tls`, `auth-401`, `forbidden-403`, `redfish-unsupported`,
`no-credentials` or `error`, each with a fix hint. **Partial** means the server answered but some
endpoints are missing (typical for old iDRAC8 and iLO 4 firmware); whatever could be read, at least
BIOS and BMC, is still analysed.

## Status, age and compliance

Per component:

* `current`: installed = latest known release
* `behind`: a newer release exists; `versions_behind` counts the releases newer than the installed one
* `unknown`: the version cannot be compared (for example CPLD `0x2A`), or a 13G component is newer than the stale catalog
* `no-reference`: the component or model is not in the Dell catalog / HPE reference

`age_days` is the time since the installed version's release date, when known. `behind_note` explains
edge cases: *older than oldest catalog entry* (versions_behind is then a minimum), *installed version
not in catalog*, *newer than catalog*.

**Compliance** is checked against, in order: `baselines.yaml` for that model, the Dell ESXi/vSAN
validated-stack catalog (for servers with `os=esxi` / `platform=vsan`, when passed), or the target
policy: `n-1` (default: latest or one release behind is compliant) or `latest`.

**HPE `support=tpm`** servers: behind BIOS/CPLD components are marked *entitlement required*; behind
iLO / controller / NIC / drive components are marked *security-only track*.

### Priority score

Each server gets points for every out-of-date component; the ranking puts the most points first.

    points = weight(category) x (1 + age in years, capped at 3)      for a 'behind' component
             x 1.5 if it is also below its target/baseline (non-compliant)
    points = weight x 0.5                                             for an 'unknown' component

Default weights: BIOS 5, BMC 5, CPLD 3, Storage 3, NIC 2, Drive 1, PSU 1, Other 1. So a BIOS that is
two years out of date and below baseline scores 5 x 3 x 1.5 = 22.5, while an out-of-date PSU scores
about 2. Everything is configurable in `config.yaml` (`scoring:`), including per-environment
multipliers such as `{prod: 1.5}`.

## Maintaining the reference data

### Dell catalog

`fwtool catalog refresh` downloads `https://downloads.dell.com/catalog/Catalog.xml.gz` (the Lifecycle
Controller catalog) when the cached copy is older than 7 days; `inventory` does this automatically
unless `--offline`. Each new catalog version is archived in `cache/dell/archive/` and all archived
catalogs are merged, so version history (and therefore release dates and versions behind) improves
with every refresh. Keep the `cache/` folder between runs.

Known limitation: Dell's live catalog carries mostly the current release per component, so on a first
run many Dell components show *older than oldest catalog entry* without an age. 13G (iDRAC8) models
have largely been removed from the catalog; their components show `no-reference`, or `unknown` when
the server is newer than the stale entry.

### hpe_reference.yaml

Nothing is downloaded from HPE at run time. `hpe_reference.yaml` is yours to maintain; the starter
file in this repo was generated from the public HPE SDR `fwpp-gen9/10/11` repository metadata. Refresh it:

```bash
# download repodata/<hash>-primary.xml.gz from
#   https://downloads.linux.hpe.com/SDR/repo/fwpp-gen{9,10,11}/current/  (see repodata/repomd.xml)
fwtool hpe-reference import fwpp-gen9-primary.xml.gz fwpp-gen10-primary.xml.gz fwpp-gen11-primary.xml.gz
```

The import regenerates generated entries and keeps every entry marked `source: manual` plus `spp_level`.
Entry keys the tool looks up automatically:

| Key | Component |
|---|---|
| `system-rom:<family>` | System ROM / BIOS, family from the BIOS string (`U30 v2.76 (02/09/2023)` -> `U30`) |
| `ilo4`, `ilo5`, `ilo6` | iLO firmware |
| `ie:<gen>`, `sps:<family>`, `sps:<gen>` | Innovation Engine, Server Platform Services |
| `drive:<model>` | Drive firmware by drive model |
| anything else | matched with its `match:` regex list against the component name |

To add something the import does not cover (NICs, MR/SR controllers, CPLD, PSUs), add a manual entry:

```yaml
components:
  nic-bcm57414:
    category: NIC
    source: manual               # protects it from re-imports
    match: ['BCM57414']          # regex on the component name as iLO reports it
    generations: [Gen11]         # optional
    versions:                    # newest first
      - {version: "233.0.152.0", date: 2025-06-01, spp: "2025.07.00"}
      - {version: "229.1.123.0", date: 2024-03-01}
```

The HPE BIOS release date always comes from the BIOS version string itself.

### baselines.yaml

Optional per-model targets; see `examples/baselines.yaml`. When present, `compliance` and
`target_version` reflect the baseline. Otherwise they use the target policy (`n-1` by default).

## Performance

Typical cost is 8 to 16 GET requests per server, plus one per drive on HPE. At 25 workers and one second per
request, 2,000 servers take about 20 to 30 minutes. For large HPE estates with many drives, raise
`--workers` (the jump host, not the BMCs, is the limit) or set `collect_hpe_drives: false`.

## Tests

```bash
pip install -e ".[test]"
pytest
```

The tests use mocked Redfish responses (`tests/fixtures/`) for iDRAC8, iDRAC9, a VxRail node,
iLO 4, iLO 5, iLO 6 and partial/failed servers. They need no hardware or network access.
