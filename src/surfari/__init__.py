from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("surfari")
except PackageNotFoundError:
    __version__ = "0.0.0"  # fallback if running from source
