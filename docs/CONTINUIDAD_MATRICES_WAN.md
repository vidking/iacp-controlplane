# Continuidad de las matrices WAN

Estado: **BLOCKED**, 2026-11-17. Rama: `devops/features`. Producción sin cambios.

1. El equipo de operaciones debe resolver la falta de ARP hacia `190.0.231.1` en VLAN 211 desde las
   VMs del nodo `mng01`, sin alterar el perfil ni utilizar LAN para certificar WAN.
2. Confirmar la imagen ejecutada y el manifiesto canónico
   `/etc/iacp/stacks/manifest-dev.json`. El archivo de entorno es
   `/etc/iacp/stacks/dev.env`; el Bearer dev se obtiene allí privadamente.
3. Comprobar seis workers de infraestructura sanos e idle, dos de servicios y
   ausencia de operaciones activas o inciertas. Conservar los datos históricos.
4. Ejecutar primero KVM ciclo 1 y ciclo 2 con `scripts/wan_matrix.py`; después
   habilitar Ansible mediante `IACP_SERVICE_EXECUTION_ENABLED=1` y
   `service_execution_enabled=true` en el manifiesto. Aplicar el cambio con
   `iacpctl`, nunca reiniciando otros stacks.
5. Ejecutar servicios ciclos 1 y 2; finalmente agentes ciclos 1 y 2. Ante fallo,
   detener nuevas inyecciones, recuperar y limpiar únicamente instancias propias
   antes de repetir el ciclo. Mantener los resultados fallidos.

Ejemplo para cada categoría, usando el Python del entorno operativo privado y el
release realmente desplegado:

```bash
sudo /opt/iacp/tools/services-pilot-venv/bin/python \
  /opt/iacp/releases/controlplane-REVISION/scripts/wan_matrix.py \
  --category kvm --cycle 1 --run MARCA_UTC_NUMERICA \
  --evidence /opt/iacp/evidence/wan-kvm-ciclo1-MARCA_UTC_NUMERICA
```

Cambiar `--category` a `services` o `agents`, y `--cycle` a 2 cuando corresponda.
Los directorios de evidencia deben ser nuevos. No pasar credenciales en argumentos.
El runner fija el dominio dev y comprueba el header de revisión contra el entorno.
Cada categoría contiene 16 casos y dos controles para mantener tandas de seis.

La CA del piloto está en el directorio privado indicado por
`IACP_SERVICE_CONFIG_DIR`; los workers necesitan `IACP_SERVICE_IP_TLS_ENABLED=1`.
Su clave privada no debe salir del Control Plane. La modalidad web por IP usa esa
CA y exige validación TLS. Los agentes usan `agent_mode=local`: no se certifican
inferencia, Telegram ni proveedores externos.

Solo preparar promoción a `main` al aprobar ambos ciclos de cada categoría,
recuperación y limpieza. Reutilizar la imagen inmutable certificada y ajustar
únicamente la configuración productiva, incluida WAN exclusiva. No fusionar ni
actualizar producción como parte de esta continuidad.
