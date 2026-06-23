# siphon

Utility to scrape data from common social media apps.

## Storage conventions

Each scraper run uploads its screenshots to S3 under a per-run prefix of the
form `{scraper_name}_{timestamp}/`, e.g. `snapchat_2026-06-18_10-30-00/`.
This keeps every run's images grouped in their own "folder" in the bucket
rather than mixed together. `timestamp` matches the local run ID used for
the `screenshots/` and `logs/` directories, so a run's local files, S3
prefix, and DB records can all be correlated by that ID.

This convention applies to every scraper, not just Snapchat. New scrapers
should build their run prefix as `f"{scraper_name}_{run_id}"` and pass it
to `siphon.storage.upload_file` / `siphon.storage.upload_bytes`.
