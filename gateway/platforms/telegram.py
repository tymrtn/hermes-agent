"""Compatibility shim for legacy imports.

Telegram is now a platform plugin. This module aliases the plugin adapter module
so monkeypatches/imports through gateway.platforms.telegram affect the live
adapter globals too.
"""

import sys
from plugins.platforms.telegram import adapter as _adapter

sys.modules[__name__] = _adapter
