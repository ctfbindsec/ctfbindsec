"""ctfquant — tier -1 live HTX funding-capture experiment.

This package implements a tightly-scoped slice of the v4 master prompt
package (see docs/master-prompt-package.md) for a $200 NAV experiment on
HTX USDT-M perpetuals. The deterministic Risk Engine and kill-switches
are the load-bearing safety layer; the LLM does not appear on the
hot path.
"""

__version__ = "0.0.1"
