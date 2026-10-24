"""Objeto S3 versionado en loopback; rechaza escrituras al state del observador."""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import RLock, Thread
from urllib.parse import unquote, urlsplit


class VersionedObject:
    def __init__(self, data, bucket, key):
        self.data, self.bucket, self.key = data, bucket, key
        self.version = 1
        self.requests = []
        self.lock = None
        self.state_available = True
        self.guard = RLock()

    @property
    def state_path(self):
        return "/" + self.bucket + "/" + self.key

    def external_write(self, data):
        # Escritor independiente del cliente bajo prueba, sin servicios reales.
        with self.guard:
            self.data = data
            self.version += 1


@contextmanager
def versioned_s3(data, bucket="fixture-state", key="terraform.tfstate"):
    store = VersionedObject(data, bucket, key)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, data=b"", headers=None):
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def error(self, status, code):
            self.respond(status, f"<Error><Code>{code}</Code></Error>".encode(), {"Content-Type": "application/xml"})

        def handle_request(self):
            path = unquote(urlsplit(self.path).path)
            with store.guard:
                store.requests.append((self.command, path))
                if self.command == "GET" and path == "/" + store.bucket:
                    self.respond(200, (f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                                       f'<Name>{store.bucket}</Name><KeyCount>0</KeyCount>'
                                       '<MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated></ListBucketResult>').encode(),
                                 {"Content-Type": "application/xml"})
                elif path == store.state_path:
                    if not store.state_available:
                        self.error(503, "ServiceUnavailable")
                    elif self.command in {"GET", "HEAD"}:
                        self.respond(200, store.data, {"Content-Type": "application/json",
                                     "x-amz-version-id": f"fixture-version-{store.version}",
                                     "ETag": '"' + hashlib.md5(store.data).hexdigest() + '"'})
                    else:
                        self.error(403, "AccessDenied")
                elif path == store.state_path + ".tflock":
                    if self.command == "PUT":
                        data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                        if store.lock is not None:
                            self.error(412, "PreconditionFailed")
                        else:
                            store.lock = data
                            self.respond(200, headers={"ETag": '"fixture-lock"'})
                    elif self.command == "DELETE":
                        store.lock = None
                        self.respond(204)
                    elif self.command == "GET" and store.lock is not None:
                        self.respond(200, store.lock, {"Content-Type": "application/json"})
                    else:
                        self.error(404, "NoSuchKey")
                else:
                    self.error(404, "NoSuchKey")

        do_GET = do_HEAD = do_PUT = do_DELETE = do_POST = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    store.endpoint = f"http://127.0.0.1:{server.server_port}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
