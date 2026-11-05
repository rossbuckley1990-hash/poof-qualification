# POOF Instant: Linux ARM64 qualification (prepared, NOT triggered)

Qualifies one approved OCI application per run on a **disposable GitHub-hosted `ubuntu-24.04-arm` runner**.
Third-party application images are never executed on a developer Mac.

**What a pass means:** the pinned linux/arm64 image starts, does real work, keeps data across a
restart, recovers from a crash, and stays inside the resource bounds, *in a locked-down Linux
container*. **It does not prove POOF compatibility on macOS.** That needs a separate run on Apple's
runtime in a verified isolation environment.

## Candidates (pinned in `images.lock.json`)
| App | Index digest | Provenance published |
|---|---|---|
| Meilisearch | `sha256:b7ba431bf6f9…` | unsigned BuildKit attestation only (explicit risk) |
| Kanboard | `sha256:637989624e53…` | unsigned BuildKit attestation only (explicit risk) |
| Excalidraw | `sha256:f7ee194addd6…` | unsigned BuildKit attestation only (explicit risk) |
| Stirling-PDF | `sha256:ed7b1775c670…` | cosign signature present; verified in CI (identity regex is a draft) |
| Actual Budget (server) | `sha256:06cf186569cf…` | unsigned BuildKit attestation only (explicit risk) |

Unverified provenance is recorded as a risk. It is never treated as evidence of safety.

## Security gates (in order; the job stops at the first failure)
1. **Trigger:** `workflow_dispatch` only, with a `choice` input passed via `env` (no expression injection). No `push`/`pull_request`/`pull_request_target`.
2. **Token:** `permissions: {}` at the top; the job gets `contents: read` only. No repository secrets. `persist-credentials: false`, so no token is written to `.git/config`.
3. **Supply chain:** actions pinned by full commit SHA (`actions/checkout` v7.0.1, `actions/upload-artifact` v7.0.2). cosign v3.1.4 is checksum-verified. Images are pulled **by digest**, then checked for `arm64` and the matching digest.
4. **Runner lockdown** (`ci/lockdown.sh`, before any untrusted pull):
   - an internal Docker network (no egress)
   - `INPUT` drop from that network, so containers can't reach the runner
   - `DOCKER-USER` drop except container-to-container traffic, plus explicit drops for IMDS (169.254.169.254) and the Azure wireserver (168.63.129.16)
   - a size-capped `nosuid,nodev` loopback ext4 filesystem for app data
5. **Isolation probe gate** (`qualify.py probe`): a pinned official Python container proves:
   - no internet, IMDS, wireserver or runner reachability
   - no Docker socket
   - all capabilities dropped and no-new-privileges set
   - no `GITHUB_*`/`ACTIONS_*`/`RUNNER_*` variables
   - only the capped data mount

   **If any check fails, no third-party image is pulled.**
6. **First-party control:** POOF Notes is built in the job and qualified through the identical path, which validates the harness on that runner.
7. **Hardened app run:**
   - `--cap-drop ALL` plus a per-app minimum `--cap-add` list from `apps.json`
   - `no-new-privileges`, `--pids-limit 512`, a memory cap equal to the swap cap, `--cpus 2`
   - `/tmp` as tmpfs; no published ports, no Docker socket, no host paths except the capped data filesystem
8. **Bounded execution:** job ≤ 30 min, each step has its own time limit, and the VM is destroyed afterwards.
9. **Cleanup:** `ci/teardown.sh` always runs and audits for leftover containers, network and mount. Evidence is uploaded as an artifact with 30-day retention.

Residual risks (accepted for a disposable VM):
- Containers share the runner's Linux kernel. A kernel escape would reach a VM that holds only a read-only, job-scoped token and no secrets.
- Image pulls go over the runner's network.

## Per-app checks (`apps.json`, drafts until the first run confirms them)
startup → readiness → real function (search, create a project, rotate a PDF, bootstrap and log in, render) →
graceful stop → restart with the same data → `docker kill` → recovery. Peak memory and CPU are sampled
every 2 s, and data-disk usage is recorded.

## Running it (only after review and explicit approval)
This repository is self-contained. It was exported from the private POOF repository with fresh history
and scanned for personal data, private network addresses and credentials before publishing. Trigger runs manually:

```bash
gh workflow run qualify.yml -f app=control-notes
```

Local check, which executes nothing: `python3 ci/qualify.py app kanboard --out /tmp/q --dry-run`.
