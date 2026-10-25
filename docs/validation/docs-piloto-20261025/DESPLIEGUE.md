# Promoción documental preparada — IACP Control Plane (VM 104)

Fecha: 25/10/2026, America/Costa_Rica. **Pendiente de merge y de ejecución.** No se ha creado un release, tag Git o imagen desplegada para este correctivo.

## Condiciones previas

1. Integrar el PR revisado y registrar el SHA exacto de main, verificando contenido también si se utiliza squash. El único código de aplicación modificado es la presentación documental; no hay migraciones ni cambios de contrato.
2. Comprobar por SSH existente a **VM 104**, sin SSH a nodos MNG: release ejecutado, imágenes por ImageID, readiness, PostgreSQL/Redis, workers, operaciones, colas y locks. El baseline operativo certificado es dedb344; no asumir que sigue vigente sin comprobarlo.
3. Capturar inventory/runtime_snapshot y respaldo por el procedimiento vigente; `runtime_snapshot.py` se ejecuta desde la ruta real del release en el host y con sus dependencias, no desde una ruta del host supuestamente montada en un contenedor. Preservar las imágenes anteriores y probar la restauración del backup aislada.
4. Preparar checkout del commit **fusionado**, imágenes locales identificables y manifiesto privado revisado. Conservar el origen IaC, perfiles, cuotas y red, secretos G6, volúmenes y Compose canónico. Usar los mecanismos de build existentes; no iniciar OCI/registry ni actualizar dependencias.
5. Registrar el cambio autorizado y la revisión del manifiesto mediante los validadores vigentes. No reutilizar una ventana de autorización vencida ni inventar evidencia de aprobaciones, salud o restauración. Verificar ausencia de operaciones activas antes de recrear servicios.

## Secuencia vigente

Definir estas variables con las rutas reales del **nuevo release fusionado y manifiesto revisado**. Son referencias operativas, no secretos. Los directorios de evidencia deben ser nuevos y privados; no sobrescribir los de Fase G.

```bash
IACP_RELEASE_DIR='/ruta/real/del/release-fusionado'
IACP_MANIFEST='/ruta/privada/al/manifiesto-revisado.json'
IACP_CANDIDATE_ENV='/ruta/privada/al/entorno-candidato'
IACP_EVIDENCE='/opt/iacp/evidence/correctivo-docs-NUEVO'

sudo python3 "$IACP_RELEASE_DIR/scripts/go_live.py" inventory \
  --manifest "$IACP_MANIFEST" --evidence-dir "$IACP_EVIDENCE-inventory"
sudo python3 "$IACP_RELEASE_DIR/scripts/go_live.py" preflight \
  --manifest "$IACP_MANIFEST" --evidence-dir "$IACP_EVIDENCE-preflight"
sudo python3 "$IACP_RELEASE_DIR/scripts/iacpctl" \
  --env-file /etc/iacp/server.env \
  --compose-file "$IACP_RELEASE_DIR/deploy/mng/compose.yaml" \
  --manifest "$IACP_MANIFEST" --evidence-dir "$IACP_EVIDENCE-env" \
  env-promote --candidate "$IACP_CANDIDATE_ENV"
sudo python3 "$IACP_RELEASE_DIR/scripts/iacpctl" \
  --env-file /etc/iacp/server.env \
  --compose-file "$IACP_RELEASE_DIR/deploy/mng/compose.yaml" \
  --manifest "$IACP_MANIFEST" --evidence-dir "$IACP_EVIDENCE-deploy" deploy
```

La promoción incluye sus verificaciones y cercado de admisión. Restablecer la activación comercial solo mediante `activate-lab` y su revisión vigente, si el procedimiento la dejó cerrada. Mantener los valores reales de tenant/cuota y el perfil de solo lectura que ya acepta el validador. No usar un `verify` con expectativas de flags cerrados para certificar un runtime activado; el chequeo final correspondiente es `iacpctl doctor` con el manifiesto real.

```bash
sudo python3 "$IACP_RELEASE_DIR/scripts/iacpctl" \
  --env-file /etc/iacp/server.env \
  --compose-file "$IACP_RELEASE_DIR/deploy/mng/compose.yaml" \
  --manifest "$IACP_MANIFEST" --evidence-dir "$IACP_EVIDENCE-doctor" doctor
```

## Certificación HTTPS posterior

Repetir `scripts/test-pilot-docs.cjs` con `IACP_DOCS_BASE=https://iac.cyberfuel.com`, un directorio de evidencia nuevo e `IACP_DOCS_AUTH=bearer`. El modo real recibe el Bearer ya autorizado **por stdin privado**, nunca por argumentos ni un archivo en Git. Resolver su origen desde la configuración/montajes existentes; no pedir ni inventar valores. El modo de fixture usa exclusivamente su identidad sintética y no prueba credenciales LAB.

Registrar SHA del checkout y procedencia del código realmente ejecutado, ImageID, contrato antes/después, capturas HTTPS, copia y navegación, salud/colas/locks. El script permite únicamente GET/HEAD/OPTIONS; no valida creación destructiva. La compatibilidad del cuerpo y la respuesta se certificó con schemas actuales y evidencia física previa; si se decide repetir una VM piloto, hacerlo como un ensayo expresamente registrado, secuencial, con cuota y limpieza, nunca como parte oculta de navegación.

## Reversión

Conservar el manifiesto anterior, imágenes previas compatibles con 0012 y sus respaldos. Ante regresión, utilizar el rollback funcional vigente para cerrar admisión y preservar intenciones; restaurar la presentación anterior mediante el procedimiento revisado de promoción de esas imágenes. No hacer downgrade de datos: este correctivo no introduce migraciones. Confirmar salud y conciliación antes de reabrir el piloto.

No usar `down -v`, borrar state/versiones o forzar locks. No tocar Ceph, RBAC, Golden Images ni VM 104 desde la API de aprovisionamiento. Los comandos anteriores pertenecen al despliegue del software **dentro del IACP Control Plane**, no a operaciones de VM en MNG.
