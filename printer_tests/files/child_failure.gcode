; EXPECTED FAILURE. Review virtual_sdcard.on_error_gcode before running:
; existing error cleanup may move the printer or change heater targets.
START NAME=sd_failure
    _GCO_TEST_PROBE LABEL=sd_failure MS=500 FAIL=1
    _GCO_TEST_PROBE LABEL=FORBIDDEN
END
_GCO_TEST_PROBE LABEL=sd_admitted MS=1500
_GCO_TEST_PROBE LABEL=FORBIDDEN
WAIT
