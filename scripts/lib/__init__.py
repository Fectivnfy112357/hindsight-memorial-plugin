"""hindsight-memorial: client-side pollution cleanup library.

This module is kept for backward compatibility with code that imports from
`lib.*`. New code should import from `hindsight_memorial.*` instead.
"""

import sys
from pathlib import Path

# Re-export from the canonical package
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from hindsight_memorial import (  # noqa: E402, F401
    CurateReport,
    CurateResult,
    HindsightAPIError,
    HindsightClient,
    MemorialConfig,
    SUPERSEDED_SCHEMA,
    build_query,
    curate_many,
    curate_memory,
    extract_superseded_ids,
    load_config,
    resolve_bank_id,
)