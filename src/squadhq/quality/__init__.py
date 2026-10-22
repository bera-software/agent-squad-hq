"""Deterministic quality gates that run next to stack CI.

``coverage``  changed-lines coverage from lcov / Cobertura / JaCoCo reports
              and a ``git diff`` (the ``changed-coverage`` router job).
``gates``     which architecture and lint gates apply to a checkout
              (``squadhq.packs ci-plan --checkout``) and the quality kit the
              router hands to tokenless jobs.

Both modules use the standard library only, so the router can run them as
plain scripts (``python3 -I coverage.py ...``) from the quality-kit artifact
in jobs that have no hub checkout and no token.
"""
