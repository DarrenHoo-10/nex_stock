# -*- coding: utf-8 -*-
"""Direct HTTP data sources adopted from a-stock-data (docs/design/a-stock-data-adoption.md section 2.2).

Each module only requests and parses: it returns plain records, raises ``SourceError`` when the source
is broken (HTTP error, changed format) and returns an empty list when there is really nothing.
Storage, cadence and fallback order live in the calling services.
"""

from __future__ import annotations


class SourceError(RuntimeError):
    """The source could not be reached or its response changed format."""
