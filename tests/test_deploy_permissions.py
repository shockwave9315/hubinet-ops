import re
from pathlib import Path


def test_upgrade_makes_venv_readable_by_service_user() -> None:
    script = Path("deploy/upgrade-0.2.1-from-pve.sh").read_text(encoding="utf-8")

    for marker in (
        "umask 022",
        "chown -R root:root",
        "chmod -R a+rX",
        "runuser -u hubinetops",
        "import paho.mqtt.client",
    ):
        assert marker in script


def test_embedded_python_validation_programs_compile() -> None:
    script = Path("deploy/upgrade-0.2.1-from-pve.sh").read_text(encoding="utf-8")
    programs = re.findall(
        r"/opt/hubinet-ops/\.venv/bin/python\s+-c\s+\\\n\s+'([^']+)'",
        script,
    )

    assert programs
    assert any("import paho.mqtt.client" in program for program in programs)
    for program in programs:
        compile(program, "<upgrade-python-c>", "exec")
