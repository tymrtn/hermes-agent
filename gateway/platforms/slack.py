"""Compatibility shim for legacy imports.

Slack is now a platform plugin. This module aliases the plugin adapter module
so gateway.platforms.slack imports affect the live adapter globals too.
"""

import sys
from plugins.platforms.slack import adapter as _adapter

sys.modules[__name__] = _adapter
