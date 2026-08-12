"""The entry point CPython imports for us.

``site.py`` imports ``sitecustomize`` during startup, after ``PYTHONPATH`` is
already on ``sys.path``. That is the whole injection mechanism: no wrapper
script, no import hook, no change to the project under test.

Copied to ``sitecustomize.py`` in the scratch directory by jitter/runner.py.
"""

import os
import sys

try:
    import _jitter_shim

    _jitter_shim.install()
except Exception:  # pragma: no cover - perturbation must never break the program
    pass


def _chain():
    """Import any *other* sitecustomize the user already had installed.

    Shadowing a project's own startup hook would change its behaviour in a way
    that has nothing to do with any knob, and jitter would go on to blame that
    difference on entropy.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    saved_path = sys.path[:]
    ours = sys.modules.get("sitecustomize")
    try:
        sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != here]
        sys.modules.pop("sitecustomize", None)
        import sitecustomize  # noqa: F401
    except Exception:
        pass
    finally:
        sys.path[:] = saved_path
        # Put our own module back where the import machinery expects it.
        # CPython's loader ends with `sys.modules.pop(name)` once this body
        # returns, so leaving the key deleted raises KeyError -- which site.py
        # catches and prints as "Error in sitecustomize" on stderr, polluting
        # the output of every trial. It only shows up on machines that have no
        # other sitecustomize to chain to, which is why CI found it and no
        # laptop did.
        if ours is not None:
            sys.modules["sitecustomize"] = ours


_chain()
