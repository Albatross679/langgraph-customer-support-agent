# EC2 demo deployment

The existing authorized demo is one Ubuntu EC2 instance in us-east-2 at http://18.219.217.47:8000. It runs the synthetic physical-media store with simulated refunds. There is no payment provider, customer authentication or employee authorization. Do not introduce real customer data.

## Promotion policy

Production promotion is manual and approval-gated, not automatic on main. `.github/workflows/deploy.yml` accepts a full merged main commit SHA, checks production required reviewers and an explicit main-only environment policy, runs backend/frontend and isolated real-stack test gates, builds and publishes the SHA-tagged image to GHCR, and promotes its immutable `@sha256` digest after required environment review. Main CI runs tests and Docker builds but never deploys.

No production-changing deployment, migration or restart is authorized by preparing these files. Wait for the change to merge and an explicit deployment approval naming the target/revision before triggering promotion. This task did not provision any AWS resource or change the running demo.

## Exact operator inputs

- The existing GitHub repository needs a production environment with required reviewers and main-only deployment protection. The workflow fails before publication if required reviewers are absent.
- `EC2_SSH_KEY` must reference the already authorized deployment key through the approved existing secret mechanism. Do not create another key or credential store. The workflow uses an ephemeral runner file and deletes it afterward.
- `EC2_KNOWN_HOSTS` must contain the verified host-key entry for 18.219.217.47. Do not use unauthenticated ssh-keyscan output as proof of host identity, and do not disable strict host checking.
- The existing host must pull the digest from GHCR. Prefer the already public repository's public image visibility; if registry authentication is needed, supply an approved existing integration rather than inventing a credential store.
- Before promotion, the target needs a verified prior immutable GHCR image and existing `/opt/portfolio-support-copilot/.env` and `docker-compose.yml`. API and worker must use the same verified prior image. A mixed-version pair or source-built tag is not an immutable rollback baseline. `promote.sh` refuses it instead of guessing. Establishing that first baseline is a separately approved production change, not performed here.

Read-only forge verification for this task found no configured environments, repository secret names or variable names. These are missing inputs, not a successful deployment receipt.

## Health and rollback

The workflow transfers only `promote.sh` and `compose.image.yml` to the existing app directory; it never transfers keys or rewrites runtime `.env`. `promote.sh` requires an immutable approved-registry digest, pulls the image, runs the one-time bootstrap with `RESET_DEMO_DATA=0`, and replaces only API/worker services without source builds. `/ready` verifies Postgres durable-run schema access and Redis connectivity. The worker's arq health key must also pass through the installed CLI's `arq --check` command. Health is polled for up to 30 attempts.

On pull, bootstrap, promotion or health failure, the script restores the prior pinned image and checks health again. An unhealthy rollback exits nonzero and requires operator intervention. `previous-image.txt` and `current-image.txt` are written only after a healthy promotion. The local executable regression suite covers success, pull failure, bootstrap failure, unhealthy promotion, unhealthy rollback and rejection of unversioned images. Those command-adapter tests are mechanism evidence, not a real EC2 deployment.

Schema changes must remain additive and compatible with the verified rollback image. The first refund-ledger release adopts existing approved demo orders as legacy simulated actions. Old Redis-only run records are not imported as new approvals: outstanding pre-upgrade reviews must be restarted in a new conversation. The rollback baseline must support the checkpointed proposal node and durable-run schema, or promotion must remain blocked. Do not wipe the live database or change refund history to make a rollout succeed.

## Existing stage-one tools

`provision.sh`, `deploy.sh` and `teardown.sh` are the historical manual lifecycle tools. They load credentials through the existing shell helper, use the documented private key at `~/.aws/portfolio-support-copilot-ec2.pem`, and target `/opt/portfolio-support-copilot`. `deploy.sh` synchronizes source and builds on the host, so it is not the immutable promotion path. Do not invoke any lifecycle tool without explicit authorization. No new instance, security group, public endpoint or credential store is needed for the promotion workflow.

## Verification record

Task evidence in `evidence/support-gap-bridge/` records AWS read-only identification of instance `i-061f12bce30cfa151`, AMI `ami-02c98622c4c3f017d`, running `t3.small`, x86_64, matching the authorized public IP and project tag. The public health GET succeeded and the actual landing page loaded through a task-specific browser bridge. SSH timed out, so the deployed source/image revision, Compose service health and bootstrap state remain unverified. Public HTTP health alone does not prove the model/queue/database pathway or a deployment of this branch.
