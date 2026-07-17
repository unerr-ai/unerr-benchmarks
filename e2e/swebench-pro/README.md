# SWE-bench Pro + Verified — private image mirror

`mirror-sweap-images.sh` is a **shared mirror script** for two SWE-bench datasets'
prebuilt eval images, selected with `DATASET=pro` (default) or `DATASET=verified`
(or `--dataset pro`/`--dataset verified`). Same account, same idempotent/resumable
design, different upstream shape per dataset (below). It's **on-demand** — run it
whenever you want to (re)sync — and **idempotent + resumable**, so a re-run only
copies what's missing.

> This is a **Docker Hub → Docker Hub image mirror**. It is *not* the fly Tigris
> pull-through cache in [`../distributed/registry/`](../distributed/registry/) — that
> one warms an in-region proxy for the *standard* SWE-bench run fleet. Different
> mechanism, different purpose; both can coexist.

## SWE-bench Pro (`DATASET=pro`, default)

[SWE-bench Pro](https://github.com/scaleapi/SWE-bench_Pro-os) (Scale AI) ships a
prebuilt Docker image per instance in **one** public Docker Hub repo,
[`jefzda/sweap-images`](https://hub.docker.com/r/jefzda/sweap-images) — one **tag**
per instance (the HuggingFace dataset
[`ScaleAI/SWE-bench_Pro`](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro)
carries the tag in its `dockerhub_tag` column). At time of writing that's **1002
tags, ~1.43 TB** compressed.

`mirror-sweap-images.sh` copies those images into an **account we control**
(`51jaswanth15/sweap-images`), the same way you'd want a private copy of the
standard SWE-bench images: no dependence on the upstream repo staying up, our own
rate-limit / retention story, and a stable base for runs.

The destination keeps the **identical repo name and tag strings**, because the eval
harness (`swe_bench_pro_eval.py` → `helper_code/image_uri.py`) builds the image ref
as `{dockerhub_username}/sweap-images:{tag}`. So after mirroring, the only change to
run the Pro eval against our copy is `--dockerhub_username=51jaswanth15`.

## Prerequisites

- **`crane`** — `brew install crane` (or `go install
  github.com/google/go-containerregistry/cmd/crane@latest`). Also needs `python3` + `curl`.
- **`DATASET=verified` only** — the `swebench` + `datasets` python packages importable
  (used to derive each instance's exact eval-image name — see below). If they aren't
  already on `PATH` or at `/work/.venv` (a swebench eval image), the script bootstraps
  a scratch venv for them automatically on first run (`./.verified-venv/`, git-ignored).
- **A Docker Hub Personal Access Token with `Read & Write` scope**, and a login:
  ```bash
  docker login -u 51jaswanth15        # paste the Read & Write PAT
  ```
  A **read-only** PAT authenticates and can *pull* but the push is rejected mid-copy
  with `access token has insufficient scopes` — regenerate it under
  **Docker Hub → Account Settings → Personal access tokens** as **Read & Write**.
  (`crane` reads `~/.docker/config.json`; alternatively export `DOCKERHUB_USER` +
  `DOCKERHUB_TOKEN` and the script logs in for you.)

## Usage

```bash
cd e2e/swebench-pro

./mirror-sweap-images.sh --status        # how many of the N tags are mirrored (pro, default)
DRY_RUN=1 ./mirror-sweap-images.sh       # list what WOULD copy, do nothing
LIMIT=5 ./mirror-sweap-images.sh         # copy only the first 5 missing (smoke test)
./mirror-sweap-images.sh                 # mirror ALL missing tags
CONCURRENCY=12 ./mirror-sweap-images.sh  # more parallel copies (default 8)

DATASET=verified ./mirror-sweap-images.sh --status   # same knobs, Verified shape (see below)
```

Re-running is always safe: it lists both sides, subtracts what the destination
already has (by tag name — one cheap catalog call per side, **not** a per-tag pull),
and copies only the remainder. If a run is interrupted or hits a rate limit, just run
it again and it picks up where it left off. Failed tags are written to
`mirror-logs/failed-tags.txt`; retry just those with
`TAGS_FILE=mirror-logs/failed-tags.txt ./mirror-sweap-images.sh` (add `DATASET=verified`
too if that's the run you're retrying).

### Finishing across the pull rate limit (`auto-resume.sh`)

A full first-time sync **will** hit Docker Hub's source pull limit — measured at
**200 pulls / hour** on `jefzda` (`Ratelimit-Limit: 200;w=3600`). One pass copies
~200–650 tags, then the rest return `TOOMANYREQUESTS`. Because each copy needs exactly
one manifest read from the source, no trick avoids that ceiling — you just wait for the
bucket to refill and resume.

`auto-resume.sh` automates that: it re-runs the (resumable) mirror every `SLEEP`
seconds (default 3700 = just over the 1-hour window) until the destination has every
source tag, then exits. Hands-off; leave it running. It's dataset-aware too —
`DATASET=verified` passes straight through to the mirror runs it drives, and it
computes its own src/dst totals for whichever shape is selected (single-repo tag
count for pro, HF-split row count / per-namespace repo count for verified).

```bash
nohup ./auto-resume.sh >> mirror-logs/auto-resume.out 2>&1 &   # runs until 1002/1002 (pro)
DATASET=verified nohup ./auto-resume.sh >> mirror-logs/auto-resume.out 2>&1 &  # runs until 500/500
```

To finish in a single ~5-minute pass instead, pull from an account **without** the free
limit (Docker Pro / Team, or a paid org) — set `DOCKERHUB_USER`/`DOCKERHUB_TOKEN` (or
`docker login`) to that account before running the mirror.

### Env vars

| Var | Default | Meaning |
|---|---|---|
| `DATASET` | `pro` | `pro` or `verified` — selects the source/dest shape (also `--dataset pro`\|`verified`) |
| `SRC_REPO` | `jefzda/sweap-images` (pro) \| `swebench` (verified) | source repo/namespace |
| `DST_REPO` | `51jaswanth15/sweap-images` (pro) \| `51jaswanth15` (verified) | destination repo/namespace |
| `CONCURRENCY` | `8` | parallel copies |
| `LIMIT` | `0` (all) | cap units processed this run |
| `TAGS_FILE` | — | newline-separated unit allowlist to use instead of enumerating the source |
| `FORCE` | `0` | re-copy even units already on the destination |
| `DRY_RUN` | `0` | plan only, copy nothing |
| `RETRIES` | `4` | per-unit copy attempts on transient / 429 errors |
| `LOG_DIR` | `./mirror-logs` | run log + `failed-tags.txt` (git-ignored) |
| `DOCKERHUB_USER` + `DOCKERHUB_TOKEN` | — | if both set, `crane auth login` first |
| `VERIFIED_HF_DATASET` | `princeton-nlp/SWE-bench_Verified` | **verified only** — HF dataset id to enumerate instance_ids from |
| `VERIFIED_HF_SPLIT` | `test` | **verified only** — HF split |
| `VERIFIED_ARCH` | `x86_64` | **verified only** — image arch baked into the eval-image name |
| `VERIFIED_VENV` | `./.verified-venv` | **verified only** — scratch venv path if `swebench`+`datasets` aren't already importable |

## SWE-bench Verified (`DATASET=verified`)

[SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/) is the
official [princeton-nlp](https://github.com/princeton-nlp/SWE-bench) harness's
human-filtered 500-instance subset. Unlike Pro's one-repo-many-tags shape, the
official harness builds and hosts **one Docker Hub repo per instance**, all under the
public `swebench` org, each with a single `latest` tag —
`docker.io/swebench/sweb.eval.x86_64.<instance-key>:latest`.

**The `<instance-key>` is not a guess.** It's derived by running the *installed*
`swebench` package's own naming logic — `TestSpec.instance_image_key`
(`swebench.harness.test_spec.test_spec`), via
`make_test_spec(instance, namespace="swebench").instance_image_key` — over every
`instance_id` in the HF dataset `princeton-nlp/SWE-bench_Verified` (split `test`,
500 rows). That property is also what performs the harness's own
`__` → `_1776_` substitution (Docker Hub repo names can't hold a double
underscore), e.g. `django__django-10097` → `django_1776_django-10097`. Hand-rolling
that substitution would silently produce the wrong ref, so the script always computes
it via the real package rather than a local re-implementation.

`mirror-sweap-images.sh` mirrors each of those 500 per-instance repos into
`51jaswanth15/sweb.eval.x86_64.<instance-key>:latest` — same repo-name shape, same
tag, just under our namespace:

```bash
DATASET=verified ./mirror-sweap-images.sh --status        # 0/500, 500/500, etc.
DATASET=verified DRY_RUN=1 LIMIT=5 ./mirror-sweap-images.sh
DATASET=verified ./mirror-sweap-images.sh                  # mirror ALL missing
```

Idempotency works the same way as Pro but on the other axis: since there's no single
shared destination repo to `crane ls`, the "already mirrored" side is one paginated
Docker Hub web-API call listing every repo under the `51jaswanth15` namespace,
filtered to the `sweb.eval.` prefix — repo-name presence (⇒ its one `latest` tag)
counts as mirrored, the same "immutable, so name-presence == mirrored" assumption
Pro makes on tags. `FORCE=1` still means "re-copy even if present."

## How it works — and why it's usually cheap

Source and destination are on the **same registry** (`registry-1.docker.io`) for
both datasets. `crane copy` streams registry→registry and, for a same-registry
destination, asks Docker Hub to **cross-repo mount** each layer blob by digest
(`POST .../blobs/uploads/?mount=<digest>&from=<src-repo>`) instead of re-uploading
it. When Hub honours the mount, the copy writes only the manifest + config (a few
KB) and finishes in **seconds** — no gigabytes shovelled through the machine running
the script.

**Read the per-tag time the script prints to tell which happened:** a mounted copy is
a few seconds; a real byte-transfer is minutes. If it's transferring for real (mounts
not honoured), run the script from an **in-region cloud box** with a fat pipe rather
than a laptop.

## Gotchas

- **Read & Write PAT required** (see Prerequisites). Read-only fails every push with
  `insufficient scopes`; the script detects this and stops fast with a clear message
  rather than failing 1002 times.
- **Docker Hub pull rate limit.** `crane copy` reads manifests/blobs from the source,
  which counts against the pull limit (authenticated free ≈ 200/6h). A full first-time
  sync of ~1002 tags can hit the cap and stall — fine, it's resumable: re-run after the
  window, or use an account / plan with a higher allowance.
- **Tags are immutable.** SWE-bench Pro tags are commit-pinned, so tag-name presence ==
  mirrored. `FORCE=1` is only for repairing a partial/corrupt earlier push.
- The `…` you may see in a tag on screen is **display-side truncation** of long tags —
  the real strings (ASCII, ≤128 chars) are intact and are what the script copies.
- **`DATASET=verified` needs `swebench`+`datasets` importable** (see Prerequisites) —
  the script bootstraps a scratch venv automatically the first time it can't find them,
  which needs network + a moment to `pip install`; subsequent runs reuse the cached venv.
- **Verified's rate-limit shape differs from Pro's.** Pro hammers ONE source repo
  (`jefzda`), so Docker Hub's per-repo pull bucket is the bottleneck `auto-resume.sh`
  waits out. Verified spreads pulls across 500 different `swebench/*` repos, so a stall
  there is more likely account/IP-level throttling than a single-repo bucket — the same
  `auto-resume.sh` retry-with-backoff loop still applies, just don't assume the exact
  "200/hour, refills in an hour" numbers carry over unchanged.

## Running the Pro eval against the mirror

Once mirrored, point Scale's evaluator at our namespace:

```bash
python swe_bench_pro_eval.py \
  --raw_sample_path=swe_bench_pro_full.csv \
  --patch_path=<your_patches>.json \
  --output_dir=<out> \
  --scripts_dir=run_scripts \
  --dockerhub_username=51jaswanth15
```

## Running the Verified eval against the mirror

Once mirrored, point the official harness's `--namespace` at our account instead of
the default `swebench` (this is the same knob used to disable namespacing entirely,
`--namespace none`, for a from-scratch local build):

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Verified \
  --predictions_path <your_patches>.json \
  --run_id <run-id> \
  --namespace 51jaswanth15
```
