"""Minimal stand-ins for ComfyUI's ``folder_paths`` and ``server`` modules.

They let the production queue server be imported and exercised without ComfyUI,
models or a GPU: by the tests, and by ``tools/preview_page.py``.
"""
import sys
import types
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]


def install(workdir, models=None):
    """Register the stubs and return the imported production_server module.

    ``workdir`` receives the input/ and output/ folders. ``models`` maps a
    models sub-folder name to the filenames ComfyUI would list for it.
    """
    workdir = Path(workdir)
    models = models or {}
    folder_paths = types.ModuleType('folder_paths')
    folder_paths.get_input_directory = lambda: str(workdir / 'input')
    folder_paths.get_output_directory = lambda: str(workdir / 'output')
    folder_paths.models_dir = str(workdir / 'models')
    folder_paths.get_filename_list = lambda kind: list(models.get(kind, []))

    class Queue:
        def get_current_queue(self):return [], []
        def delete_queue_item(self, match):return None

    class Instance:
        routes = web.RouteTableDef()
        app = web.Application()
        prompt_queue = Queue()

    server = types.ModuleType('server')
    server.PromptServer = type('PromptServer', (), {'instance': Instance})
    sys.modules['folder_paths'] = folder_paths
    sys.modules['server'] = server
    for name in [name for name in sys.modules if name.startswith('custom_nodes')]:del sys.modules[name]
    if str(ROOT) not in sys.path:sys.path.insert(0, str(ROOT))
    import custom_nodes.drama_pipeline.production_server as module
    return module


def application(module):
    """A fresh aiohttp app serving the routes the module registered."""
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.add_routes(module.routes)
    return app
