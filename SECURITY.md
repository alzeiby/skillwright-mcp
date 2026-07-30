# Security

## Reporting a vulnerability

Do not publish exploit details in a public issue.

Use GitHub's private vulnerability reporting for this repository when available. If private reporting is unavailable, open a minimal issue stating that you need a private contact path without including sensitive details.

Include the affected version or commit, the impact, and the smallest reproduction you can provide safely.

## Security model

Skillwright runs locally with the permissions of the user who starts it. Generated skills can navigate websites and perform browser actions, so they should be reviewed like any other local automation.

Secret workflow inputs are resolved from `SKILLWRIGHT_SECRET_*` environment variables. Plaintext secret values are not stored in workflow definitions and are omitted from generated MCP tool schemas. They still exist in process memory long enough to be sent through the browser to the destination site.

Recorded non-secret browser input is application data and may be persisted in local authoring history. Do not enter credentials through `browser_fill`; use `browser_fill_secret` instead.
