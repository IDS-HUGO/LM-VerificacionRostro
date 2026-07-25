"""Pruebas de la API HTTP (FastAPI TestClient).

Estas pruebas mockean `DeepFace.extract_faces`/`DeepFace.verify` (a través de
los wrappers `app.pipeline._deepface_extract_faces`/`_deepface_verify`) en
vez de depender de los pesos reales del modelo, porque entornos de CI/sandbox
no siempre tienen acceso a internet para descargarlos. La prueba que sí
ejercita el DeepFace real vive en `tests/test_pipeline.py`, marcada
`@pytest.mark.slow`.
"""
import io
from unittest.mock import Mock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
import app.main as main_module


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """`get_settings` está cacheado con lru_cache; lo limpiamos entre tests
    para que las variables de entorno seteadas por un test no se filtren a
    otro."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    # raise_server_exceptions=False: necesario para poder observar la
    # respuesta 503 que produce nuestro exception_handler(Exception) en vez
    # de que TestClient re-lance la excepción original (comportamiento
    # default de Starlette pensado para debugging).
    return TestClient(app, raise_server_exceptions=False)


def _files(id_bytes: bytes, selfie_bytes: bytes, id_name="id.jpg", selfie_name="selfie.jpg"):
    return {
        "id_document": (id_name, io.BytesIO(id_bytes), "image/jpeg"),
        "selfie": (selfie_name, io.BytesIO(selfie_bytes), "image/jpeg"),
    }


class TestHealth:
    def test_health_returns_200_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestVerifyFailClosedOnInvalidImage:
    def test_corrupt_upload_is_rejected_not_500(self, client, corrupt_image_bytes, solid_color_image_bytes):
        response = client.post(
            "/verify",
            files=_files(corrupt_image_bytes, solid_color_image_bytes),
        )
        # Fail-closed: nunca 200, nunca un 500 con stack trace crudo.
        assert 400 <= response.status_code < 500
        body = response.json()
        assert body["error"] == "INVALID_IMAGE"
        assert "request_id" in body

    def test_corrupt_selfie_is_rejected(self, client, solid_color_image_bytes, corrupt_image_bytes):
        response = client.post(
            "/verify",
            files=_files(solid_color_image_bytes, corrupt_image_bytes),
        )
        assert 400 <= response.status_code < 500
        assert response.json()["error"] == "INVALID_IMAGE"


class TestVerifyOversizedUpload:
    def test_oversized_upload_rejected_413_before_deepface_call(self, client, monkeypatch, solid_color_image_bytes):
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "1")
        get_settings.cache_clear()

        oversized = b"0" * (2 * 1024 * 1024)  # 2 MB > límite de 1 MB configurado

        run_verification_mock = Mock(
            side_effect=AssertionError(
                "run_verification (y por lo tanto DeepFace) no debería invocarse "
                "para un upload rechazado por tamaño"
            )
        )
        monkeypatch.setattr(main_module, "run_verification", run_verification_mock)

        response = client.post(
            "/verify",
            files=_files(oversized, solid_color_image_bytes),
        )

        assert response.status_code == 413
        body = response.json()
        assert body["error"] == "PAYLOAD_TOO_LARGE"
        run_verification_mock.assert_not_called()


class TestVerifyHappyPathMocked:
    def _mock_deepface(self, monkeypatch, distance: float):
        fake_face = np.zeros((100, 100, 3), dtype=np.float64)

        def fake_extract_faces(image, detector_backend):
            return [{"face": fake_face, "confidence": 0.99, "facial_area": {}}]

        def fake_verify(img1, img2, model_name, detector_backend, distance_metric):
            return {"distance": distance, "verified": distance <= 0.32, "model": model_name}

        monkeypatch.setattr("app.pipeline._deepface_extract_faces", fake_extract_faces)
        monkeypatch.setattr("app.pipeline._deepface_verify", fake_verify)

    def test_match_true_when_distance_within_threshold(self, client, monkeypatch, solid_color_image_bytes, another_solid_color_image_bytes):
        self._mock_deepface(monkeypatch, distance=0.10)

        response = client.post(
            "/verify",
            files=_files(solid_color_image_bytes, another_solid_color_image_bytes),
            headers={"X-Request-Id": "test-request-123"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["match"] is True
        assert body["distance"] == pytest.approx(0.10)
        assert body["threshold"] == pytest.approx(0.32)
        assert body["model"] == "ArcFace"
        assert body["request_id"] == "test-request-123"
        assert body["liveness"] == {"status": "skipped", "score": None}
        assert 0.0 <= body["similarity"] <= 1.0

    def test_match_false_when_distance_beyond_threshold(self, client, monkeypatch, solid_color_image_bytes, another_solid_color_image_bytes):
        self._mock_deepface(monkeypatch, distance=0.75)

        response = client.post(
            "/verify",
            files=_files(solid_color_image_bytes, another_solid_color_image_bytes),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["match"] is False
        # request_id generado server-side cuando no se envía X-Request-Id
        assert body["request_id"]

    def test_no_face_on_id_returns_422(self, client, monkeypatch, solid_color_image_bytes, another_solid_color_image_bytes):
        def fake_extract_faces(image, detector_backend):
            return []

        monkeypatch.setattr("app.pipeline._deepface_extract_faces", fake_extract_faces)

        response = client.post(
            "/verify",
            files=_files(solid_color_image_bytes, another_solid_color_image_bytes),
        )

        assert response.status_code == 422
        assert response.json()["error"] == "NO_FACE_ON_ID"

    def test_no_face_on_selfie_returns_422(self, client, monkeypatch, solid_color_image_bytes, another_solid_color_image_bytes):
        fake_face = np.zeros((100, 100, 3), dtype=np.float64)

        def fake_extract_faces(image, detector_backend):
            return [{"face": fake_face, "confidence": 0.99, "facial_area": {}}]

        def fake_verify(img1, img2, model_name, detector_backend, distance_metric):
            raise ValueError("Face could not be detected in numpy array.")

        monkeypatch.setattr("app.pipeline._deepface_extract_faces", fake_extract_faces)
        monkeypatch.setattr("app.pipeline._deepface_verify", fake_verify)

        response = client.post(
            "/verify",
            files=_files(solid_color_image_bytes, another_solid_color_image_bytes),
        )

        assert response.status_code == 422
        assert response.json()["error"] == "NO_FACE_ON_SELFIE"


class TestVerifyUnexpectedFailureIsFailClosed:
    def test_unexpected_exception_returns_503_not_200(self, client, monkeypatch, solid_color_image_bytes, another_solid_color_image_bytes):
        def fake_extract_faces(image, detector_backend):
            raise RuntimeError("boom inesperado, no relacionado a rostros ni infraestructura")

        monkeypatch.setattr("app.pipeline._deepface_extract_faces", fake_extract_faces)

        response = client.post(
            "/verify",
            files=_files(solid_color_image_bytes, another_solid_color_image_bytes),
        )

        assert response.status_code == 503
        body = response.json()
        assert body["error"] == "VERIFICATION_UNAVAILABLE"
        # Nunca debe fugarse el mensaje crudo de la excepción interna al cliente.
        assert "boom inesperado" not in str(body)
