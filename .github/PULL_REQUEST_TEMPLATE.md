# Pull Request

## Description

Briefly describe what this change does and why.

## Testing

Describe how you tested this change. For example:

- `python -c "import sys; sys.path.insert(0,'service'); from app.main import app"`
- `python -m pytest tests/ -q`
- Manual steps, if any.

## Notes

- The patch version bumps automatically through the git hook
  (`scripts/install-git-hooks.sh`). You do not need to bump `APP_VERSION`
  by hand.
- Please keep prose plain: no em-dashes and no ASCII art.
