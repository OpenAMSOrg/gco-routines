# Cooperating driver-facing API for gco-routines
#
# Provides hooks for device command handlers to supply structured replies
# and observable detail to the active command frame / routine.

from typing import Dict, Any

class DriverAPI:
    def __init__(self, runtime):
        self.runtime = runtime

    def set_reply(self, gcmd, mapping: Dict[str, Any]) -> None:
        """Set structured plain-data response for the active command frame."""
        if not isinstance(mapping, dict):
            raise ValueError("set_reply mapping must be a dict")
        self.runtime.set_command_reply(gcmd, mapping)

    def set_detail(self, gcmd, **fields) -> None:
        """Set observable detail (e.g. device, reason, progress) for current routine."""
        self.runtime.set_routine_detail(fields, gcmd=gcmd)
