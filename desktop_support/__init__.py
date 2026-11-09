"""Vendored desktop distribution contract v1, with no cross-service runtime dependency."""
from .settings import Installation, read_installation, installation_path, configured_data_dir, configured_backup_dir
from .updates import check_release
from .background import bind_background

__all__ = ['Installation', 'read_installation', 'installation_path', 'configured_data_dir',
           'configured_backup_dir', 'check_release', 'bind_background']
