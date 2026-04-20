# Rules: Flask App (app.py)

Apply these rules when modifying `app.py` or adding new Flask routes.

## Route conventions
- Every route handler **must** have a docstring (enforced by fitness function).
- Routes return either `render_template(...)` or a JSON response via `jsonify(...)`.
- Never call `app.client.messages.create()` outside a try/except block — always catch `Exception`.
- On API error, pass the error message to the template as `error=str(e)`.

## Module-level client
- The Anthropic client is `app.client = Anthropic()` at module level (ADR-001).
- Do **not** create a new `Anthropic()` instance inside route handlers.
- In tests, patch `app.client` via `unittest.mock.patch` — never pass a real API key.

## Template variables
- Quote output → `quote` variable passed to `render_template`.
- Error state → `error` variable passed to `render_template`.
- Work input → read from `request.form.get("work", "").strip()`.

## Input validation
- Strip whitespace from all form inputs before use.
- Return early (render empty form) if stripped input is empty.
- Never trust `request.form` values directly in API calls without sanitisation.

## Error handling
- Wrap all `app.client.messages.create(...)` calls in `try/except Exception as e`.
- Log errors with `app.logger.error(...)` before returning error to template.
- HTTP 500 should never propagate to the user — always catch and render friendly error.

## Function size
- Route handlers must stay under 40 lines (enforced by fitness function).
- Extract helper functions if a handler grows large.
