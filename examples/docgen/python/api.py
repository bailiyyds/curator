"""Example API module."""

from .util import normalize


class Worker:
    """Convert input values into normalized display labels."""

    @staticmethod
    def run(value: int = 2) -> str:
        """Return a label for an integer input.

        Args:
            value: Input to normalize.

        Returns:
            A normalized label.
        """
        return normalize(value)
