# US-003.1: submission-schema TDD red phase

These tests describe the first submission-schema behavior for US-003.1, tracing
to FR-001/FR-002 and the relevant validation behavior in AC-002. Production
application files are unchanged.

The intended public interface is `app.schemas.ScanSubmission`, a Pydantic model
with `model_validate`, `model_dump`, and `model_dump_json`. Tests exercise public
input/output behavior rather than private validators or model configuration.

`fixtures/contracts/v1/scan-submission.json` versions the test request examples.
It covers HTTP/HTTPS ZAP requests; missing fields; unsupported tools; malformed,
empty, non-string, or non-HTTP URLs; invalid parameter objects; and unknown root
and nested fields, including an untrusted ownership field.

Unit tests check typed validation, field-level error details, and JSON output.
Integration tests mount that same model on a test-only FastAPI route, call it
directly through ASGI, and verify invalid input returns structured HTTP 422 without
calling the handler. A positive case verifies that valid input reaches the handler
as a typed model. The handler spy records admission only; it does not model a
database or implement the production submission endpoint.

## Run

Use the pinned application image built for local development. No running services,
AWS credentials, database, network, pytest, or additional packages are required:

```sh
docker build -f docker/api/Dockerfile -t elasticscan-api:local .
docker run --rm --network none --read-only --mount "type=bind,src=$PWD,dst=/workspace,readonly" --workdir /workspace --entrypoint python elasticscan-api:local -B -m unittest discover -s tests -t . -p 'test_*.py' -v
```

The bind mount runs the current source and tests. The runner uses Python's standard
`unittest` library and the application's existing locked FastAPI/Pydantic packages.

The expected initial red result is five assertion failures stating that
`app.schemas` must expose `ScanSubmission`. Discovery and imports must succeed;
a missing test dependency, collection error, or external service failure is not
the intended red result. No test is skipped or marked as an expected failure.

The fixture contract version identifies the test examples; it does not add a
version field to the public submission JSON or establish the other cross-component
contracts mentioned in the story's sequencing note.
