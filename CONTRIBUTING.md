# Contributing to Pantry Raider

Thanks for your interest in Pantry Raider. This guide covers how the project is
developed so your changes land smoothly.

## Where Work Is Tracked

Bug reports and feature requests are tracked in GitHub issues. If you are filing a bug or a feature idea, use the issue forms in this repository.


## Branching and Commits

Development happens directly on `main`. Keep changes focused and write plain,
descriptive commit messages.

Every commit bumps at least the patch version. `APP_VERSION` in
`service/app/config.py` is the single source of truth. A git hook handles the
bump automatically; install it once per clone:

```bash
scripts/install-git-hooks.sh
```

The hook auto-increments the patch number and skips rebases and merges. You do not need to edit the version by hand for an ordinary
change.

## Local Smoke Test

You can verify the app imports cleanly without Docker. Install the minimal
dependency set and import the app:

```bash
pip install fastapi jinja2 itsdangerous pillow python-multipart sqlalchemy pydantic-settings httpx
python -c "import sys; sys.path.insert(0,'service'); from app.main import app"
```

If that import succeeds, the app and its module graph load correctly.

## Running Tests

The test suite is pure logic (staple matching, tier classification, LLM JSON
parsing) and needs no network or Docker:

```bash
pip install pytest
python -m pytest tests/ -q
```

Please add or update tests when you change behavior that the suite covers.

## Writing Style

Documentation and user-facing text follow a couple of simple rules:

- Do not use em-dashes. Use plain hyphens, commas, or separate sentences.
- Do not use ASCII art. Keep Markdown and prose plain.

## Pull Requests

Open a pull request against `main` with a short description of the change and a
note on how you tested it. The patch version bumps automatically through the git
hook, so you do not need to change it in your PR.

## Code of Conduct

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md). By participating you
agree to uphold it.

## Security

Please do not report security vulnerabilities through public issues or pull
requests. See [SECURITY.md](SECURITY.md) for the private disclosure process.
