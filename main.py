"""Application entry point required by buildozer.

buildozer expects a ``main.py`` at the project root. It only wires the package
onto ``sys.path`` and starts the UI, so the application logic stays importable
(and testable) as a normal package.
"""

import os
import sys

# On Android the packaged source lives beside this file.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def main() -> None:
    from pdf2mobi_android.ui import run

    run()


if __name__ == "__main__":
    main()
