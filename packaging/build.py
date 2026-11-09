"""Build the standalone app with process-local developer tools and no installation changes."""

import os
from pathlib import Path
import subprocess
import sys


def main():
    """Use the active native Python environment; leave global Xcode selection untouched."""
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    command_line_tools = Path('/Library/Developer/CommandLineTools')
    if sys.platform == 'darwin' and command_line_tools.is_dir():
        environment.setdefault('DEVELOPER_DIR', str(command_line_tools))
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', str(root / 'packaging/ag_tasks.spec')],
                   cwd=root, env=environment, check=True)
    if sys.platform == 'darwin':
        app = root / 'dist/Ag Tasks.app'
        subprocess.run(['codesign', '--force', '--sign', '-', str(app)], env=environment, check=True)
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], env=environment, check=True)


if __name__ == '__main__':
    main()
