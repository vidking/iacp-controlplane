# Evidencias de preparación G

Fecha: 25/10/2026, America/Costa_Rica.

La revisión posterior incorpora el cliente dedicado `capacity-ro`. Sus 144
pruebas focalizadas pasaron; las pruebas HTTP reales de esa identidad siguen
BLOCKED por material operativo no localizado. No confundir las lecturas previas
con Terraform con validación de la nueva identidad.

**Regresión final: PASS, 1012 pruebas aprobadas, cero omitidas, 30 advertencias
de deprecación, 348,36 segundos.** Ejecutada desde una imagen de pruebas con el
código final, PostgreSQL y Redis descartables. Incluye el cliente RO y los
controles de promoción. No es certificación física ni validación HTTP real del
token pendiente.

Estas evidencias separan tres objetos: main integrado `3e6d0f9`, candidata de
preparación posterior y runtime histórico conservado `8a58db2`. No hubo promoción
G ni nuevas VMs. Los E2E físicos kvm-1/kvm-2 están BLOCKED, no aprobados.

- [Comprobaciones sanitizadas](comprobaciones.json): capacidad con identidad
  Terraform existente, rechazo con identidad API, cuotas preparadas, AES,
  persistencia Redis y snapshot del runtime.
- [Playwright de la candidata local](candidata/playwright.json): Chromium
  escritorio/móvil y Firefox escritorio; cero errores JS, recursos externos y
  solicitudes mutadoras.
- [Candidata escritorio](candidata/chromium-escritorio.png),
  [móvil](candidata/chromium-movil.png) y
  [Firefox](candidata/firefox-escritorio.png).
- [Playwright sobre HTTPS real](runtime-historico/playwright.json): baseline
  histórico autenticado, OpenAPI 3.1 y cuerpo `InstanceCreate`, sin errores JS,
  recursos externos ni mutaciones. **No certifica contrato comercial G.**
- [Runtime histórico escritorio](runtime-historico/escritorio.png) y
  [móvil](runtime-historico/movil.png).

La prueba reproducible de Redis usa
`scripts/test-bootstrap-redis-restart.py`, ejecutado en el stack local
`iacp-g-promocion-tests`, con la DB 15 descartable y reinicio coordinado del
contenedor `test-redis`. Replica AOF y snapshots observados en VM 104. No permite
apuntar al hostname de Redis productivo. Debe iniciarse ese Redis de pruebas con
`--appendonly yes --appendfsync everysec --save '3600 1 300 100 60 10000'` para
comprobar también AOF después del reinicio. El archivo temporal de override no
forma parte del despliegue del IACP Control Plane.

La clave AES usada por la prueba es aleatoria y solo vive en el proceso de
pruebas; no se exporta la clave operativa ni se introducen contraseñas reales.
TTL y DELETE no implican borrado inmediato de ciphertext histórico en RDB/AOF.

La suite de preparación se ejecuta con PostgreSQL/Redis locales descartables:
`docker compose -p iacp-g-promocion-tests -f compose.test.yaml run --build --rm -T tests python -m pytest -p no:cacheprovider -q`.
Se comprobaron también 22 manuales y 31 contratos autenticados sin red. Los
snapshots OpenAPI y schemas comerciales permanecen idénticos a main `3e6d0f9`.
La revisión del catálogo interno cambia su versión/digest, no el JSON de creación.

Revisión de secretos: cinco coincidencias del escáner en código fijo de preflight
y fixtures/aserciones negativas; ninguna credencial operativa incorporada. Las
capturas inspeccionadas muestran documentación y ejemplos ilustrativos, sin
Bearer ni contraseñas reales.

[Informe de alcance, bloqueos y reversión](../../../FASE_G_PROMOCION_20261025.md).
