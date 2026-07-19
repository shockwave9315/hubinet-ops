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

IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".pytest_tmp",
    ".mypy_cache",
    ".ruff_cache",
    ".tmp",
    "node_modules",
    "build",
    "dist",
    "cache",
    "runtime",
    "backups",
}


def yaml_paths(root: Path) -> list[Path]:
    candidates = list(root.rglob("*.yaml")) + list(root.rglob("*.yml"))
    return sorted(
        (
            path
            for path in candidates
            if not any(part.lower() in IGNORED_DIRECTORIES for part in path.relative_to(root).parts)
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    paths = yaml_paths(root)
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            yaml.load(handle, Loader=HomeAssistantLoader)
    print(f"YAML validation: {len(paths)} files parsed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
