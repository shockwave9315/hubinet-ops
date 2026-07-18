from pathlib import Path


def test_upgrade_makes_venv_readable_by_service_user() -> None:
    script = Path("deploy/upgrade-0.2.1-from-pve.sh").read_text(encoding="utf-8")

    assert "umask 022" in script
    assert "chown -R root:root /opt/hubinet-ops/.venv" in script
    assert "chmod -R a+rX /opt/hubinet-ops/.venv" in script
    assert "runuser -u hubinetops -- /opt/hubinet-ops/.venv/bin/python" in script
    assert "import paho.mqtt.client as mqtt" in script
