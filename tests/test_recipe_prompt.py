"""The shared recipe-extraction prompt (FoodAssistant-zq7k, FoodAssistant-43m4).

_RECIPE_PROMPT lives in providers/gemini.py and is imported verbatim by the
openai, anthropic, and ollama providers, so one edit covers every device
provider. These pin the guidance the three fixes added and that the prompt still
documents its JSON contract and still formats (its JSON braces stay escaped).

The provider modules import their vendor SDKs at module load (google.generativeai,
anthropic), which are not installed in the pure-logic test env; the app itself
imports them lazily so this never matters at runtime. Stubbing the bare modules
lets the test read the prompt string without those SDKs present.
"""
import sys
import types

for _name in ("google", "google.generativeai"):
    sys.modules.setdefault(_name, types.ModuleType(_name))
sys.modules["google"].generativeai = sys.modules["google.generativeai"]
if "anthropic" not in sys.modules:
    _anthropic = types.ModuleType("anthropic")
    _anthropic.AsyncAnthropic = object
    sys.modules["anthropic"] = _anthropic

from app.providers.gemini import _RECIPE_PROMPT  # noqa: E402


def test_prompt_keeps_the_core_contract():
    # The documented JSON schema is intact.
    for field in ('"name"', '"description"', '"servings"', '"total_time"',
                  '"ingredients"', '"instructions"'):
        assert field in _RECIPE_PROMPT
    # And the faithful-transcription rule is not dropped.
    assert "do not invent" in _RECIPE_PROMPT.lower()


def test_prompt_has_section_grouping():
    # FIX 2 (zq7k): the grouped-ingredient form the model may return.
    assert '"section"' in _RECIPE_PROMPT
    assert '"items"' in _RECIPE_PROMPT


def test_prompt_has_ambiguity_and_spelling_guidance():
    # FIX 3 (43m4): prefer a plausible ingredient over a literal misread, and
    # spell each ingredient the same way throughout.
    lowered = _RECIPE_PROMPT.lower()
    assert "saffron threads" in lowered
    assert "ambiguous" in lowered
    assert "same way" in lowered


def test_prompt_still_formats():
    # The JSON braces stay doubled so .format only fills {source}.
    text = _RECIPE_PROMPT.format(source="photo")
    assert "photo" in text
    assert '"ingredients"' in text
    # {source} was the only field; no stray unescaped brace remains.
    assert "{source}" not in text


def test_all_device_providers_share_one_prompt():
    from app.providers import anthropic, gemini, ollama, openai
    assert openai._RECIPE_PROMPT is gemini._RECIPE_PROMPT
    assert anthropic._RECIPE_PROMPT is gemini._RECIPE_PROMPT
    assert ollama._RECIPE_PROMPT is gemini._RECIPE_PROMPT
