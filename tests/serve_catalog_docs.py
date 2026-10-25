"""Servidor documental descartable: DB temporal y solo lectura.

Ejecutar exclusivamente desde la imagen de tests, publicando en loopback.
Nunca cargar credenciales o configuración del laboratorio en este proceso.
"""
import os
from pathlib import Path
import tempfile

from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import uvicorn

from iacp.db import get_session
from iacp.main import create_app
from tests.helpers import upgrade


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="iacp-g-docs-fixture-") as directory:
        path=Path(directory)/"token"
        path.write_text("identidad-sintetica-solo-pruebas-documentales")
        path.chmod(0o600)
        os.environ["IACP_API_TOKEN_FILE"]=str(path)
        os.environ["IACP_CREATION_CONTRACT"]="commercial-g"
        engine=create_engine(f"sqlite:///{Path(directory)/'docs.sqlite'}",connect_args={"check_same_thread":False})
        upgrade(engine)
        sessions=sessionmaker(bind=engine)
        app=create_app()
        # Try it out solo puede consultar este fixture; nunca la API real.
        app.servers=[{"url":"http://127.0.0.1:8765","description":"Fixture local de solo lectura"}]
        def session_override():
            with sessions() as session:
                yield session
        app.dependency_overrides[get_session]=session_override
        @app.middleware("http")
        async def read_only_fixture(request,call_next):
            if request.method not in {"GET","HEAD","OPTIONS"}:
                return JSONResponse(status_code=405,content={"detail":"Fixture documental de solo lectura"})
            return await call_next(request)
        uvicorn.run(app,host="0.0.0.0",port=8765,log_level="warning")
