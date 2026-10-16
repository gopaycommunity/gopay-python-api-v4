"""Regenerate the TypedDict models from the "next spec".

The next spec is the upcoming version of the GoPay Payments API spec, ahead of what
api-docs.gopay.com publishes. Run:

    SPEC_SOURCE='../gopay-payments-api-v4/spec/en/payments.yaml' uv run scripts/codegen.py

SPEC_SOURCE is required and may be an https:// URL or a local file path. The two supported ways to
obtain the next spec are documented in CLAUDE.md. The source is deliberately not hardcoded:
this repo is mirrored to the public gopaycommunity organisation, so a URL baked in here would
be published with it.

The generator (datamodel-code-generator) is pinned below and run through `uvx`, so it never
becomes a dependency of the package. It needs `uv` on PATH.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDORED_SPEC = ROOT / "spec" / "payments.yaml"
STAGED_SPEC = ROOT / "spec" / ".payments.yaml.tmp"
TMP_DIR = ROOT / ".codegen-tmp"
OUTPUT = ROOT / "src" / "gopay_payments" / "_generated.py"

GENERATOR = "datamodel-code-generator[ruff]==0.83.0"

# The public reference stamped into the generated file. Not wherever the bytes came from:
# a consumer reading the header must be able to open the link.
STAMP_SPEC_URL = "https://api-docs.gopay.com/spec/en/payments.yaml"

# Python 3.9 has no NotRequired / TypeAlias in `typing`, and 3.9 is the supported floor.
# The newest generator no longer targets 3.9 directly, so it targets 3.10 without the `|`
# union operator and takes every typing construct 3.9 lacks from typing_extensions.
IMPORT_OVERRIDES = (
    '{"TypedDict":"typing_extensions","NotRequired":"typing_extensions",'
    '"Required":"typing_extensions","Literal":"typing_extensions",'
    '"TypeAlias":"typing_extensions"}'
)

# One servers list item: the "- url:" line plus its indented continuation lines. Same
# pattern as gopay-php-api-v4 and gp-gw-js-sdk, so all three SDKs strip the same shapes.
MOCK_SERVER = re.compile(
    r"\n([ \t]*)- url:[ \t]*['\"]?[^\n'\"]*payments-api-mock[^\n'\"]*['\"]?[^\n]*\n"
    r"(?:\1[ \t]+[^\n]*\n)*",
)

USAGE = """\
codegen: no spec source given.

Set SPEC_SOURCE to the next spec. It may be an https:// URL or a local path:

  SPEC_SOURCE='https://…/spec/en/payments.yaml' uv run scripts/codegen.py   # internal feed
  SPEC_SOURCE='../gopay-payments-api-v4/spec/en/payments.yaml' uv run scripts/codegen.py  # sideload

See CLAUDE.md ("Models — generated") for both sources.
"""


def fail(message: str) -> None:
    print(f"codegen: {message}", file=sys.stderr)
    sys.exit(1)


def stage_spec(source: str) -> None:
    if source.startswith("http://"):
        # The spec is turned into code committed here: it must not travel unauthenticated.
        fail("SPEC_SOURCE must use https://, not http://.")
    if source.startswith("https://"):
        print("→ Fetching the next spec ...")
        with urllib.request.urlopen(source, timeout=60) as response:
            STAGED_SPEC.write_bytes(response.read())
        return

    path = Path(source).expanduser()
    if not path.is_file():
        fail(f"spec source not found: {source}")
    # spec/payments.yaml is what this script writes, not something it reads. Regenerating
    # from it would roll the models back to whatever it already held, silently dropping
    # anything added upstream since the last refresh.
    if path.resolve() == VENDORED_SPEC.resolve():
        fail(
            "spec/payments.yaml is the vendored output of this script, not a spec source.\n"
            "Regenerating from it would roll the models back to whatever it already held.\n"
            "Pass the next spec instead — see CLAUDE.md."
        )
    print(f"→ Copying the next spec from {source} ...")
    shutil.copyfile(path, STAGED_SPEC)


def strip_mock_server() -> None:
    # The pre-release feed injects a mock server as the first servers entry. The published
    # spec lists only Sandbox and Production, and the vendored copy must match it.
    text = STAGED_SPEC.read_text(encoding="utf-8")
    text, count = MOCK_SERVER.subn("\n", text)
    STAGED_SPEC.write_text(text, encoding="utf-8")
    print(f"   removed {count} mock server entr{'y' if count == 1 else 'ies'}")
    # Fail closed: a silent no-op here is exactly how a mock host would reach the repo.
    if "payments-api-mock" in text:
        fail(
            "payments-api-mock is still present after stripping.\n"
            "The feed changed the shape of this entry — update MOCK_SERVER in scripts/codegen.py."
        )


def generate() -> Path:
    TMP_DIR.mkdir(exist_ok=True)
    out = TMP_DIR / "_generated.py"
    out.unlink(missing_ok=True)
    print("→ Generating TypedDict models ...")
    subprocess.run(
        [
            "uvx",
            "--quiet",
            "--from",
            GENERATOR,
            "datamodel-codegen",
            "--input",
            str(STAGED_SPEC),
            "--input-file-type",
            "openapi",
            "--output",
            str(out),
            "--output-model-type",
            "typing.TypedDict",
            "--target-python-version",
            "3.10",
            "--no-use-union-operator",
            "--use-standard-collections",
            "--enum-field-as-literal",
            "all",
            "--use-schema-description",
            "--use-field-description",
            "--no-use-closed-typed-dict",
            "--disable-timestamp",
            "--disable-future-imports",
            "--formatters",
            "ruff-format",
            "--import-overrides",
            IMPORT_OVERRIDES,
        ],
        check=True,
    )
    # The generator can exit 0 having written nothing usable. Check before anything in
    # the source tree is touched, not after.
    if not out.is_file() or "class PaymentDetails(TypedDict)" not in out.read_text("utf-8"):
        fail("generator produced no usable output — leaving the models untouched.")
    return out


def stamp(generated: Path) -> str:
    body = generated.read_text(encoding="utf-8")
    # Drop the generator's own banner, which names the temporary file it read.
    body = re.sub(r"\A# generated by datamodel-codegen:\n#   filename:[^\n]*\n\n?", "", body)
    header = (
        f'"""Models generated from {STAMP_SPEC_URL}.\n\n'
        "Do not edit this file — report issues against the upstream spec, then rerun\n"
        "scripts/codegen.py.\n"
        '"""\n\n'
    )
    return header + body


def main() -> None:
    source = os.environ.get("SPEC_SOURCE", "").strip()
    if not source:
        print(USAGE, file=sys.stderr, end="")
        sys.exit(1)
    try:
        stage_spec(source)
        strip_mock_server()
        generated = generate()
        text = stamp(generated)
        # Only now does anything in the tree change: everything above could still abort.
        OUTPUT.write_text(text, encoding="utf-8")
        STAGED_SPEC.replace(VENDORED_SPEC)
    finally:
        STAGED_SPEC.unlink(missing_ok=True)
        shutil.rmtree(TMP_DIR, ignore_errors=True)
    print(f"✓ Codegen complete → {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
