from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION = "gopay-payments-v4"

try:
    # Read from the installed distribution, whose version the release tag set at build
    # time. Nothing in the source tree carries a version to keep in step by hand.
    __version__ = version(DISTRIBUTION)
except PackageNotFoundError:  # pragma: no cover - running from a bare source checkout
    __version__ = "0.0.0"

# "v4" marks the API generation, not the package version: the v3 SDK (`gopay`) sends
# `GoPay Python <version>`, and the two version ranges overlap.
USER_AGENT = f"GoPay Python v4 {__version__}"
