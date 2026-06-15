"""thinkweave — Obsidian-native universal memory layer."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("thinkweave")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0+unknown"
