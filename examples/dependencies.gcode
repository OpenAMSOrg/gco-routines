START NAME=heating
    M109 S220
END
START NAME=filament
    T0
    WAIT ON=heating
    CLEAN_NOZZLE
END
WAIT ON=filament
; This includes the filament routine's own dependency and subsequent cleaning.
