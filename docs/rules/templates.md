# Rules: Jinja2 Templates (templates/)

Apply these rules when modifying HTML templates in `templates/`.

## Template variables
The main template `templates/index.html` receives these variables from `app.py`:
- `{{ quote }}` — the Claude-generated motivational quote (string or None)
- `{{ error }}` — error message string (None if no error)
- `{{ work }}` — the submitted work description (echoed back for UX)

Always guard optional variables: `{% if quote %}...{% endif %}`.

## CSS selectors (must remain stable — used by E2E tests)
These selectors are tested by Puppeteer in `tests/e2e/test_e2e.js`:
- `.quote-box` — container for the displayed quote
- `.error` — container for error messages
- `.work-label` — label element for the work input field
- `#work` — the textarea/input for work description
- `form[method="post"]` — the main submission form

**Do not rename or remove these selectors** — doing so will break E2E tests.

## Form structure
- Main form must use `method="post"` and `action="/"`.
- Input field for work description must have `name="work"` and `id="work"`.
- Submit button must be inside the form.

## Accessibility
- All form inputs must have a matching `<label for="...">` element.
- Use semantic HTML: `<main>`, `<header>`, `<section>` where appropriate.
- Provide alt text for any images.

## No inline scripts
- Do not add `<script>` tags with inline JavaScript.
- External JS only from trusted CDNs or local static files.
- Do not use `eval()` or `innerHTML` assignments in JavaScript.

## Escaping
- Jinja2 auto-escapes HTML by default — do not use `| safe` unless absolutely necessary.
- User-supplied data (e.g. `work` value echoed back) must never be marked `| safe`.
