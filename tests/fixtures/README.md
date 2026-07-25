# Fixtures de prueba

Este directorio existe como punto de extensión para fixtures de imagen
reales (p. ej. 2-3 pares INE/selfie de prueba, con consentimiento y sin
datos personales reales) que habilitarían las pruebas `@pytest.mark.slow` de
extremo a extremo contra el DeepFace real.

Deliberadamente **no se commitean imágenes de rostros aquí** (ni sintéticas
de stock ni reales) para evitar cualquier ambigüedad sobre datos biométricos
en el repositorio. En su lugar:

- Las pruebas que solo necesitan "una imagen válida sin rostro" (para
  ejercitar los caminos `NO_FACE_ON_ID`/`NO_FACE_ON_SELFIE` o la
  decodificación básica) generan imágenes sintéticas de color sólido en
  memoria via `tests/conftest.py::encode_solid_color_image`.
- Las pruebas de `/verify` con match/no-match usan mocks de
  `DeepFace.extract_faces`/`DeepFace.verify` (ver `tests/test_api.py`), no
  imágenes reales.
- Si en el futuro se agregan fixtures reales para la prueba lenta de extremo
  a extremo, colócalas aquí y actualiza el test marcado `@pytest.mark.slow`
  en `tests/test_pipeline.py` para leerlas desde este directorio.
