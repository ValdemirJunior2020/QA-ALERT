# Slack alerts

QA ALERT keeps the Slack bot token only in `.env` as `SLACK_BOT_TOKEN`.

The alert destination is configurable in **Pixel Office -> Settings**:

- **Recipient name** is the human-readable label shown in the UI.
- **Slack member / channel ID** is the real destination used for delivery.
- `U...` or `W...` is treated as a person; QA ALERT opens a DM first.
- `C...`, `G...`, or `D...` is treated as a channel or existing conversation.

Use **Test Slack** before enabling live alerts.

The token is intentionally not editable or readable from the browser UI so publishing QA ALERT through Cloudflare does not expose the credential.
