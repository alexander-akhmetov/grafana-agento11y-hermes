# Changelog

## [0.5.0](https://github.com/alexander-akhmetov/grafana-agento11y-hermes/compare/0.4.0...0.5.0) - 2026-08-15

- Manage the project with uv and take the version from git tags

## [0.4.0](https://github.com/alexander-akhmetov/grafana-agento11y-hermes/compare/0.3.0...0.4.0) - 2026-06-07

- bump sigil-sdk to 0.8.0
- Send plugin User-Agent on generation export
- fix Grafana Cloud path
- add screenshot

## [0.3.0](https://github.com/alexander-akhmetov/grafana-agento11y-hermes/compare/0.2.0...0.3.0) - 2026-06-07

- Derive OTLP auth headers from Sigil creds when unset

## [0.2.0](https://github.com/alexander-akhmetov/grafana-agento11y-hermes/compare/0.1.0...0.2.0) - 2026-06-07

- Let the SDK derive tool-execution content capture
- Add SIGIL_HERMES_AGENT_VERSION
- sigil-sdk 0.5.0
- Rename project from hermes-plugin-sigil to sigil-hermes
- llms.txt: route Page B through the in-stack access-policies URL
- Make the install verify recipe actually work
- Change URL pattern for AI Observability
- Clarify Grafana AI Observability plugin instructions (#2)
- Update README to correct plugin description
- Add llms.txt
- hooks: scope recorder→assistant pairing to this turn

## [0.1.0] - 2026-05-01

- otel: drop SIGIL_OTEL_* schema, use standard OTEL_* envs
- config: adopt canonical SIGIL_* env-var schema
- hooks: move all tool work to post_tool_call
- hooks: bound generation span and duration to the LLM call window
- hooks: drop redundant set_result(input=...) at pre-hook time
- hooks: catch prep errors when closing pending generation recorders
- hooks: thread cfg.max_chars into _redact.safe_value
- redact: cap before materialization in safe_value
- initial commit
