# Resultados de la candidata Fase G

Fecha: 24/10/2026, America/Costa_Rica. Rama: `feat/fase-g-catalogo-contrato`.
Base: `8a58db244ec1e9cd1a0d627b21e086548489e75d`. Sin fusión ni promoción.

## Resultados ejecutados

| Verificación | Resultado | Alcance real |
|---|---|---|
| Runtime inicial | PASS | VM 104 conserva imágenes F2H y SHA baseline |
| `iacpctl doctor` | PASS | Comparación sin cambios, siete roles operativos |
| Snapshot DB/Redis, 22:54:51 CR | PASS | `0011_crm_contract`; seis clases de operaciones no terminales vacías; colas vacías y unacked=0 |
| Regresión completa | PASS | **949 pruebas aprobadas**, 30 advertencias de deprecación, 136,23 s; PostgreSQL y Redis descartables locales |
| Regresión focalizada | PASS | **150 aprobadas**, 5 advertencias, 17,02 s; catálogo, documentación y preservación del contrato histórico |
| Revalidación final de catálogo/documentación | PASS | **63 aprobadas**, 5 advertencias, 3,36 s; incluye los ejemplos locales y el digest fijado |
| Manuales versionados | PASS | `validate-handbook.py`: 22 documentos, 31 contratos autenticados, sin contacto de red |
| OpenAPI candidato G | PASS | OpenAPI 3.1 válido; ejemplos KVM 1/2 válidos; marcado BLOCKED y separado del POST activo |
| Contrato activo candidato | PASS | Solo se agregan GET `/v1/plans` y `/v1/images`; rutas, schemas y semántica histórica preservados |
| Resolución de cuatro planes | PASS | CPU/RAM/disco exactos, mínimos de imagen y variables de expansión de disco; sin Apply |
| Validación de acceso | PASS | Password obligatoria, SSH opcional real, duplicados/formato inválido rechazados y sin contraseña en representaciones serializadas |
| Playwright Chromium escritorio | PASS | Candidata local, sin credenciales de MNG |
| Playwright Chromium móvil | PASS | Viewport 390×844; menú, guía, código, catálogo y regreso |
| Playwright Firefox escritorio | PASS | Candidata local; contenido/estilos, navegación y recarga |
| Entrega root fuera de DB/plan/state | BLOCKED | El adaptador histórico persiste la contraseña; no se conectó al contrato nuevo |
| E2E físico KVM 1/2 | BLOCKED | Disco autorizado actual máximo 40 GiB, plan mínimo 50 GiB; además G6 |
| E2E físico KVM 3/4 | NO EJECUTADO | No se aprovisionaron 200/400 GiB ni se declaran certificados |
| WAN estática real | BLOCKED | Falta certificación de reserva, duplicados, red y conectividad; contrato válido no habilita ejecución |
| Admisión automática G y migración | PENDIENTE | No se adaptaron workers ni bindings de políticas históricos a la nueva admisión |
| Consumidor E2E externo | PENDIENTE | `iacp-load-testing` SHA `f0fe34324b1b5549cf963df02f53c2558d3948ce` sigue usando el contrato antiguo |

En esta corrida completa sí se levantaron PostgreSQL/Redis descartables; no se
arrastran como PASS las omisiones históricas por falta de PostgreSQL. Las pruebas
de integración usan servicios locales, fixtures y transportes simulados; no
acreditan aprovisionamiento físico, consumo real de disco ni resiliencia de MNG.

## Reproducción

La suite usa el Compose de tests existente sin modificarlo:

```sh
docker compose -p iacp-fase-g-tests -f compose.test.yaml run --build --rm -T tests python -m pytest -p no:cacheprovider -q
```

Para documentación, `tests/serve_catalog_docs.py` levanta SQLite temporal y los
handlers reales de lectura en la imagen de tests. Solo se publica
`127.0.0.1:8765`, con identidad sintética y bloqueo de métodos mutadores. No
cargar archivos operativos, secretos ni variables del laboratorio en ese proceso.
`scripts/test-catalog-docs.cjs` requiere la instalación Playwright existente y
`IACP_DOCS_EVIDENCE_DIR` para resultados/capturas. `CHROMIUM_PATH` permite emplear
Chromium del sistema. No añade dependencias al runtime.

La prueba UI comprobó DOM abierto, texto real, coordenadas bajo la cabecera,
enlaces a operaciones desplegadas, historial, recarga y código visible. También
exigió cero errores JavaScript, cero recursos externos y cero métodos mutadores.
La primera ejecución detectó problemas del enlace hacia el catálogo: el click
programático apuntaba al contenedor en lugar del botón real de Swagger y el
hash se registraba dos veces al abrir. Se corrigió el controlador común y se
repitieron las pruebas en los tres navegadores/viewports.

La inspección heurística de los 22 archivos modificados/nuevos detectó diez
coincidencias revisadas: siete apariciones del placeholder de contraseña
ilustrativa en schemas/manual/snapshot, una identidad exclusivamente sintética
del servidor de tests, un nombre de función que contiene `bearer` y el
delimitador de clave privada usado como entrada negativa (sin material de clave).
No se incorporaron valores de credenciales del laboratorio. Este resultado no
certifica ni sanea el historial de secretos del runtime.

Evidencias reales de **la candidata local**, no del runtime desplegado:

- [Resultados Playwright](ui/playwright.json).
- [Chromium escritorio](ui/chromium-escritorio.png).
- [Chromium móvil](ui/chromium-movil.png).
- [Firefox escritorio](ui/firefox-escritorio.png).

Evidencia privada de auditoría en VM 104:
`/opt/iacp/evidence/fase-g-auditoria-20261024T2300CR`. Contiene el resultado real
de doctor y snapshot; este informe publica únicamente conteos/metadatos.

Catálogo `KVMHOST-54.1`, SHA256:
`fa565a7f79f1ebd1c4bf2131c4cc0e0bd07e6598ea1e3c9f88f7082f919bd689`.

## Dictamen

**Fase G completa: NO-GO / parcial**. No hay autorización técnica para presentar
el nuevo POST como disponible. El esquema, resolvedor y catálogos candidatos son
revisables, pero G3/G6 y la migración/E2E siguen pendientes.

**MVP histórico: GO para piloto supervisado**, sujeto a sus limitaciones ya
registradas. **Resiliencia integral: NO-GO**. Esta entrega no cambia ese dictamen.

No se alteraron VM 106, Golden Image 29000, configuración, cuotas, state ni runtime.
No se prepararon imágenes OCI de release ni se fusionaron cambios incompatibles.
