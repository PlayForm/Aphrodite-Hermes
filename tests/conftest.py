import sys, os

# Add the monorepo root so plugins.aphrodite._marker can do from .._core
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
