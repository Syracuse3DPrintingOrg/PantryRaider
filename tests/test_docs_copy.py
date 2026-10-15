"""User-facing copy that points people somewhere has to point at something real.

The release notes, the docs and the app's own messages send people to Settings
sections, log commands and example values. When one of those goes stale (a
renamed Settings section, a version pin that would now downgrade a server), the
reader is left looking for something that is not there. These checks pin the
copy to what the app actually shows.

Run: python -m pytest tests/test_docs_copy.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _release_section(version: str) -> str:
    text = _read("CHANGELOG.md")
    start = text.index(f"## [{version}]")
    end = text.index("\n## [", start + 1)
    return text[start:end]


def _entry_titles(section: str) -> list[str]:
    return re.findall(r"^- \*\*(.+?)\*\*", section, flags=re.MULTILINE)


# --- 0.21.0 release notes ---------------------------------------------------

def test_release_notes_list_each_entry_once():
    titles = _entry_titles(_release_section("0.21.0"))
    dupes = sorted({t for t in titles if titles.count(t) > 1})
    assert not dupes, f"entries listed twice in 0.21.0: {dupes}"


def test_release_notes_describe_model_retirement_once():
    section = _release_section("0.21.0")
    assert section.count("retire old models") == 1
    assert "A retired AI model is replaced on the day" not in section
    for model in ("gpt-4.1-nano", "October 23, 2026", "January 14, 2027",
                  "Gemini 2.0 Flash-Lite", "Claude Haiku 4.5"):
        assert model in section, model


def test_release_notes_have_a_security_section():
    section = _release_section("0.21.0")
    assert "\n### Security\n" in section
    security = section.split("\n### Security\n", 1)[1]
    assert "Camera logins no longer reach the browser." in security
    fixed = section.split("\n### Fixed\n", 1)[1].split("\n### Security\n", 1)[0]
    assert "Camera logins no longer reach the browser." not in fixed


def test_release_notes_give_the_setup_code_command():
    section = _release_section("0.21.0")
    assert 'sudo docker logs foodassistant-service 2>&1 | grep "setup code"' in section
    assert "followed by the name of the Pantry Raider container" not in section


def test_release_notes_avoid_builder_wording():
    section = _release_section("0.21.0")
    assert "the new name for the other option" not in section
    assert "a missing image never starts a slow build" not in section


def test_setup_code_page_names_the_log_command():
    import sys
    sys.path.insert(0, str(_ROOT / "service"))
    from app.services import access
    where = access.SETUP_CODE_WHERE
    assert "foodassistant-service" in where
    assert "Unraid" in where
    assert "followed by the name" not in where
    first_run = _read("docs/first-run.md")
    assert 'sudo docker logs foodassistant-service 2>&1 | grep "setup code"' in first_run


# --- Settings section names -------------------------------------------------

# Names the Settings menu no longer has, mapped to where the thing lives now.
# "Settings, Devices & Services" is Home Assistant's own menu and stays.
_STALE_SECTIONS = re.compile(
    r"Settings(?:,| >) (?:Devices(?! &)|AI\b|Remote Access|Navigation\b"
    r"|Backups?\b(?! &)|Security & Access|Security > Backup|Personalization)")

_COPY_TREES = [("service/app", (".py", ".html", ".js")), ("docs", (".md",)),
               ("streamdeck", (".py", ".md"))]
_COPY_FILES = ["README.md", "esphome/README.md", "esphome/cub-custom.example.yaml",
               "esphome/cub-tdisplay-ble.yaml"]


def _copy_paths():
    for rel in _COPY_FILES:
        yield _ROOT / rel
    for tree, exts in _COPY_TREES:
        for path in sorted((_ROOT / tree).rglob("*")):
            if path.suffix not in exts or "design" in path.parts or "vendor" in path.parts:
                continue
            yield path


def test_copy_names_settings_sections_that_exist():
    hits = []
    for path in _copy_paths():
        text = path.read_text(encoding="utf-8")
        # Copy can wrap across lines, so match on whitespace-joined text.
        flat = re.sub(r"\s+", " ", text)
        for m in _STALE_SECTIONS.finditer(flat):
            hits.append(f"{path.relative_to(_ROOT)}: {m.group(0)}")
    assert not hits, "stale Settings names:\n" + "\n".join(hits)


def test_stale_pattern_catches_the_old_names():
    for old in ("Settings, Devices.", "Settings, AI.", "Settings, Remote Access",
                "Settings, Navigation", "Settings, Backups)", "Settings > Backup.",
                "Settings, Security & Access"):
        assert _STALE_SECTIONS.search(old), old
    for ok in ("Settings, Devices & Services", "Settings, Backups & Updates",
               "Settings, Scanning & AI", "Settings, Bandit Remotes"):
        assert not _STALE_SECTIONS.search(ok), ok


# --- Version pin examples ---------------------------------------------------

def test_pin_examples_do_not_downgrade_a_current_server():
    import sys
    sys.path.insert(0, str(_ROOT / "service"))
    from app.config import APP_VERSION
    current = tuple(int(x) for x in APP_VERSION.split("."))[:2]
    for rel in ("README.md", "docs/privacy.md", "docs/what-needs-internet.md"):
        text = _read(rel)
        pins = re.findall(r"PANTRYRAIDER_TAG=(\d+)\.(\d+)", text)
        assert pins, rel
        for major, minor in pins:
            assert (int(major), int(minor)) >= (0, 21), (rel, major, minor)
            assert (int(major), int(minor)) <= current, (rel, major, minor)
        assert "older release" in text, rel


# --- GitHub issue forms -----------------------------------------------------

def test_issue_forms_link_to_pages_that_exist_and_ask_the_right_things():
    import yaml
    config = yaml.safe_load(_read(".github/ISSUE_TEMPLATE/config.yml"))
    urls = [link["url"] for link in config["contact_links"]]
    # Discussions are off on the public repo, so that link was a 404.
    assert not any("/discussions" in u for u in urls)

    form = yaml.safe_load(_read(".github/ISSUE_TEMPLATE/bug_report.yml"))
    fields = {item.get("id"): item for item in form["body"] if item.get("id")}
    assert fields["version"]["attributes"]["placeholder"] == "0.21.0"
    assert "Backups & Updates" in fields["version"]["attributes"]["description"]
    installs = fields["install_type"]["attributes"]["options"]
    for kind in ("Docker Compose", "Unraid", "Home Assistant add-on",
                 "Raspberry Pi SD image", "install.sh"):
        assert kind in installs, kind
    assert any(o.startswith("Bandit") for o in installs)
    profiles = " ".join(o["label"] for o in fields["profiles"]["attributes"]["options"])
    for profile in ("with-grocy", "with-mealie", "with-ollama", "with-printing", "with-beszel"):
        assert profile in profiles, profile
    assert "setup code" in fields["logs"]["attributes"]["description"]


# --- README status and update notes -----------------------------------------

def test_readme_matches_the_current_release():
    import sys
    sys.path.insert(0, str(_ROOT / "service"))
    from app.config import APP_VERSION, Settings
    readme = _read("README.md")
    minor = ".".join(APP_VERSION.split(".")[:2])
    assert f"**Public beta ({minor}).**" in readme
    default = Settings.model_fields["gemini_model"].default
    pretty = default.replace("gemini-", "Gemini ").replace("-flash", " Flash")
    assert f"The default cloud model is {pretty}" in readme


def test_readme_update_section_leads_with_the_install_sh_step():
    readme = _read("README.md")
    updating = readme.split("\n## Updating\n", 1)[1]
    head = updating[:600]
    assert "sudo foodassistant-update" in head and "before 0.21" in head
    assert "docs/platforms.md#going-back-to-an-earlier-version" in updating


def test_downgrade_guide_matches_the_updater():
    guide = _read("docs/platforms.md")
    assert "### Going back to an earlier version" in guide
    updater = _read("scripts/image-build/foodassistant-update")
    pin = re.search(r'PINNED_VERSION_FILE="\$\{PINNED_VERSION_FILE:-([^}]+)\}"', updater).group(1)
    assert pin in guide
    assert "sudo foodassistant-update" in guide


# --- What leaves the install ------------------------------------------------

def test_privacy_docs_match_what_runs_by_default():
    import sys
    sys.path.insert(0, str(_ROOT / "service"))
    from app.config import Settings
    fields = Settings.model_fields
    privacy = " ".join(_read("docs/privacy.md").split())
    internet = " ".join(_read("docs/what-needs-internet.md").split())

    # Automatic updates run every six hours on their own, whatever the
    # update-check switch says, so the docs must not tie the two together.
    assert fields["auto_update"].default is True
    assert "6 * 3600" in _read("service/app/main.py")
    assert "every six hours" in privacy and "every six hours" in internet
    assert "Turn it off and the app itself stops contacting GitHub" not in privacy
    assert "if you enable auto-update" not in internet

    # The community table download is on by default and has its own row.
    assert fields["use_community_expiry"].default is True
    assert "| Community shelf-life estimates |" in internet
    # TheMealDB is the default recipe source.
    assert fields["recipe_source"].default == "themealdb"
    assert "TheMealDB is the default source" in internet
    assert "The only things it contacts are the specific services you configure" not in internet

    # Saving a recipe parses its ingredients with the AI, and the Gemini free tier note.
    assert "ingredient lines of each recipe you save" in privacy
    assert "free Gemini tier" in privacy


# --- Third-party licence texts ----------------------------------------------

def test_zebra_ppd_ships_with_its_gpl2_text():
    copying = _read("docker/cups/COPYING")
    assert "GNU GENERAL PUBLIC LICENSE" in copying and "Version 2, June 1991" in copying
    assert "END OF TERMS AND CONDITIONS" in copying
    notices = " ".join(_read("THIRD_PARTY_NOTICES.md").split())
    assert "docker/cups/COPYING" in notices
    assert "fetches this file at start" not in notices
    assert "ships inside the app image" in notices
    dockerfile = _read("service/Dockerfile")
    assert "COPY docker/cups/zebra-zpl.ppd /usr/share/ppd/foodassistant/zebra-zpl.ppd" in dockerfile
    assert "COPY docker/cups/COPYING /usr/share/ppd/foodassistant/COPYING" in dockerfile


# --- Placeholders, Forager README, compose header ---------------------------

def test_label_placeholder_names_no_real_person():
    pane = _read("service/app/templates/setup/_pane_printing.html")
    assert "Dan's kitchen" not in pane
    assert "e.g. Return to the Smith kitchen" in pane


def test_forager_readme_matches_the_plan_table():
    readme = " ".join(_read("cloud/README.md").split())
    config = _read("cloud/app/config.py")
    trial = int(re.search(r'"trial": ([\d_]+)', config).group(1).replace("_", ""))
    basic = int(re.search(r'"basic": ([\d_]+)', config).group(1).replace("_", ""))
    days = int(re.search(r"TRIAL_DAYS = (\d+)", config).group(1))
    assert f"{days}-day free trial of {trial:,} AI tokens" in readme
    assert f"Cloud Basic includes {basic:,} tokens" in readme
    assert "Schema is created with `create_all` at startup" not in readme
    assert "Alembic" in readme


def test_prod_compose_header_matches_the_readme_install():
    head = _read("docker-compose.prod.yml").split("\nservices:", 1)[0]
    assert "docker compose --profile with-grocy up -d" in head
    assert "docker/grocy-init/10-repair-auth-class.sh" in head
    assert "chmod 755 docker/grocy-init/10-repair-auth-class.sh" in head
    assert "0.19.6" not in head
