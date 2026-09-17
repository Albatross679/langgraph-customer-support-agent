#!/usr/bin/env bash
# Shared helpers for the single-instance EC2 demo deployment.
set -euo pipefail

readonly AWS_REGION="${AWS_REGION:-us-east-2}"
readonly PROJECT_TAG="portfolio-support-copilot-ec2"
readonly INSTANCE_NAME="portfolio-support-copilot-ec2"
readonly SECURITY_GROUP_NAME="portfolio-support-copilot-ec2-sg"
readonly KEY_NAME="portfolio-support-copilot-ec2"
readonly KEY_PATH="${HOME}/.aws/${KEY_NAME}.pem"
readonly REMOTE_USER="ubuntu"
readonly REMOTE_APP_DIR="/opt/portfolio-support-copilot"

source_aws_credentials() {
  # The deploy credentials and OpenRouter key are intentionally kept outside this repository.
  # ~/.zshrc can contain zsh-only commands or reference unset variables, so its status is not
  # the credential gate.
  local errexit_enabled=0
  local nounset_enabled=0
  [[ $- == *e* ]] && errexit_enabled=1
  [[ $- == *u* ]] && nounset_enabled=1
  # shellcheck disable=SC1090
  set +e +u
  source "${HOME}/.zshrc" >/dev/null 2>&1
  (( errexit_enabled )) && set -e || set +e
  (( nounset_enabled )) && set -u || set +u
  if [[ -z "${AWS_ACCESS_KEY_ID:-}" || -z "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
    echo "AWS credentials are missing after loading ~/.zshrc." >&2
  fi
  : "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID must be exported in ~/.zshrc}"
  : "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY must be exported in ~/.zshrc}"
}

aws_cmd() {
  uv tool run --from awscli aws --region "$AWS_REGION" "$@"
}

instance_id() {
  aws_cmd ec2 describe-instances \
    --filters "Name=tag:Name,Values=${INSTANCE_NAME}" \
      'Name=instance-state-name,Values=pending,running,stopping,stopped' \
    --query 'Reservations[].Instances[].InstanceId' --output text
}

instance_host() {
  local id
  id="$(instance_id)"
  [[ -n "$id" && "$id" != "None" ]] || {
    echo "No active ${INSTANCE_NAME} instance found. Run deploy/ec2/provision.sh first." >&2
    return 1
  }
  aws_cmd ec2 describe-instances --instance-ids "$id" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text
}
