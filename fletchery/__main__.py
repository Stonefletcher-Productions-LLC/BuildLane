import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cli import main  # noqa: E402

sys.exit(main())
