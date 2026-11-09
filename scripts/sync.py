#!/usr/bin/env python3
"""Build custom_components/openai_usage and tests/ from the pristine upstream copy.

upstream/openai_conversation/  official integration, as shipped in Home Assistant
upstream/tests/                its tests, from the home-assistant/core repository

This script copies them, renames the domain, applies the hooks listed in HOOKS
(every inserted line carries the marker "# openai_usage:"), merges our
translations and writes the manifest. Our own modules (usage.py, sensor.py,
prices.py, usage_flow.py, tests/test_usage.py, tests/ha_helpers.py) are never
touched.

Usage:
    scripts/sync.py           regenerate the files
    scripts/sync.py --check   fail if the committed files differ from a fresh build
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import filecmp
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = ROOT / "upstream"
COMPONENT = ROOT / "custom_components" / "openai_usage"
TESTS = ROOT / "tests"
MARK = "# openai_usage:"

UPSTREAM_DOMAIN = "openai_conversation"
DOMAIN = "openai_usage"
REPO = "https://github.com/m0s4ik/ha-openai-usage"

MANIFEST = {
    "domain": DOMAIN,
    "name": "OpenAI (with usage)",
    "codeowners": ["@m0s4ik"],
    "documentation": REPO,
    "issue_tracker": f"{REPO}/issues",
}
MANIFEST_DROP = ("quality_scale",)

# Files of ours that live next to the generated ones.
OWN_COMPONENT_FILES = {"usage.py", "sensor.py", "prices.py", "usage_flow.py"}
OWN_TEST_FILES = {"test_usage.py", "ha_helpers.py"}

IMPORT_USAGE = f"from . import usage  {MARK} usage tracking hooks"


@dataclass(frozen=True)
class Hook:
    """A text change in a generated file.

    kind: "replace" anchor with text, insert text "before"/"after" the anchor,
    or "append" text at the end of the file. The anchor must occur exactly once.
    """

    file: str
    kind: str
    anchor: str
    text: str


COMPONENT_HOOKS = [
    Hook(
        "const.py",
        "replace",
        f'DOMAIN = "{UPSTREAM_DOMAIN}"\n',
        f'DOMAIN = "{DOMAIN}"  {MARK} own domain, installable side by side\n',
    ),
    # __init__.py: sensor platform, tracker set up before the platforms.
    Hook("__init__.py", "before", "from .const import (\n", IMPORT_USAGE + "\n"),
    Hook(
        "__init__.py",
        "after",
        "PLATFORMS = (Platform.AI_TASK, Platform.CONVERSATION, Platform.STT, Platform.TTS)\n",
        f"PLATFORMS = (*PLATFORMS, Platform.SENSOR)  {MARK} usage sensors\n",
    ),
    Hook(
        "__init__.py",
        "before",
        "    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)\n",
        f"    usage.async_setup_tracker(hass, entry)  {MARK}\n",
    ),
    Hook(
        "__init__.py",
        "after",
        "    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)\n",
        f"    usage.async_unload_tracker(hass, entry)  {MARK}\n",
    ),
    # entity.py: Responses API stream (conversation and AI task data/image).
    Hook("entity.py", "before", "from .const import (\n", IMPORT_USAGE + "\n"),
    Hook(
        "entity.py",
        "after",
        "                stream = await client.responses.create(**model_args)\n",
        f"                stream = usage.wrap_response_stream(self, stream)  {MARK}\n",
    ),
    # ai_task.py: Decisions API (AI task evaluation).
    Hook("ai_task.py", "before", "from .const import (\n", IMPORT_USAGE + "\n"),
    Hook(
        "ai_task.py",
        "after",
        '            raise HomeAssistantError(f"Error evaluating OpenAI task: {err}") from err\n',
        f"        usage.async_record_decision(self, response)  {MARK}\n",
    ),
    # stt.py: transcription.
    Hook("stt.py", "before", "from .const import ", IMPORT_USAGE + "\n"),
    Hook(
        "stt.py",
        "before",
        "            if response.text:\n",
        "            usage.async_record_transcription(  " + MARK + "\n"
        "                self,  " + MARK + "\n"
        "                options.get(CONF_CHAT_MODEL, RECOMMENDED_STT_MODEL),  "
        + MARK
        + "\n"
        "                response,  " + MARK + "\n"
        "                metadata,  " + MARK + "\n"
        "                len(audio_bytes),  " + MARK + "\n"
        "            )  " + MARK + "\n",
    ),
    # tts.py: speech.
    Hook("tts.py", "before", "from .const import ", IMPORT_USAGE + "\n"),
    Hook(
        "tts.py",
        "before",
        "        return response_format, bytes(response_data)\n",
        f"        usage.async_record_speech(self, options[CONF_CHAT_MODEL], message)  {MARK}\n",
    ),
    # config_flow.py: options flow of the main entry (price overrides).
    Hook(
        "config_flow.py",
        "append",
        "",
        "\n\n"
        f"from . import usage_flow  # noqa: E402  {MARK} price options\n\n"
        f"usage_flow.attach(OpenAIConfigFlow)  {MARK}\n",
    ),
]

TEST_REPLACEMENTS = [
    (f"homeassistant.components.{UPSTREAM_DOMAIN}", f"custom_components.{DOMAIN}"),
    ("from tests.components.conversation import", "from .ha_helpers import"),
    ("from tests.components.tts.common import", "from .ha_helpers import"),
    (
        f'"{UPSTREAM_DOMAIN} integration not ready yet"',
        f'"{DOMAIN} integration not ready yet"',
    ),
    (f'components.add("{UPSTREAM_DOMAIN}")', f'components.add("{DOMAIN}")'),
]

TEST_HOOKS = [
    Hook(
        "test_init.py",
        "replace",
        "    assert len(devices) == 5  # Conversation, data, evaluation, STT, and TTS\n",
        f"    assert len(devices) == 6  {MARK} 5 subentry devices + the usage device\n",
    ),
    Hook(
        "conftest.py",
        "append",
        "",
        "\n\n"
        f'pytest_plugins = ["tests.ha_helpers"]  {MARK} helpers from HA core tests\n',
    ),
]


class HookError(Exception):
    """A hook anchor was not found exactly once."""


def apply_hook(text: str, hook: Hook) -> str:
    """Apply one hook to a file content."""
    if hook.kind == "append":
        return text.rstrip("\n") + "\n" + hook.text
    count = text.count(hook.anchor)
    if count != 1:
        raise HookError(
            f"{hook.file}: anchor found {count} times (expected 1): {hook.anchor!r}"
        )
    if hook.kind == "replace":
        return text.replace(hook.anchor, hook.text)
    if hook.kind == "before":
        index = text.index(hook.anchor)
        return text[:index] + hook.text + text[index:]
    if hook.kind == "after":
        index = text.index(hook.anchor) + len(hook.anchor)
        return text[:index] + hook.text + text[index:]
    raise HookError(f"unknown hook kind {hook.kind}")


def apply_hooks(directory: Path, hooks: list[Hook]) -> list[str]:
    """Apply hooks in a directory, return the errors."""
    errors = []
    for hook in hooks:
        path = directory / hook.file
        try:
            path.write_text(apply_hook(path.read_text(), hook))
        except (HookError, FileNotFoundError) as err:
            errors.append(str(err))
    return errors


def deep_merge(base: dict, extra: dict) -> dict:
    """Merge extra into base recursively."""
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def own_version() -> str:
    """Return our version from VERSION."""
    return (ROOT / "VERSION").read_text().strip()


def build_component(out: Path) -> list[str]:
    """Build the integration into out."""
    src = UPSTREAM / UPSTREAM_DOMAIN
    out.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.glob("*.py")):
        shutil.copy(path, out / path.name)

    manifest = json.loads((src / "manifest.json").read_text())
    manifest.update(MANIFEST)
    manifest["version"] = own_version()
    for key in MANIFEST_DROP:
        manifest.pop(key, None)
    # hassfest rejects exact pins in custom integrations for packages Home
    # Assistant itself pins (openai ships with core): same version, as a minimum.
    manifest["requirements"] = [
        req.replace("==", ">=", 1) for req in manifest.get("requirements", [])
    ]
    ordered = {"domain": manifest.pop("domain"), "name": manifest.pop("name")}
    ordered.update(sorted(manifest.items()))
    (out / "manifest.json").write_text(json.dumps(ordered, indent=2) + "\n")

    (out / "translations").mkdir(exist_ok=True)
    for path in sorted((src / "translations").glob("*.json")):
        data = json.loads(path.read_text())
        extra = ROOT / "scripts" / "translations" / path.name
        if extra.exists():
            deep_merge(data, json.loads(extra.read_text()))
        (out / "translations" / path.name).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        )
    return apply_hooks(out, COMPONENT_HOOKS)


def build_tests(out: Path) -> list[str]:
    """Build the tests into out."""
    src = UPSTREAM / "tests"
    out.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.glob("*.py")):
        text = path.read_text()
        for old, new in TEST_REPLACEMENTS:
            text = text.replace(old, new)
        (out / path.name).write_text(text)
    return apply_hooks(out, TEST_HOOKS)


def generated_files(directory: Path, own: set[str]) -> list[Path]:
    """List the generated files of a directory (relative paths)."""
    return sorted(
        path.relative_to(directory)
        for path in directory.rglob("*")
        if path.is_file()
        and path.name not in own
        and "__pycache__" not in path.parts
        and "snapshots" not in path.parts
    )


def main() -> int:
    """Run the script."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only compare")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_component = Path(tmp) / "component"
        tmp_tests = Path(tmp) / "tests"
        errors = build_component(tmp_component) + build_tests(tmp_tests)
        if errors:
            print("Hooks that could not be applied (update HOOKS in scripts/sync.py):")
            for error in errors:
                print(f"  - {error}")
            return 1

        pairs = [
            (tmp_component, COMPONENT, OWN_COMPONENT_FILES),
            (tmp_tests, TESTS, OWN_TEST_FILES),
        ]
        if args.check:
            different = []
            for fresh, committed, own in pairs:
                names = set(generated_files(fresh, own)) | set(
                    generated_files(committed, own) if committed.exists() else []
                )
                for name in sorted(names):
                    a, b = fresh / name, committed / name
                    if not (a.exists() and b.exists()) or not filecmp.cmp(
                        a, b, shallow=False
                    ):
                        different.append(str(committed.relative_to(ROOT) / name))
            if different:
                print("Out of date (run scripts/sync.py):")
                print("\n".join(f"  - {name}" for name in different))
                return 1
            print("Generated files are up to date.")
            return 0

        for fresh, committed, own in pairs:
            for name in generated_files(committed, own) if committed.exists() else []:
                (committed / name).unlink()
            for name in generated_files(fresh, own):
                (committed / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(fresh / name, committed / name)
        print("custom_components/openai_usage and tests/ regenerated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
