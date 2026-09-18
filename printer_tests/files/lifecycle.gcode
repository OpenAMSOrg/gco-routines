; Supervised lifecycle fixture. Without intervention, completes in ~10 s.
; Use a fresh run for PAUSE, CANCEL_PRINT, reset, and emergency-stop checks.
; Those operations invoke YOUR existing macros; they can move hardware.
START NAME=lifecycle
    _GCO_TEST_PROBE LABEL=lifecycle_pending MS=10000
    _GCO_TEST_PROBE LABEL=lifecycle_child_tail
END
WAIT ON=lifecycle
_GCO_TEST_PROBE LABEL=lifecycle_parent_tail
