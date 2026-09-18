; Diagnostic only: start through virtual SD, not line-by-line console paste.
; Print completion must wait for sd_tail:end even without an explicit WAIT.
START NAME=sd_tail
    _GCO_TEST_PROBE LABEL=sd_tail MS=5000 VALUE=17
END
_GCO_TEST_PROBE LABEL=sd_foreground
