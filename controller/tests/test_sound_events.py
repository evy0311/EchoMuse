import json
import re
from pathlib import Path

import pytest

import em_db as db


ROOT = Path(__file__).resolve().parent.parent.parent
ESPHOME = ROOT / "controller" / "em_esphome.py"
CONTROLLER = ROOT / "controller" / "em_controller.py"


@pytest.fixture()
def fresh_db(tmp_path):
    db.init(str(tmp_path / "sound-events.db"))
    db.register_new_device("garage", "192.0.2.10", "test")
    yield
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def event(sequence=1):
    return {
        "ts": 1_800_000_000.25,
        "kind": "smoke_alarm",
        "confidence": 0.82,
        "scores": {"Smoke detector, smoke alarm": 0.82, "Siren": 0.31},
        "mode": "shadow",
        "confirmations": 3,
        "cadence": "t3",
        "evidence": {"threshold": 0.5},
        "playback_active": False,
        "model": "yamnet_classifier.onnx",
        "runtime": "onnxruntime 1.19.2",
        "boot_id": "boot-a",
        "event_sequence": sequence,
    }


def test_migration_creates_deduplicated_sound_event_table(fresh_db):
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(sound_events)")}
    assert {"kind", "scores_json", "boot_id", "event_sequence"} <= cols
    assert db.record_sound_event("garage", event()) is True
    assert db.record_sound_event("garage", event()) is False
    assert len(db.get_sound_events("garage")) == 1


def test_round_trip_preserves_evidence_and_playback(fresh_db):
    item = event()
    item["kind"] = "possible_co_alarm"
    item["playback_active"] = True
    db.record_sound_event("garage", item)
    got = db.get_sound_events("garage")[0]
    assert got["kind"] == "possible_co_alarm"
    assert got["scores"]["Siren"] == pytest.approx(0.31)
    assert got["evidence"] == {"threshold": 0.5}
    assert got["playback_active"] is True
    json.dumps(got)  # API responses must remain directly serializable.


def test_sequence_may_repeat_after_reboot(fresh_db):
    first = event()
    second = event()
    second["boot_id"] = "boot-b"
    assert db.record_sound_event("garage", first)
    assert db.record_sound_event("garage", second)
    assert len(db.get_sound_events("garage")) == 2


def test_home_assistant_event_entity_covers_every_protocol_kind():
    src = ESPHOME.read_text()
    match = re.search(r"HAZARD_EVENT_TYPES\s*=\s*\[(.*?)\]", src, re.S)
    assert match, "hazardous-sound event types must be advertised to HA"
    advertised = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    assert advertised == {
        "smoke_alarm", "fire_alarm", "possible_co_alarm", "glass_break",
        "alarm_unknown",
    }
    assert 'object_id="hazardous_sound"' in src
    assert '_device_has("sound_events")' in src


def test_home_assistant_receives_shadow_and_on_incidents_with_confidence():
    controller = CONTROLLER.read_text()
    handler = controller.split("async def _handle_sound_event", 1)[1].split(
        "\n\nasync def ", 1
    )[0]
    assert "esphome.send_sound_event(device.device_id, kind, confidence)" in handler
    assert re.search(r'if\s+mode\s*==\s*["\']on["\']', handler) is None, (
        "shadow detections must reach HA too; the user decides whether to act"
    )

    esphome = ESPHOME.read_text()
    sender = esphome.split("def send_sound_event", 1)[1].split("\n\ndef ", 1)[0]
    confidence_send = sender.index("SensorStateResponse")
    event_send = sender.index("EventResponse")
    assert confidence_send < event_send, (
        "confidence must be current before HA receives the event trigger"
    )
