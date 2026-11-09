"""Presentation notifications cannot carry commands, cross origins or outlive their window."""
import json
from threading import Event
from types import SimpleNamespace

import pytest
from ag_tasks.native_presentation import SidebarPresentation, accepts_notification, origin_key, validate_state


def test_exact_origin_and_bounded_presentation_protocol():
    """Only local owned HTTP origins and the three harmless presentation fields are accepted."""
    assert origin_key('http://127.0.0.1:8890/?incidentId=x') == ('http','127.0.0.1',8890)
    for origin in ['https://127.0.0.1:8890','http://localhost:8890','http://user@127.0.0.1:8890','http://example.com:8890']:
        with pytest.raises(ValueError):origin_key(origin)
    good = {'side':'right','collapsed':True,'width':48}
    assert validate_state(good) == good
    for invalid in [{**good,'path':'/tmp'}, {**good,'side':'center'}, {**good,'collapsed':1}, {**good,'width':True}, {**good,'width':float('nan')}, {**good,'width':1601}]:
        assert validate_state(invalid) is None


def test_zeroarg_notifications_coalesce_and_closed_binding_drops_late_results():
    """A resize storm creates one active pull, and shutdown prevents that pull from touching native state."""
    entered, release, complete = Event(), Event(), Event()
    scripts, callbacks, applied = [], [], []

    def read(script):
        """Hold one trusted native DOM read while repeated notifications arrive."""
        scripts.append(script);entered.set();release.wait(2);complete.set()
        return json.dumps({'side':'left','collapsed':False,'width':200})

    window = SimpleNamespace(expose=lambda callback:callbacks.append(callback),events=SimpleNamespace(closed=Event()),get_current_url=lambda:'http://127.0.0.1:8890/',run_js=read)
    binding = SidebarPresentation(window,'http://127.0.0.1:8890',applied.append)
    assert not callbacks, 'No eval-based pywebview expose generator is used'
    with pytest.raises(TypeError):binding.notify({'width':999})
    binding.notify();assert entered.wait(2)
    for _ in range(100):binding.notify()
    assert len(scripts)==1
    binding.close();release.set();assert complete.wait(2)
    assert not applied
    binding.notify();assert len(scripts)==1


def test_foreign_document_never_executes_the_fixed_dom_pull():
    """Presentation notifications from an unowned top-level document grant no browser evaluation."""
    scripts=[]
    window=SimpleNamespace(expose=lambda callback:None,events=SimpleNamespace(closed=Event()),get_current_url=lambda:'http://127.0.0.1:9999/',run_js=lambda script:scripts.append(script))
    binding=SidebarPresentation(window,'http://127.0.0.1:8890',lambda state:None)
    assert not binding._same_origin()
    binding.close();binding.notify();assert not scripts


def test_frame_origin_and_null_body_are_required_before_notification():
    """Foreign main frames, same-origin child frames and non-null bodies cannot notify native layout."""
    origin=('http','127.0.0.1',8890)
    assert accepts_notification(origin,origin,True,True)
    assert not accepts_notification(origin,origin,False,True)
    assert not accepts_notification(origin,('http','127.0.0.1',8891),True,True)
    assert not accepts_notification(origin,origin,True,False)


def test_reload_reinstalls_fixed_wrapper_and_cleanup_is_idempotent():
    """Every document gets the CSP-safe notifier; close releases the native channel once."""
    scripts, applied, cleanup = [], [], []
    window = SimpleNamespace(events=SimpleNamespace(closed=Event()),
        get_current_url=lambda:'http://127.0.0.1:8890/', run_js=scripts.append)
    binding = SidebarPresentation(window,'http://127.0.0.1:8890',applied.append,right_class='')
    binding.notify = lambda: None
    binding.loaded();binding.loaded()
    assert binding.document_generation == 2
    assert len(scripts) == 2
    assert all('postMessage(null)' in script and 'new Function' not in script for script in scripts)
    assert '""' in binding.script
    binding.native_cleanup = lambda:cleanup.append(True)
    binding.close();binding.close()
    assert cleanup == [True] and not binding.active()
