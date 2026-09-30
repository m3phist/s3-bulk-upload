import sys

if __package__ in (None, ""):
    # invoked as `python s3migrate ...` (path, not module) — fix imports so
    # it behaves the same as `python -m s3migrate ...`
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from s3migrate.cli import main
else:
    from .cli import main

sys.exit(main())
