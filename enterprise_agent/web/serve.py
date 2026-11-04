"""Independent loopback API and static UI launchers."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def serve_ui(port=5173):
    root = Path(__file__).resolve().parents[2] / "frontend"
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"MAPS UI: http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def serve_api(db_path, port=8000, workers=2, allow_protocol_fixture=False):
    import uvicorn
    from enterprise_agent.web.api import create_app
    # Exactly one web server process; TaskRuntime owns isolated task workers.
    app = create_app(db_path, workers=workers, allow_protocol_fixture=allow_protocol_fixture)
    uvicorn.run(app, host="127.0.0.1", port=port)
