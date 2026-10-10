# Privacy

Sigma does not report usage data. The captured `local-goals` onboarding run launched only local `git` commands. The local-goals doctor capture also launched `gh api`; this hook records that launch but cannot inspect the CLI's own connection. Your agent host may communicate with its model provider under that host's own policy, rather than through Sigma.

The checked-in evidence records scripted local and GitHub-mode onboarding captures. Use `python3 tools/readiness/egress_capture.py run --log egress.jsonl -- <command>` to capture destinations and launch intent; it never records request paths, bodies, or field values.

| Destination | What is sent | When | Config key or env var | Default |
| --- | --- | --- | --- | --- |
| Local `git` executable | command verb | local and GitHub onboarding capture | none | used for local repository operations |
| GitHub CLI (`gh`) | command verb | GitHub-mode onboarding and local-goals doctor capture | GitHub source configuration | launch recorded; its own connection is outside the hook |
| Slack API | configured message payload | opt-in notification | `SIGMA_SLACK_BOT_TOKEN` | not captured (opt-in; traced from `skills/sigma-loop/scripts/slack_client.py:81`) |
| Configured webhook | notification payload | opt-in channel notification | `ledger.autowatch.channel_webhook_url` | not captured (opt-in; traced from `skills/sigma-loop/scripts/channel_notify.py:109`) |
| SMTP server | configured notification | opt-in agent-death notification | `agent_watch.notify.email.enabled` | not captured (opt-in; traced from `skills/sigma-loop/scripts/agent_watch.py:94`) |
| Model host process | prompt supplied by the host | explicitly invoked model session | host configuration | not captured (opt-in; traced from `skills/sigma-loop/scripts/autowatch.py:915`, `skills/sigma-loop/scripts/feature_judge.py:226`, and `skills/sigma-loop/scripts/reviewer.py:239`) |
| Model host process (drift judge) | question and reference document text, only when the caller passes a model call | opt-in shadow-mode judgment | `decision_rubric.drift.enabled`, `drift.model`, `drift.spend_ceiling_usd_per_day` | not captured (off by default; no call without a model and a ceiling; see `skills/sigma-loop/scripts/drift_judge.py`) |

## What stays local

- `.sdlc/`, its action log, timing store, and phase transcripts.
- `phase_report.py` reads `~/.claude/projects` and `~/.codex/sessions` locally when measuring phase context.

## Capture limits

- Python processes started with `-I`, `-E`, or `-S` skip `sitecustomize`.
- Non-Python programs such as `gh` and `git` are recorded when launched; their own connections are outside the Python audit hook.
