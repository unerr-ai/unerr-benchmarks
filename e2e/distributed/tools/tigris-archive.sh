#!/usr/bin/env bash
# tigris-archive.sh — HOST-side lookup tool for benchmark run data archived to
# Tigris (fly.io S3-compatible object storage) by tools/tigris_archive.py. Runs
# with NO live fleet: the whole point is that fleets get destroyed after a run
# but the archived data (overview, traces, grading, submission, logs, the full
# bundle) survives in Tigris and stays reachable from here.
#
# S3 KEY LAYOUT (written by tigris_archive.py — see its docstring for detail):
#   <prefix>/<benchmark>/<arm>/<YYYY-MM-DD>/<label>/
#       overview.json              grade{resolved,total,pct} + cost{usd,by_tier,turns,...} + counts + instances{}
#       bundle.tgz                 the full tarball (one-shot restore)
#       submission/{preds.json,all_preds.jsonl}
#       grading/{merged.json,<iid>/report.json}
#       traces/<iid>/{events.jsonl,err.txt,engine.log,opencode.db,trajectory.json,sessions.cast}
#       results/{preds.json,meta.jsonl,dead.jsonl,cost-report.md}
#       logs/<file>
#       db/queue.db
#   prefix default "runs"; benchmark ∈ verified|lite|pro|terminal|live_verified; arm ∈ econ|claude;
#   label is the unique run id.
#
# Subcommands:
#   list [--benchmark B] [--arm A] [--date D] [--bucket X]
#       List archived runs (label + a one-line overview: resolved/total, pct, cost —
#       fetched from each run's overview.json). Facets narrow the S3 prefix; omit any
#       to enumerate all values at that level.
#   overview <label> [--benchmark B] [--arm A]
#       Fetch + pretty-print that run's overview.json. Fast "how did run X do and
#       what did it cost" path — no full download.
#   get <label> [--dest DIR] [--only traces|grading|submission|logs|overview|bundle] [--benchmark B] [--arm A]
#       Download a run's objects (all, or one category) to DIR (default
#       out/archive/<label>/). --only bundle just pulls bundle.tgz.
#   -h|--help
#
# A bare <label> with no --benchmark/--arm may match more than one run (same
# label reused across benchmarks/arms). If so this prints every match and asks
# you to narrow with --benchmark/--arm.
#
# CREDENTIALS (never printed — only key LENGTHS if presence must be confirmed):
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / TIGRIS_BUCKET (or BUCKET_NAME) from
#   the environment first; if any are unset, sourced from e2e/distributed/.env.tigris,
#   then e2e/econ/.env.local (dotenv-style KEY=value, first file wins per-key).
#   Endpoint: AWS_ENDPOINT_URL_S3, default https://fly.storage.tigris.dev. If nothing
#   is found anywhere, this prints the provisioning step and exits non-zero:
#     flyctl storage create -a <fleet-app>          # prints a keypair + bucket name
#     # then save into e2e/distributed/.env.tigris:
#     AWS_ACCESS_KEY_ID=...
#     AWS_SECRET_ACCESS_KEY=...
#     TIGRIS_BUCKET=...
#
# S3 backend: boto3 preferred (python3 -c "import boto3"); falls back to the `aws`
# CLI (`aws s3api ...` / `aws s3 cp`) if boto3 isn't importable. Errors clearly if
# neither is available.
#
# Examples:
#   ./tools/tigris-archive.sh list
#   ./tools/tigris-archive.sh list --benchmark verified --arm claude
#   ./tools/tigris-archive.sh overview july16-smoke
#   ./tools/tigris-archive.sh get july16-smoke --only overview
#   ./tools/tigris-archive.sh get july16-smoke --dest /tmp/pull --benchmark lite --arm econ
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # e2e/distributed
PY="${PYTHON:-python3}"

usage() { sed -n '2,54p' "$0" | sed 's/^# \{0,1\}//'; }

SUB="${1:-}"
if [ -z "$SUB" ] || [ "$SUB" = "-h" ] || [ "$SUB" = "--help" ]; then
  usage
  exit 0
fi
shift

BENCHMARK=""; ARM=""; DATE=""; BUCKET_OVERRIDE=""; DEST=""; ONLY=""; LABEL=""
case "$SUB" in
  list)
    while [ $# -gt 0 ]; do
      case "$1" in
        --benchmark) BENCHMARK="${2:?--benchmark needs a value}"; shift 2 ;;
        --arm)       ARM="${2:?--arm needs a value}"; shift 2 ;;
        --date)      DATE="${2:?--date needs a value}"; shift 2 ;;
        --bucket)    BUCKET_OVERRIDE="${2:?--bucket needs a value}"; shift 2 ;;
        -h|--help)   usage; exit 0 ;;
        -*)          echo "ERROR: unknown flag '$1' for list" >&2; exit 2 ;;
        *)           echo "ERROR: unexpected arg '$1' for list" >&2; exit 2 ;;
      esac
    done
    ;;
  overview)
    LABEL="${1:?usage: tigris-archive.sh overview <label> [--benchmark B] [--arm A]}"; shift
    while [ $# -gt 0 ]; do
      case "$1" in
        --benchmark) BENCHMARK="${2:?--benchmark needs a value}"; shift 2 ;;
        --arm)       ARM="${2:?--arm needs a value}"; shift 2 ;;
        --bucket)    BUCKET_OVERRIDE="${2:?--bucket needs a value}"; shift 2 ;;
        -h|--help)   usage; exit 0 ;;
        -*)          echo "ERROR: unknown flag '$1' for overview" >&2; exit 2 ;;
        *)           echo "ERROR: unexpected arg '$1' for overview" >&2; exit 2 ;;
      esac
    done
    ;;
  get)
    LABEL="${1:?usage: tigris-archive.sh get <label> [--dest DIR] [--only CATEGORY] [--benchmark B] [--arm A]}"; shift
    while [ $# -gt 0 ]; do
      case "$1" in
        --dest)      DEST="${2:?--dest needs a value}"; shift 2 ;;
        --only)      ONLY="${2:?--only needs a value}"; shift 2 ;;
        --benchmark) BENCHMARK="${2:?--benchmark needs a value}"; shift 2 ;;
        --arm)       ARM="${2:?--arm needs a value}"; shift 2 ;;
        --bucket)    BUCKET_OVERRIDE="${2:?--bucket needs a value}"; shift 2 ;;
        -h|--help)   usage; exit 0 ;;
        -*)          echo "ERROR: unknown flag '$1' for get" >&2; exit 2 ;;
        *)           echo "ERROR: unexpected arg '$1' for get" >&2; exit 2 ;;
      esac
    done
    case "$ONLY" in
      ''|traces|grading|submission|logs|overview|bundle) : ;;
      *) echo "ERROR: --only must be one of traces|grading|submission|logs|overview|bundle" >&2; exit 2 ;;
    esac
    [ -n "$DEST" ] || DEST="$HERE/out/archive/$LABEL"
    ;;
  *)
    echo "ERROR: unknown subcommand '$SUB' (use list|overview|get|-h)" >&2
    exit 2
    ;;
esac

# ── creds: env first, else e2e/distributed/.env.tigris, else e2e/econ/.env.local ──
# dotenv-style KEY=value; only fills vars not already set in env (env always wins).
# NEVER echoes a value — presence is confirmed by key LENGTH only, if at all.
_source_env_file() {  # <path>
  local f="$1"
  [ -f "$f" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    local k="${line%%=*}" v="${line#*=}"
    k="$(printf '%s' "$k" | tr -d '[:space:]')"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    [ -n "$k" ] || continue
    if [ -z "${!k:-}" ]; then
      export "$k=$v"
    fi
  done <"$f"
}

_creds_missing() {
  [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ] || \
    [ -z "${BUCKET_OVERRIDE:-${TIGRIS_BUCKET:-${BUCKET_NAME:-}}}" ]
}

if _creds_missing; then _source_env_file "$HERE/.env.tigris"; fi
if _creds_missing; then _source_env_file "$HERE/../econ/.env.local"; fi

BUCKET="${BUCKET_OVERRIDE:-${TIGRIS_BUCKET:-${BUCKET_NAME:-}}}"
ENDPOINT="${AWS_ENDPOINT_URL_S3:-https://fly.storage.tigris.dev}"
PREFIX="${TIGRIS_PREFIX:-runs}"
export AWS_DEFAULT_REGION="${AWS_REGION:-auto}"   # so the `aws` CLI fallback signs correctly against Tigris

if [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ] || [ -z "$BUCKET" ]; then
  echo "ERROR: no Tigris credentials/bucket found." >&2
  echo "  checked: env, $HERE/.env.tigris, $HERE/../econ/.env.local" >&2
  if [ -n "${AWS_ACCESS_KEY_ID:-}" ]; then echo "  AWS_ACCESS_KEY_ID: present (len=${#AWS_ACCESS_KEY_ID})" >&2; else echo "  AWS_ACCESS_KEY_ID: MISSING" >&2; fi
  if [ -n "${AWS_SECRET_ACCESS_KEY:-}" ]; then echo "  AWS_SECRET_ACCESS_KEY: present (len=${#AWS_SECRET_ACCESS_KEY})" >&2; else echo "  AWS_SECRET_ACCESS_KEY: MISSING" >&2; fi
  if [ -n "$BUCKET" ]; then echo "  bucket: $BUCKET" >&2; else echo "  bucket: MISSING (set --bucket or TIGRIS_BUCKET)" >&2; fi
  echo >&2
  echo "  Provision once per fleet app:  flyctl storage create -a <fleet-app>" >&2
  echo "  Then save the printed keypair into $HERE/.env.tigris:" >&2
  echo "    AWS_ACCESS_KEY_ID=..." >&2
  echo "    AWS_SECRET_ACCESS_KEY=..." >&2
  echo "    TIGRIS_BUCKET=..." >&2
  exit 3
fi

have_boto3() { "$PY" -c "import boto3" >/dev/null 2>&1; }
have_awscli() { command -v aws >/dev/null 2>&1; }
if ! have_boto3 && ! have_awscli; then
  echo "ERROR: neither boto3 nor the aws CLI is available." >&2
  echo "  install one:  pip install boto3   OR   brew install awscli / your package manager" >&2
  exit 3
fi

# ── the S3 logic — boto3 preferred, aws CLI fallback (script on stdin via `-`,
#    args as argv — same pattern download-all.sh/status.sh use for their
#    inline python). No credential ever touches argv; boto3/aws CLI read them
#    straight from the exported env. ──
"$PY" - "$SUB" "$BUCKET" "$ENDPOINT" "$PREFIX" "$BENCHMARK" "$ARM" "$DATE" "$LABEL" "$DEST" "$ONLY" <<'PY'
import json, os, pathlib, subprocess, sys

sub, bucket, endpoint, prefix, benchmark, arm, date, label, dest, only = sys.argv[1:11]
benchmark = benchmark or None
arm = arm or None
date = date or None


def _err(msg):
    print(f"[tigris-archive] {msg}", file=sys.stderr)


try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
    BACKEND = "boto3"
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=os.environ.get("AWS_REGION", "auto"),
        config=Config(retries={"max_attempts": 5, "mode": "standard"}, s3={"addressing_style": "path"}),
    )
except ImportError:
    import shutil
    if not shutil.which("aws"):
        _err("neither boto3 nor the aws CLI is available")
        sys.exit(3)
    BACKEND = "awscli"
    ClientError = Exception


def list_common_prefixes(p):
    """immediate 'folder' prefixes under p (p is '' or ends with '/')."""
    if BACKEND == "boto3":
        out = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=p, Delimiter="/"):
            for cp in page.get("CommonPrefixes") or []:
                out.append(cp["Prefix"])
        return out
    cmd = ["aws", "s3api", "list-objects-v2", "--bucket", bucket, "--prefix", p,
           "--delimiter", "/", "--endpoint-url", endpoint, "--output", "json"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        _err(f"list-objects-v2 failed: {r.stderr.strip()}")
        return []
    d = json.loads(r.stdout) if r.stdout.strip() else {}
    return [cp["Prefix"] for cp in (d.get("CommonPrefixes") or [])]


def list_objects(p):
    """every object key under prefix p (no delimiter)."""
    if BACKEND == "boto3":
        out = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=p):
            for obj in page.get("Contents") or []:
                out.append(obj["Key"])
        return out
    keys, token = [], None
    while True:
        cmd = ["aws", "s3api", "list-objects-v2", "--bucket", bucket, "--prefix", p,
               "--endpoint-url", endpoint, "--output", "json"]
        if token:
            cmd += ["--starting-token", token]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            _err(f"list-objects-v2 failed: {r.stderr.strip()}")
            break
        d = json.loads(r.stdout) if r.stdout.strip() else {}
        keys.extend(obj["Key"] for obj in (d.get("Contents") or []))
        token = d.get("NextToken")
        if not token:
            break
    return keys


def get_text(key):
    if BACKEND == "boto3":
        try:
            r = s3.get_object(Bucket=bucket, Key=key)
            return r["Body"].read().decode("utf-8", "replace")
        except ClientError:
            return None
    import tempfile
    fd, tmp = tempfile.mkstemp()
    os.close(fd)
    try:
        cmd = ["aws", "s3api", "get-object", "--bucket", bucket, "--key", key,
               "--endpoint-url", endpoint, tmp]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return None
        return pathlib.Path(tmp).read_text(encoding="utf-8", errors="replace")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def download_file(key, dest_path):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if BACKEND == "boto3":
        try:
            s3.download_file(bucket, key, str(dest_path))
            return True
        except ClientError as e:
            _err(f"FAILED {key}: {e}")
            return False
    cmd = ["aws", "s3", "cp", f"s3://{bucket}/{key}", str(dest_path), "--endpoint-url", endpoint]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        _err(f"FAILED {key}: {r.stderr.strip()}")
        return False
    return True


base = (prefix.rstrip("/") + "/") if prefix else ""


def enum_level(level_base, forced):
    if forced:
        return [level_base + forced + "/"]
    return list_common_prefixes(level_base)


def resolve_runs(bench_f, arm_f, date_f, label_f=None):
    """walk <base>/<benchmark>/<arm>/<date>/<label>/, filtering any facet given.
    Returns [{benchmark, arm, date, label, prefix}, ...]."""
    matches = []
    for bp in enum_level(base, bench_f):
        b = bp.rstrip("/").split("/")[-1]
        for ap in enum_level(bp, arm_f):
            a = ap.rstrip("/").split("/")[-1]
            for dp in enum_level(ap, date_f):
                d = dp.rstrip("/").split("/")[-1]
                for lp in list_common_prefixes(dp):
                    l = lp.rstrip("/").split("/")[-1]
                    if label_f and l != label_f:
                        continue
                    matches.append({"benchmark": b, "arm": a, "date": d, "label": l, "prefix": lp})
    return matches


def print_ambiguous(matches, label_f):
    _err(f"label={label_f!r} is ambiguous — {len(matches)} match(es):")
    for m in sorted(matches, key=lambda r: (r["benchmark"], r["arm"], r["date"])):
        _err(f"    benchmark={m['benchmark']} arm={m['arm']} date={m['date']}")
    _err("disambiguate with --benchmark/--arm")


if sub == "list":
    runs = resolve_runs(benchmark, arm, date)
    if not runs:
        _err("no runs found" + (f" benchmark={benchmark}" if benchmark else "")
             + (f" arm={arm}" if arm else "") + (f" date={date}" if date else ""))
        sys.exit(0)
    runs.sort(key=lambda r: (r["benchmark"], r["arm"], r["date"], r["label"]))
    for r in runs:
        line = f"{r['benchmark']:<10} {r['arm']:<7} {r['date']}  {r['label']}"
        txt = get_text(r["prefix"] + "overview.json")
        if txt:
            try:
                ov = json.loads(txt)
                g = ov.get("grade") or {}
                c = ov.get("cost") or {}
                res, tot, pct = g.get("resolved"), g.get("total"), g.get("pct")
                usd = c.get("usd")
                if res is not None:
                    line += f"   resolved={res}/{tot} ({pct}%)  cost=${usd}"
                else:
                    line += "   (overview.json has no grade)"
            except Exception:
                line += "   (overview.json unparseable)"
        else:
            line += "   (no overview.json)"
        print(line)
    _err(f"{len(runs)} run(s)  s3://{bucket}/{base}")

elif sub == "overview":
    matches = resolve_runs(benchmark, arm, None, label_f=label)
    if not matches:
        _err(f"no run found for label={label!r}"
             + (f" benchmark={benchmark}" if benchmark else "") + (f" arm={arm}" if arm else ""))
        sys.exit(1)
    if len(matches) > 1:
        print_ambiguous(matches, label)
        sys.exit(1)
    m = matches[0]
    txt = get_text(m["prefix"] + "overview.json")
    if not txt:
        _err(f"no overview.json at s3://{bucket}/{m['prefix']}")
        sys.exit(1)
    try:
        ov = json.loads(txt)
    except Exception as e:
        _err(f"overview.json unparseable: {e}")
        sys.exit(1)
    print(json.dumps(ov, indent=2))

elif sub == "get":
    matches = resolve_runs(benchmark, arm, None, label_f=label)
    if not matches:
        _err(f"no run found for label={label!r}"
             + (f" benchmark={benchmark}" if benchmark else "") + (f" arm={arm}" if arm else ""))
        sys.exit(1)
    if len(matches) > 1:
        print_ambiguous(matches, label)
        sys.exit(1)
    m = matches[0]
    run_prefix = m["prefix"]
    dest_dir = pathlib.Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if only == "bundle":
        keys = [run_prefix + "bundle.tgz"]
    elif only == "overview":
        keys = [run_prefix + "overview.json"]
    elif only in ("traces", "grading", "submission", "logs"):
        keys = list_objects(run_prefix + only + "/")
    else:
        keys = list_objects(run_prefix)

    ok = 0
    for k in keys:
        rel = k[len(run_prefix):]
        if not rel:
            continue
        if download_file(k, dest_dir / rel):
            ok += 1
    total = len(keys)
    _err(f"benchmark={m['benchmark']} arm={m['arm']} date={m['date']}  {ok}/{total} object(s) -> {dest_dir}/")
    sys.exit(0 if ok == total else 4)
PY
