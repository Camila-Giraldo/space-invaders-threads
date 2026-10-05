"""Configuracion de pytest.

Anade la raiz del proyecto al `sys.path` para que `import config`, `import
estado`, etc. funcionen al ejecutar los tests, y fuerza el driver SDL de pygame
en modo sin ventana.
"""

import os
import pathlib
import sys

RAIZ = pathlib.Path(__file__).parent.resolve()
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
