from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


class HomeAssistantLoader(yaml.SafeLoader):
    pass


def _unknown_tag(loader: HomeAssistantLoader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


HomeAssistantLoader.add_multi_constructor("!", _unknown_tag)


def main() -> int:
    paths = sorted({*Path(".").rglob("*.yaml"), *Path(".").rglob("*.yml")})
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            yaml.load(handle, Loader=HomeAssistantLoader)
    print(f"YAML validation: {len(paths)} files parsed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
