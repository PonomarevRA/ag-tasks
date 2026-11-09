"""Installer precedence and bounded close/tray integration, with no user configuration writes."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import sys
import pytest
import desktop_support.settings as settings
from ag_tasks import native_shutdown as shutdown, shutdown_platforms as platforms


class Events(list):
    """Minimal subscription collection without a native GUI session."""
    def __iadd__(self, callback): self.append(callback); return self
    def __isub__(self, callback): self.remove(callback); return self


def locator(monkeypatch, tmp_path):
    """Point the real shared parser at one disposable install manifest."""
    file=tmp_path/'installation.json'
    monkeypatch.setattr(settings,'installation_path',lambda *args,**kwargs:file)
    value={'schemaVersion':'ag-desktop/install-v1','appId':'ag-tasks','dataDirectory':str((tmp_path/'data').resolve()),
           'backupDirectory':str((tmp_path/'backups').resolve()),'applicationPath':str((tmp_path/'Applications/App.app').resolve()),
           'closeToTray':True,'launchAtLogin':False,'preferredPort':51235}
    file.write_text(json.dumps(value))
    return file,value


def test_locator_env_and_cli_precedence_and_explicit_port(monkeypatch,tmp_path):
    """Installer selection is used only below explicit service environment and CLI arguments."""
    from ag_tasks.app import default_directory
    from ag_tasks import __main__ as entry,desktop
    file,value=locator(monkeypatch,tmp_path)
    monkeypatch.delenv('AG_TASKS_DATA_DIR',raising=False)
    assert default_directory()==Path(value['dataDirectory'])
    explicit=tmp_path/'override';monkeypatch.setenv('AG_TASKS_DATA_DIR',str(explicit))
    assert default_directory()==explicit
    calls=[];monkeypatch.setattr(desktop,'run_desktop',lambda *args,**kwargs:calls.append((args,kwargs)))
    monkeypatch.setattr(sys,'argv',['application','--desktop','--data-dir',str(tmp_path/'cli'),'--port','0'])
    entry.main();assert calls[-1][0][1:]==(tmp_path/'cli',0)
    monkeypatch.setattr(sys,'argv',['application','--desktop'])
    entry.main();assert calls[-1][0][1:]==(explicit,51235)
    file.write_text('{bad json')
    assert default_directory()==explicit
    monkeypatch.delenv('AG_TASKS_DATA_DIR')
    with pytest.raises(ValueError):default_directory()


def test_frozen_logging_cli_precedes_installer_and_environment(monkeypatch,tmp_path):
    """Startup logs must never be opened in a default user directory after an explicit override."""
    file,value=locator(monkeypatch,tmp_path)
    monkeypatch.delenv('AG_TASKS_DATA_DIR',raising=False)
    spec=importlib.util.spec_from_file_location('isolated_entry',Path(__file__).parents[1]/'packaging/entrypoint.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert module.logging_directory([],{})==Path(value['dataDirectory'])
    assert module.logging_directory(['--data-dir',str(tmp_path/'cli')],{'AG_TASKS_DATA_DIR':str(tmp_path/'env')})==tmp_path/'cli'


@pytest.mark.parametrize('system_ending,hidden,throws',[(False,True,False),(False,False,False),(True,True,False),(False,False,True)])
def test_tray_close_is_optional_and_explicit_quit_never_hides(monkeypatch,system_ending,hidden,throws):
    """Ready tray can veto user close only; failed tray/explicit Quit/OS end retain one bounded drain."""
    controllers=[];calls=[]
    class Controller:
        """Observe dispatch decisions without arming a process-exit watchdog."""
        def __init__(self,*args,**kwargs):
            self.finished=threading.Event();self.started=False;self.system_ending=system_ending;controllers.append(self)
        def request(self):self.started=True;calls.append('quit')
    def hide():
        calls.append('hide')
        if throws:raise RuntimeError('tray unavailable')
        return hidden
    monkeypatch.setattr(shutdown,'Shutdown',Controller)
    monkeypatch.setattr(shutdown,'SignalPipe',lambda request:SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(platforms,'install_platform',lambda *args:None)
    monkeypatch.setattr(sys,'platform','linux')
    window=SimpleNamespace(events=SimpleNamespace(closing=Events(),before_show=Events()),destroy=lambda:None)
    restore=shutdown.bind_native_shutdown(window,lambda:True,hide_on_close=hide)
    try:
        result=window.events.closing[0]()
        if system_ending:
            assert result is None and calls==['quit']
        elif hidden and not throws:
            assert result is False and calls==['hide'] and not controllers[0].started
            restore.request_quit();assert controllers[0].started
            count=calls.count('hide');window.events.closing[0]();assert calls.count('hide')==count
        else:assert result is False and calls==['hide','quit']
    finally:restore()
    assert not window.events.closing and not window.events.before_show


def test_product_manifest_and_frozen_shared_package_are_self_contained():
    """A tagged installer uses the pinned public product, not another checkout or signed-identity assumption."""
    root=Path(__file__).parents[1]
    manifest=json.loads((root/'packaging/desktop-product.json').read_text())
    assert manifest['appId']=='ag-tasks' and manifest['repository']=='PonomarevRA/ag-tasks'
    assert "collect_submodules('desktop_support')" in (root/'packaging/ag_tasks.spec').read_text()
    assert 'desktop_support' in (root/'pyproject.toml').read_text()


def test_release_version_matches_runtime_package_and_native_manifest():
    """An installed previous release can detect this update without conflicting product identities."""
    import tomllib
    from ag_tasks import __version__
    root = Path(__file__).parents[1]
    product = json.loads((root / 'packaging/desktop-product.json').read_text())
    metadata = tomllib.loads((root / 'pyproject.toml').read_text())
    assert __version__ == product['version'] == metadata['project']['version'] == '0.8.2'
    assert "version='0.8.2'" in (root / 'packaging/ag_tasks.spec').read_text()
    assert "version='" + __version__ + "'" in (root / 'packaging/ag_tasks.spec').read_text()
    from ag_tasks.config import VERSION
    assert VERSION == __version__
