#!/usr/bin/env bash
# Runs on the EXISTING authorized EC2 host, only after explicit promotion approval.
set -Eeuo pipefail
cd "${DEPLOY_APP_DIR:-/opt/portfolio-support-copilot}"
image="${1:?Immutable image digest required}"
[[ "$image" =~ ^ghcr.io/albatross679/langgraph-customer-support-agent@sha256:[a-f0-9]{64}$ ]] || exit 2
[[ -f .env && -f docker-compose.yml && -f compose.image.yml ]] || exit 2
# Never read, print, overwrite, or transfer runtime credentials here.
previous="$(docker inspect "$(docker compose ps -q api)" --format '{{.Config.Image}}')"
[[ "$previous" =~ ^ghcr.io/albatross679/langgraph-customer-support-agent@sha256:[a-f0-9]{64}$ ]] || {
  echo 'Existing deployment needs an approved one-time immutable-image baseline before rollback is possible.' >&2
  exit 2
}
compose() { APP_IMAGE="$1" docker compose -f docker-compose.yml -f compose.image.yml "${@:2}"; }
healthy() {
  local candidate="$1"
  for _ in $(seq 1 30); do
    if curl --fail --silent --max-time 5 http://127.0.0.1:8000/ready >/dev/null &&
       compose "$candidate" exec -T worker arq --check-health support_copilot.worker.WorkerSettings >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}
rollback() {
  trap - ERR
  echo 'Promotion failed; restoring prior immutable image.' >&2
  compose "$previous" up -d --no-build --no-deps api worker
  healthy "$previous" || { echo 'Rollback health failed; operator intervention required.' >&2; exit 1; }
  exit 1
}
trap rollback ERR
compose "$image" pull init api worker
# Schema changes must be additive and compatible with the previous image; never reset demo data.
compose "$image" run --rm --no-deps -e RESET_DEMO_DATA=0 init
compose "$image" up -d --no-build --no-deps api worker
healthy "$image"
printf '%s\n' "$previous" > previous-image.txt
printf '%s\n' "$image" > current-image.txt
compose "$image" ps
