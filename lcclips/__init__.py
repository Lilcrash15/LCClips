__version__ = "0.4.2"

try:  # written by packaging/build.py: identifies this exact build
    from ._build import BUILD
except ImportError:
    BUILD = "dev"
