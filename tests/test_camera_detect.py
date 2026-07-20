"""Tests for the detection-to-pop-up decision logic and the Reolink AI-state /
device-info parsing (FoodAssistant-akd0, FoodAssistant-qft4).

All of ``camera_detect`` is pure (no network, no settings), so these exercise
it directly against representative Reolink CGI reply shapes.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import camera_detect  # noqa: E402


# -- should_popup -------------------------------------------------------------

def test_should_popup_true_when_type_enabled():
    assert camera_detect.should_popup("person", ["person", "vehicle"]) is True


def test_should_popup_false_when_type_disabled():
    assert camera_detect.should_popup("animal", ["person", "vehicle"]) is False


def test_should_popup_case_insensitive():
    assert camera_detect.should_popup("Person", ["PERSON"]) is True


def test_should_popup_blank_type_never_pops():
    assert camera_detect.should_popup("", ["person"]) is False
    assert camera_detect.should_popup(None, ["person"]) is False


def test_should_popup_no_enabled_types_never_pops():
    assert camera_detect.should_popup("person", []) is False
    assert camera_detect.should_popup("person", None) is False


# -- reolink_ai_detections -----------------------------------------------------

def test_ai_detections_maps_categories():
    state = {"value": {
        "people": {"alarm_state": 1},
        "vehicle": {"alarm_state": 0},
        "dog_cat": {"alarm_state": 1},
        "face": {"alarm_state": 0},
    }}
    assert camera_detect.reolink_ai_detections(state) == ["animal", "person"]


def test_ai_detections_visitor_and_ring_map_to_visitor():
    assert camera_detect.reolink_ai_detections(
        {"value": {"visitor": {"alarm_state": 1}}}) == ["visitor"]
    assert camera_detect.reolink_ai_detections(
        {"value": {"ring": {"alarm_state": 1}}}) == ["visitor"]


def test_ai_detections_handles_batch_list_wrapper():
    state = [{"value": {"people": {"alarm_state": 1}}}]
    assert camera_detect.reolink_ai_detections(state) == ["person"]


def test_ai_detections_dedupes_person_face_and_people():
    state = {"value": {"people": {"alarm_state": 1}, "face": {"alarm_state": 1}}}
    assert camera_detect.reolink_ai_detections(state) == ["person"]


def test_ai_detections_empty_when_nothing_alarming():
    state = {"value": {"people": {"alarm_state": 0}, "vehicle": {"alarm_state": 0}}}
    assert camera_detect.reolink_ai_detections(state) == []


def test_ai_detections_malformed_reply_never_raises():
    assert camera_detect.reolink_ai_detections({}) == []
    assert camera_detect.reolink_ai_detections(None) == []
    assert camera_detect.reolink_ai_detections([]) == []
    assert camera_detect.reolink_ai_detections({"value": "not a dict"}) == []
    assert camera_detect.reolink_ai_detections({"value": {"people": "oops"}}) == []
    assert camera_detect.reolink_ai_detections(
        {"value": {"people": {"alarm_state": "bogus"}}}) == []


# -- reolink_popup_decision ----------------------------------------------------

def test_popup_decision_true_when_any_detected_type_enabled():
    state = {"value": {"people": {"alarm_state": 1}, "vehicle": {"alarm_state": 1}}}
    should, detected = camera_detect.reolink_popup_decision(state, ["person"])
    assert should is True
    assert detected == ["person", "vehicle"]


def test_popup_decision_false_when_detected_types_all_disabled():
    state = {"value": {"vehicle": {"alarm_state": 1}}}
    should, detected = camera_detect.reolink_popup_decision(state, ["person", "animal"])
    assert should is False
    assert detected == ["vehicle"]


def test_popup_decision_false_when_nothing_alarming():
    should, detected = camera_detect.reolink_popup_decision(
        {"value": {"people": {"alarm_state": 0}}}, ["person"])
    assert should is False
    assert detected == []


# -- reolink_capabilities (FoodAssistant-qft4) ---------------------------------

def test_capabilities_recognises_doorbell_by_type():
    dev_info = {"value": {"DevInfo": {"type": "wifi_doorbell", "model": "Reolink Video Doorbell WiFi"}}}
    caps = camera_detect.reolink_capabilities(dev_info)
    assert caps == {"is_doorbell": True, "two_way_talk": True}


def test_capabilities_recognises_doorbell_by_model_name():
    dev_info = {"DevInfo": {"type": "IPC", "model": "Reolink Doorbell PoE"}}
    caps = camera_detect.reolink_capabilities(dev_info)
    assert caps["is_doorbell"] is True
    assert caps["two_way_talk"] is True


def test_capabilities_plain_camera_not_a_doorbell():
    dev_info = {"value": {"DevInfo": {"type": "IPC", "model": "RLC-810A"}}}
    caps = camera_detect.reolink_capabilities(dev_info)
    assert caps == {"is_doorbell": False, "two_way_talk": False}


def test_capabilities_plain_camera_can_still_flag_audio_talk():
    dev_info = {"DevInfo": {"type": "IPC", "model": "RLC-823A", "audioTalk": 1}}
    caps = camera_detect.reolink_capabilities(dev_info)
    assert caps == {"is_doorbell": False, "two_way_talk": True}


def test_capabilities_handles_unwrapped_shape():
    dev_info = {"type": "doorbell", "model": "X"}
    caps = camera_detect.reolink_capabilities(dev_info)
    assert caps["is_doorbell"] is True


def test_capabilities_malformed_reply_never_raises():
    assert camera_detect.reolink_capabilities({}) == {"is_doorbell": False, "two_way_talk": False}
    assert camera_detect.reolink_capabilities(None) == {"is_doorbell": False, "two_way_talk": False}
    assert camera_detect.reolink_capabilities({"value": []}) == {"is_doorbell": False, "two_way_talk": False}
