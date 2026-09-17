; The caller has parked and released the previous material.
; Use M400 when earlier queued motion must physically finish before the fork.
M400
START NAME=filament_change
    T0
    ; T0 must remain pending until the MMU confirms the requested operation.
END
START NAME=nozzle_heating
    M109 S220
END
CLEAN_NOZZLE
; Cleaning may overlap only if it does not conflict with T0's device resources.
WAIT ON=filament_change,nozzle_heating
; Both routines succeeded. No implicit physical wait beyond each command's contract.
M117 Ready
