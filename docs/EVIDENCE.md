# Alert evidence clips

QA ALERT keeps the full call audio only temporarily.

When a serious finding is supported by a timestamped transcript segment, the evidence worker should create a short retained clip **before** deleting the full source audio.

Default evidence window:

- 8 seconds before the finding
- 8 seconds after the finding

Both values are adjustable in **Pixel Office -> Settings**.

Each alert case stores:

- evidence start timestamp
- evidence end timestamp
- matching transcript excerpt
- evidence clip path
- finding/severity metadata

Suggested filename:

`evidence_<call-id>_<start>-<end>.mp3`

After the transcript, QA result and any required evidence clip are safely written, the full temporary recording can be permanently deleted.
