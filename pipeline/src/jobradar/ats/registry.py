"""Look up the adapter for a board's ATS.

Kept out of `jobradar/ats/__init__.py` on purpose: the ATS modules import
`jobradar.ats.base`, and a package `__init__` that imports them back would make the
import order fragile.
"""

from jobradar.ats import ashby, greenhouse, lever
from jobradar.ats.base import AtsAdapter

ADAPTERS: dict[str, AtsAdapter] = {
    "greenhouse": AtsAdapter(board_url=greenhouse.board_url, parse_board=greenhouse.parse_board),
    "ashby": AtsAdapter(board_url=ashby.board_url, parse_board=ashby.parse_board),
    "lever": AtsAdapter(board_url=lever.board_url, parse_board=lever.parse_board),
}


def adapter_for(ats: str) -> AtsAdapter:
    """Raise KeyError for an ATS we don't support; the boards CHECK constraint makes that a bug."""
    return ADAPTERS[ats]
