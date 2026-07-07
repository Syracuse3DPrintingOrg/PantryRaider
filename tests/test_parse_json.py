"""parse_json_response — fenced/unfenced LLM JSON replies."""
import pytest

from app.providers.base import parse_json_response


def test_plain_json():
    assert parse_json_response('{"a": 1}') == {"a": 1}


def test_json_array():
    assert parse_json_response('[1, 2]') == [1, 2]


def test_fenced_json():
    assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}


def test_fenced_without_language():
    assert parse_json_response('```\n{"a": 1}\n```') == {"a": 1}


def test_surrounding_whitespace():
    assert parse_json_response('  \n{"a": 1}\n  ') == {"a": 1}


def test_invalid_raises():
    with pytest.raises(Exception):
        parse_json_response("I'm sorry, I can't do that.")
