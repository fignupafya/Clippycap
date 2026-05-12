"""PyInstaller entry point -- launches the Clippycap CLI / desktop shell.

(A separate file so PyInstaller has a script to analyse; ``pathex`` in the .spec puts ``src/`` on
the path so ``clippycap`` imports.)
"""

from clippycap.shell.cli import main

raise SystemExit(main())
